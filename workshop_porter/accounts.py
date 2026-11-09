"""Community Steam account pool: submission, validation and health checks."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.request import urlopen

from . import catalog
from .config import Config, SteamAccount
from .database import Database, utc_now
from .steamcmd import SteamCmd
from .vault import CredentialVault


log = logging.getLogger(__name__)

# Transient failures (rate limit, Steam down) are retried soon; an account is
# only flagged broken after this many of them in a row.
MAX_TRANSIENT_FAILURES = 3
RETRY_MINUTES = 5
MAX_CATALOGUE_PROBES = 300

DISPLAY_NAME_RE = re.compile(r"^[\w .\-\[\]()|]{2,32}$", re.UNICODE)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@\-]{2,64}$")
PLACEHOLDER_USERNAMES = {"", "your-steam-login"}


def _later(minutes: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


class AccountPool:
    def __init__(
        self,
        config: Config,
        database: Database,
        vault: CredentialVault,
        steamcmd: SteamCmd | None = None,
        opener=urlopen,
    ):
        self.config = config
        self.database = database
        self.vault = vault
        self.steamcmd = steamcmd or SteamCmd(config)
        self.opener = opener

    # ----- games --------------------------------------------------------

    def known_games(self) -> dict[int, str]:
        """Configured [[games]] plus Workshop titles found in account libraries."""
        games = {game.app_id: game.name for game in self.config.games}
        catalogue = self.database.app_catalog()
        for app_id in self.database.workshop_app_ids():
            games.setdefault(app_id, catalogue[app_id]["name"])
        return games

    def supported_games(self) -> list[dict[str, Any]]:
        """Known games with the number of working accounts that own each."""
        names = self.known_games()
        counts = {app_id: 0 for app_id in self.config_game_ids()}
        for account in self.database.list_accounts():
            if account["enabled"] and account["status"] == "working":
                for app_id in account["owned_app_ids"]:
                    if app_id in names:
                        counts[app_id] = counts.get(app_id, 0) + 1
        games = [
            {"app_id": app_id, "name": names[app_id], "accounts": count}
            for app_id, count in counts.items()
        ]
        games.sort(key=lambda game: game["name"].lower())
        return games

    def config_game_ids(self) -> list[int]:
        return [game.app_id for game in self.config.games]

    # ----- submission ---------------------------------------------------

    @staticmethod
    def clean_display_name(display_name: str) -> str:
        display_name = " ".join(display_name.split())
        if not DISPLAY_NAME_RE.fullmatch(display_name):
            raise ValueError(
                "Display name must be 2-32 characters (letters, numbers, spaces, . - _)"
            )
        return display_name

    def rename(self, account: dict[str, Any], display_name: str) -> str:
        display_name = self.clean_display_name(display_name)
        try:
            username = self.vault.decrypt(account["username_enc"])
        except RuntimeError:
            username = ""
        # The display name is public, so it must never reveal the login.
        if display_name.lower() == username.lower():
            raise ValueError("The display name cannot be the Steam login")
        self.database.update_account(account["id"], display_name=display_name)
        return display_name

    def submit(
        self, display_name: str, username: str, password: str, guard_code: str = ""
    ) -> tuple[dict[str, Any], bool]:
        display_name = self.clean_display_name(display_name)
        username = username.strip()
        guard_code = guard_code.strip()
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("That does not look like a Steam login name")
        if not 1 <= len(password) <= 128:
            raise ValueError("Enter the Steam password")
        if guard_code and not re.fullmatch(r"[A-Za-z0-9]{5}", guard_code):
            raise ValueError("Steam Guard codes are 5 letters or numbers")
        if display_name.lower() == username.lower():
            raise ValueError("Pick a display name that is not your Steam login")
        return self.database.create_account(
            self._new_account(display_name, username, password, guard_code),
            self.config.max_pending_accounts,
        )

    def _new_account(
        self, display_name: str, username: str, password: str, guard_code: str,
        reason: str = "Waiting for validation", steam_id: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        return {
            "id": uuid.uuid4().hex,
            "display_name": display_name,
            "username_hash": self.vault.fingerprint(username),
            "username_enc": self.vault.encrypt(username),
            "password_enc": self.vault.encrypt(password),
            "guard_code_enc": self.vault.encrypt(guard_code) if guard_code else None,
            "status": "pending",
            "status_reason": reason,
            "created_at": now,
            "next_check_at": now,
            "steam_id": steam_id or None,
        }

    def import_config_accounts(self) -> None:
        """Move [steam] / [[steam.accounts]] logins from config.toml into the pool."""
        for account in self.config.steam_accounts:
            if account.username.lower() in PLACEHOLDER_USERNAMES or not account.password:
                continue
            if self.database.account_exists(self.vault.fingerprint(account.username)):
                continue
            name = account.name if account.name != account.username else "Server account"
            name = name if DISPLAY_NAME_RE.fullmatch(name) else "Server account"
            self.database.create_account(
                self._new_account(
                    name, account.username, account.password, account.guard_code,
                    reason="Imported from config.toml", steam_id=account.steam_id,
                ),
                max_pending=10**6,
            )
            log.info("Imported config.toml Steam account %r into the account pool", name)

    # ----- checks -------------------------------------------------------

    def credentials(self, account: dict[str, Any], with_guard: bool = False) -> SteamAccount:
        guard = ""
        if with_guard and account.get("guard_code_enc"):
            guard = self.vault.decrypt(account["guard_code_enc"])
        return SteamAccount(
            name=account["display_name"],
            username=self.vault.decrypt(account["username_enc"]),
            password=self.vault.decrypt(account["password_enc"]),
            guard_code=guard,
            steam_id=account.get("steam_id") or "",
        )

    def check_due_account(self) -> bool:
        """Check one account whose check is due. Returns True if one ran."""
        account = self.database.next_due_account(utc_now())
        if not account:
            return False
        self.check(account)
        return True

    def check(self, account: dict[str, Any]) -> dict[str, Any]:
        account_id = account["id"]
        # app_status is checked for configured games and games the account
        # already had. Accounts whose library the Web API cannot see are also
        # probed against Workshop games found in other libraries.
        probe = set(self.config_game_ids()) | set(account["owned_app_ids"])
        if account.get("library_visible") != 1:
            probe |= set(self.database.workshop_app_ids()[:MAX_CATALOGUE_PROBES])
        probe_ids = sorted(probe)
        try:
            result = self.steamcmd.check_account(
                self.credentials(account, with_guard=True), probe_ids
            )
        except Exception as exc:  # SteamCMD missing, key changed, ...
            log.exception("Account check %s crashed", account_id)
            self._failure(account, "Check could not run", str(exc), transient=True)
            return self.database.get_account(account_id)

        # A Steam Guard code is single-use; drop it after the first attempt.
        if account.get("guard_code_enc"):
            self.database.update_account(account_id, guard_code_enc=None)
        if not result.logged_in:
            self._failure(account, result.reason, result.detail, result.transient)
            return self._logged(account)

        now = utc_now()
        steam_id = result.steam_id or account.get("steam_id") or ""
        owned = set(result.owned_app_ids)
        library_visible, scan_incomplete = None, False
        if self.config.steam_web_api_key and steam_id:
            library = catalog.fetch_owned_games(
                self.config.steam_web_api_key, steam_id, self.opener
            )
            library_visible = library is not None
            if library:
                self.database.remember_apps(library)
                scan_incomplete = self._scan_store(list(library))
                owned |= self._workshop_games(library)

        common = dict(
            last_error=None,
            consecutive_failures=0,
            last_checked_at=now,
            last_ok_at=now,
            steam_id=steam_id or None,
            library_visible=library_visible,
        )
        if owned:
            self.database.update_account(
                account_id,
                status="working",
                status_reason="",
                owned_app_ids=sorted(owned),
                broken_since=None,
                next_check_at=_later(
                    RETRY_MINUTES if scan_incomplete else self.config.account_check_minutes
                ),
                **common,
            )
        elif scan_incomplete:
            self.database.update_account(
                account_id,
                status_reason="Scanning the game library",
                owned_app_ids=[],
                next_check_at=_later(RETRY_MINUTES),
                **common,
            )
        else:
            self.database.update_account(
                account_id,
                status="broken",
                status_reason=(
                    "Owns none of the supported games"
                    if library_visible is not False
                    else "Owns none of the supported games (library is private)"
                ),
                owned_app_ids=[],
                broken_since=account.get("broken_since") or now,
                next_check_at=_later(self.config.broken_recheck_minutes),
                **common,
            )
        return self._logged(account)

    def _workshop_games(self, library: dict[int, str]) -> set[int]:
        catalogue = self.database.app_catalog(list(library))
        return {app_id for app_id, row in catalogue.items() if row["workshop"] == 1}

    def _scan_store(self, app_ids: list[int]) -> bool:
        """Ask the Store which unknown apps have a Workshop. True if some remain."""
        catalogue = self.database.app_catalog(app_ids)
        unknown = [a for a, row in catalogue.items() if row["workshop"] is None]
        batch = unknown[: self.config.max_store_lookups_per_check]
        for app_id in batch:
            self.database.set_app_workshop(app_id, catalog.has_workshop(app_id, self.opener))
        if batch:
            log.info("Steam catalogue: inspected %s apps, %s left", len(batch), len(unknown) - len(batch))
        return len(unknown) > len(batch)

    def _logged(self, account: dict[str, Any]) -> dict[str, Any]:
        updated = self.database.get_account(account["id"])
        log.info(
            "Account %s (%s) checked: %s %s games=%s",
            account["id"][:8], account["display_name"], updated["status"],
            updated["status_reason"], len(updated["owned_app_ids"]),
        )
        return updated

    def _failure(
        self, account: dict[str, Any], reason: str, detail: str, transient: bool
    ) -> None:
        now = utc_now()
        failures = account["consecutive_failures"] + 1
        if transient and failures < MAX_TRANSIENT_FAILURES:
            # Keep the current status; Steam hiccups should not flag accounts.
            self.database.update_account(
                account["id"],
                status_reason=(
                    account["status_reason"] if account["status"] == "working"
                    else f"Retrying: {reason}"
                ),
                last_error=detail[-4000:],
                consecutive_failures=failures,
                last_checked_at=now,
                next_check_at=_later(RETRY_MINUTES),
            )
            return
        self.database.update_account(
            account["id"],
            status="broken",
            status_reason=reason,
            last_error=detail[-4000:],
            consecutive_failures=failures,
            last_checked_at=now,
            broken_since=account.get("broken_since") or now,
            next_check_at=_later(self.config.broken_recheck_minutes),
        )

    def request_recheck(self, account_id: str) -> None:
        self.database.update_account(account_id, next_check_at=utc_now())

    # ----- downloads ----------------------------------------------------

    def accounts_for_app(self, app_id: int) -> list[dict[str, Any]]:
        return self.database.working_accounts_for_app(app_id)

    def mark_used(self, account_id: str) -> None:
        self.database.update_account(account_id, last_used_at=utc_now())
