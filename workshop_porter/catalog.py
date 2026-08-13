from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from urllib.parse import urlencode
from urllib.request import urlopen

from .config import Config, Game, SteamAccount


log = logging.getLogger(__name__)
OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"


@dataclass(frozen=True)
class Catalog:
    games: tuple[Game, ...]
    accounts_by_app_id: dict[int, SteamAccount]

    def account_for(self, app_id: int) -> SteamAccount:
        account = self.accounts_by_app_id.get(app_id)
        if not account:
            raise RuntimeError(f"No configured Steam account owns app {app_id}")
        return account


def load_catalog(config: Config, opener=urlopen) -> Catalog:
    """Build the usable Workshop list from configured account libraries.

    SteamCMD cannot list a user's licences, so this uses the Steam Web API plus
    the public Store app metadata. A failed scan leaves configured [[games]]
    available, using the first account as the downloader.
    """
    fallback_account = config.steam_accounts[0] if config.steam_accounts else None
    fallback = Catalog(
        games=config.games,
        accounts_by_app_id={game.app_id: fallback_account for game in config.games if fallback_account},
    )
    eligible = [account for account in config.steam_accounts if account.steam_id]
    if not config.steam_web_api_key or not eligible:
        log.warning("Steam catalogue scan skipped: configure steam.web_api_key and steam_id for each account")
        return fallback
    try:
        owners: dict[int, SteamAccount] = {}
        names: dict[int, str] = {}
        for account in eligible:
            query = urlencode({"key": config.steam_web_api_key, "steamid": account.steam_id,
                               "include_appinfo": "1", "include_played_free_games": "1"})
            with opener(f"{OWNED_GAMES_URL}?{query}", timeout=20) as response:
                payload = json.load(response)
            games = payload.get("response", {}).get("games", [])
            log.info("Steam catalogue: account=%s returned %s owned games", account.name, len(games))
            for game in games:
                app_id = int(game.get("appid", 0))
                if app_id:
                    owners.setdefault(app_id, account)
                    names[app_id] = str(game.get("name", f"Steam app {app_id}")).strip()

        workshop_ids: set[int] = set()
        # The Store endpoint documents a single app ID per request. Do not let a
        # missing or delisted title prevent the catalogue for every other game.
        for app_id in owners:
            try:
                query = urlencode({"appids": app_id, "l": "english"})
                with opener(f"{APP_DETAILS_URL}?{query}", timeout=20) as response:
                    details = json.load(response)
                data = details.get(str(app_id), {})
                categories = data.get("data", {}).get("categories", []) if data.get("success") else []
                if any(category.get("description") == "Steam Workshop" for category in categories):
                    workshop_ids.add(app_id)
            except Exception as exc:
                log.warning("Steam catalogue: could not inspect app_id=%s: %s", app_id, exc)
        games = tuple(Game(app_id, names[app_id]) for app_id in sorted(workshop_ids, key=lambda value: names[value].lower()))
        log.info("Steam catalogue scan complete: %s Workshop-enabled titles", len(games))
        return Catalog(games=games, accounts_by_app_id={app_id: owners[app_id] for app_id in workshop_ids})
    except Exception:
        log.exception("Steam catalogue scan failed; using configured [[games]] fallback")
        return fallback
