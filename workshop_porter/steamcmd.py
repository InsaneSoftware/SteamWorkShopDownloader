from __future__ import annotations

import os
import re
import subprocess
import logging
from collections.abc import Callable
from pathlib import Path

from .config import Config


PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
PATH_RE = re.compile(r'Success\. Downloaded item \d+ to "([^"]+)"')
FAILURE_RE = re.compile(r"ERROR!\s+Download item\s+(\d+)\s+failed\s+\(([^)]+)\)", re.IGNORECASE)


log = logging.getLogger(__name__)


class SteamCmdError(RuntimeError):
    pass


class SteamCmd:
    def __init__(self, config: Config):
        self.config = config

    def download(
        self,
        app_id: int,
        workshop_id: str,
        progress: Callable[[int, str], None],
    ) -> Path:
        if not self.config.steamcmd_path.is_file():
            raise SteamCmdError(f"SteamCMD was not found at {self.config.steamcmd_path}")
        if not self.config.steam_username or not self.config.steam_password:
            raise SteamCmdError("STEAM_USERNAME and STEAM_PASSWORD are not configured")

        command = [
            str(self.config.steamcmd_path),
            "+@ShutdownOnFailedCommand", "1",
            "+@NoPromptForPassword", "1",
            "+login", self.config.steam_username, self.config.steam_password,
        ]
        if self.config.steam_guard_code:
            command.append(self.config.steam_guard_code)
        command.extend(
            ["+workshop_download_item", str(app_id), workshop_id, "validate", "+quit"]
        )

        # Do not log the original command: its login arguments contain secrets.
        safe_command = [self._redact(argument) for argument in command]
        log.info(
            "Starting SteamCMD download: app_id=%s workshop_id=%s executable=%s cwd=%s expected_content=%s command=%s",
            app_id,
            workshop_id,
            self.config.steamcmd_path,
            self.config.steamcmd_path.parent,
            self.config.steam_content_root / str(app_id) / workshop_id,
            " ".join(safe_command),
        )

        process = subprocess.Popen(
            command,
            cwd=self.config.steamcmd_path.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
        tail: list[str] = []
        downloaded_path: Path | None = None
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = self._redact(raw_line.strip())
            if not line:
                continue
            tail.append(line)
            tail = tail[-100:]
            # SteamCMD often puts its only useful diagnostic on stdout. It has
            # already been redacted above, so preserve it in the service log.
            log.info("SteamCMD [%s]: %s", workshop_id, line)
            match = PERCENT_RE.search(line)
            if match:
                percent = min(100, int(float(match.group(1))))
                progress(5 + int(percent * 0.7), "Downloading from Steam")
            elif "Success. Downloaded item" in line:
                path_match = PATH_RE.search(line)
                if path_match:
                    downloaded_path = Path(path_match.group(1))
                progress(76, "Workshop item downloaded")

        return_code = process.wait()
        target = downloaded_path or (
            self.config.steam_content_root / str(app_id) / workshop_id
        )
        downloaded = target.is_file() or (
            target.is_dir() and any(target.iterdir())
        )
        failure = next((line for line in reversed(tail) if FAILURE_RE.search(line)), None)
        log.info(
            "SteamCMD completed: app_id=%s workshop_id=%s return_code=%s target=%s exists=%s",
            app_id,
            workshop_id,
            return_code,
            target,
            downloaded,
        )
        if failure:
            log.error("SteamCMD reported a workshop download failure: %s", failure)
        if return_code != 0 and not downloaded:
            detail = "\n".join(tail[-8:]) or f"SteamCMD exited with code {return_code}"
            raise SteamCmdError(detail)

        if not downloaded:
            detail = "\n".join(tail[-8:])
            raise SteamCmdError(
                "SteamCMD finished but the workshop content was not created."
                + (f"\n{detail}" if detail else "")
            )
        return target

    def _redact(self, value: str) -> str:
        for secret in (
            self.config.steam_username,
            self.config.steam_password,
            self.config.steam_guard_code,
        ):
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
