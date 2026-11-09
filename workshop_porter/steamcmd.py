from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, SteamAccount


PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
PATH_RE = re.compile(r'Success\. Downloaded item \d+ to "([^"]+)"')
FAILURE_RE = re.compile(r"ERROR!\s+Download item\s+(\d+)\s+failed\s+\(([^)]+)\)", re.IGNORECASE)
APP_STATUS_RE = re.compile(r"^AppID (\d+)\b")
ACCOUNT_ID_RE = re.compile(r"\[U:1:(\d+)\]")
STEAM_ID64_BASE = 76561197960265728
LOGIN_OK_MARKERS = ("waiting for user info...ok", "logged in ok")

# (substring in lowercased output, short public reason, transient?)
# Order matters: the first matching rule wins.
LOGIN_FAILURES: tuple[tuple[str, str, bool], ...] = (
    ("rate limit", "Steam rate limit reached", True),
    ("invalid password", "Wrong login or password", False),
    ("invalid login auth code", "Steam Guard is blocking the login", False),
    ("two-factor", "Steam Guard is blocking the login", False),
    ("twofactor", "Steam Guard is blocking the login", False),
    ("account logon denied", "Steam Guard is blocking the login", False),
    ("steam guard", "Steam Guard is blocking the login", False),
    ("confirm the login", "Steam Guard is blocking the login", False),
    ("account disabled", "Account disabled or locked by Steam", False),
    ("accountdisabled", "Account disabled or locked by Steam", False),
    ("account locked", "Account disabled or locked by Steam", False),
    ("password unset", "Wrong login or password", False),
    ("no connection", "Could not reach Steam", True),
    ("service unavailable", "Could not reach Steam", True),
    ("try another cm", "Could not reach Steam", True),
    ("timeout", "Could not reach Steam", True),
)


log = logging.getLogger(__name__)


@dataclass
class AccountCheck:
    logged_in: bool
    owned_app_ids: list[int] = field(default_factory=list)
    steam_id: str = ""
    reason: str = ""
    transient: bool = False
    detail: str = ""


class SteamCmdError(RuntimeError):
    def __init__(self, message: str, login_failed: bool = False):
        super().__init__(message)
        self.login_failed = login_failed


@dataclass
class _RunResult:
    return_code: int
    lines: list[str]
    logged_in: bool
    timed_out: bool


class SteamCmd:
    def __init__(self, config: Config):
        self.config = config

    # ----- public API ---------------------------------------------------

    def check_account(self, account: SteamAccount, app_ids: list[int]) -> AccountCheck:
        """Log in and report which of ``app_ids`` the account owns.

        ``app_status`` reports ``Subscribed`` for owned apps and ``No License``
        otherwise. ``licenses_print`` is not used because SteamCMD truncates
        long package app lists.
        """
        commands: list[str] = []
        for app_id in app_ids:
            commands += ["+app_status", str(app_id)]
        result = self._run(
            account,
            commands,
            keep_lines=max(200, len(app_ids) * 8 + 100),
            total_timeout=self.config.steamcmd_timeout_seconds + 60 + len(app_ids),
        )
        detail = "\n".join(result.lines[-12:])
        if not result.logged_in:
            reason, transient = classify_login_failure(result.lines, result.timed_out)
            return AccountCheck(False, reason=reason, transient=transient, detail=detail)
        wanted = set(app_ids)
        return AccountCheck(
            logged_in=True,
            owned_app_ids=[a for a in parse_owned_apps(result.lines) if a in wanted],
            steam_id=parse_steam_id(result.lines),
            detail=detail,
        )

    def validate_account(self, account: SteamAccount) -> tuple[bool, str]:
        """Check SteamCMD credentials without downloading any content."""
        try:
            result = self.check_account(account, [])
        except SteamCmdError as exc:
            return False, str(exc)
        return (True, "authenticated") if result.logged_in else (False, result.reason)

    def download(
        self,
        app_id: int,
        workshop_id: str,
        progress: Callable[[int, str], None],
        account: SteamAccount,
    ) -> Path:
        log.info(
            "Starting SteamCMD download: account=%s app_id=%s workshop_id=%s executable=%s cwd=%s expected_content=%s",
            account.name,
            app_id,
            workshop_id,
            self.config.steamcmd_path,
            self.config.steamcmd_path.parent,
            self.config.steam_content_root / str(app_id) / workshop_id,
        )
        if not self.config.steamcmd_path.is_file():
            raise SteamCmdError(f"SteamCMD was not found at {self.config.steamcmd_path}")
        self._clear_app_workshop_cache(app_id)

        downloaded_path: Path | None = None

        def on_line(line: str) -> None:
            nonlocal downloaded_path
            # SteamCMD often puts its only useful diagnostic on stdout. It has
            # already been redacted, so preserve it in the service log.
            log.info("SteamCMD [%s]: %s", workshop_id, line)
            match = PERCENT_RE.search(line)
            if "Success. Downloaded item" in line:
                path_match = PATH_RE.search(line)
                if path_match:
                    downloaded_path = Path(path_match.group(1))
                progress(76, "Workshop item downloaded")
            elif match and "update" not in line.lower():
                percent = min(100, int(float(match.group(1))))
                progress(5 + int(percent * 0.7), "Downloading from Steam")

        result = self._run(
            account,
            ["+workshop_download_item", str(app_id), workshop_id, "validate"],
            on_line=on_line,
            keep_lines=100,
        )
        if not result.logged_in:
            reason, _ = classify_login_failure(result.lines, result.timed_out)
            raise SteamCmdError(
                f"Steam login failed for {account.name}: {reason}\n"
                + "\n".join(result.lines[-8:]),
                login_failed=True,
            )

        target = downloaded_path or (
            self.config.steam_content_root / str(app_id) / workshop_id
        )
        downloaded = target.is_file() or (target.is_dir() and any(target.iterdir()))
        failure = next(
            (line for line in reversed(result.lines) if FAILURE_RE.search(line)), None
        )
        log.info(
            "SteamCMD completed: app_id=%s workshop_id=%s return_code=%s target=%s exists=%s",
            app_id, workshop_id, result.return_code, target, downloaded,
        )
        if failure:
            log.error("SteamCMD reported a workshop download failure: %s", failure)
        if result.return_code != 0 and not downloaded:
            detail = "\n".join(result.lines[-8:])
            raise SteamCmdError(
                detail or f"SteamCMD exited with code {result.return_code}"
            )
        if not downloaded:
            detail = "\n".join(result.lines[-8:])
            raise SteamCmdError(
                "SteamCMD finished but the workshop content was not created."
                + (f"\n{detail}" if detail else "")
            )
        return target

    # ----- internals ----------------------------------------------------

    def _run(
        self,
        account: SteamAccount,
        commands: list[str],
        on_line: Callable[[str], None] | None = None,
        keep_lines: int = 40,
        total_timeout: float | None = None,
    ) -> _RunResult:
        if not self.config.steamcmd_path.is_file():
            raise SteamCmdError(f"SteamCMD was not found at {self.config.steamcmd_path}")
        if not account.username or not account.password:
            raise SteamCmdError("No Steam login is available", login_failed=True)

        command = [
            str(self.config.steamcmd_path),
            "+@ShutdownOnFailedCommand", "1",
            "+@NoPromptForPassword", "1",
            "+login", account.username, account.password,
        ]
        if account.guard_code:
            command.append(account.guard_code)
        command += [*commands, "+quit"]

        process = subprocess.Popen(
            command,
            cwd=self.config.steamcmd_path.parent,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )

        logged_in = threading.Event()
        timed_out = threading.Event()

        def kill_if(condition: Callable[[], bool]) -> None:
            if condition() and process.poll() is None:
                timed_out.set()
                process.kill()

        # A login waiting for phone approval or a Steam Guard prompt would
        # otherwise block the single worker forever.
        login_timer = threading.Timer(
            self.config.steamcmd_timeout_seconds,
            kill_if,
            args=(lambda: not logged_in.is_set(),),
        )
        login_timer.daemon = True
        login_timer.start()
        total_timer = None
        if total_timeout:
            total_timer = threading.Timer(total_timeout, kill_if, args=(lambda: True,))
            total_timer.daemon = True
            total_timer.start()

        lines: list[str] = []
        try:
            assert process.stdout is not None
            for raw_line in process.stdout:
                line = self._redact(raw_line.strip(), account)
                if not line:
                    continue
                lines.append(line)
                if len(lines) > keep_lines:
                    del lines[: len(lines) - keep_lines]
                if not logged_in.is_set() and any(
                    marker in line.lower() for marker in LOGIN_OK_MARKERS
                ):
                    logged_in.set()
                if on_line:
                    on_line(line)
            return_code = process.wait()
        finally:
            login_timer.cancel()
            if total_timer:
                total_timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()

        return _RunResult(
            return_code=return_code,
            lines=lines,
            logged_in=logged_in.is_set(),
            timed_out=timed_out.is_set(),
        )

    def _clear_app_workshop_cache(self, app_id: int) -> None:
        """Remove only this app's cached Workshop state before downloading.

        SteamCMD can retain an app workshop manifest after an interrupted cache
        cleanup. That manifest makes it try to reuse files that no longer exist,
        causing a generic ``Download item failed (Failure)`` response.
        """
        content_root = self.config.steam_content_root.resolve()
        workshop_root = content_root.parent
        targets = (
            content_root / str(app_id),
            workshop_root / "downloads" / str(app_id),
            workshop_root / f"appworkshop_{app_id}.acf",
        )
        for target in targets:
            try:
                target.relative_to(workshop_root)
            except ValueError as exc:
                raise SteamCmdError(
                    f"Refusing to clean Workshop cache outside {workshop_root}: {target}"
                ) from exc
            if target.is_symlink():
                raise SteamCmdError(f"Refusing to clean symlinked Workshop cache path: {target}")
            if target.is_dir():
                log.info("Removing stale Steam Workshop cache directory: %s", target)
                shutil.rmtree(target)
            elif target.is_file():
                log.info("Removing stale Steam Workshop cache file: %s", target)
                target.unlink()

    def _redact(self, value: str, account: SteamAccount | None = None) -> str:
        secrets = [
            secret
            for item in self.config.steam_accounts
            for secret in (item.username, item.password, item.guard_code)
        ]
        if account:
            secrets.extend((account.username, account.password, account.guard_code))
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value


def classify_login_failure(lines: list[str], timed_out: bool) -> tuple[str, bool]:
    """Return ``(public reason, transient)`` for a failed SteamCMD login."""
    text = "\n".join(lines).lower()
    for needle, reason, transient in LOGIN_FAILURES:
        if needle in text:
            return reason, transient
    if timed_out:
        return "Login timed out (Steam Guard approval?)", True
    return "Login failed for an unknown reason", True


def parse_owned_apps(lines: list[str]) -> list[int]:
    """Parse ``app_status`` output; an app is owned when it is Subscribed."""
    owned: list[int] = []
    current: int | None = None
    for line in lines:
        match = APP_STATUS_RE.match(line)
        if match:
            current = int(match.group(1))
            continue
        if current is not None and "release state:" in line:
            if "Subscribed" in line and "No License" not in line:
                owned.append(current)
            current = None
    return owned


def parse_steam_id(lines: list[str]) -> str:
    """SteamID64 from the ``Logging in user '...' [U:1:N]`` line."""
    for line in lines:
        match = ACCOUNT_ID_RE.search(line)
        if match and int(match.group(1)) > 0:
            return str(STEAM_ID64_BASE + int(match.group(1)))
    return ""
