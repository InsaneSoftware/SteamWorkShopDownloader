"""Steam library scanning used by account checks.

The Steam Web API lists the games an account owns (only when the account's
game details are public, or the API key belongs to that account). The public
Store API then tells whether a game has a Steam Workshop. Store answers are
cached in the ``app_catalog`` table, so every app is looked up only once.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlencode
from urllib.request import urlopen


log = logging.getLogger(__name__)
OWNED_GAMES_URL = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v0001/"
APP_DETAILS_URL = "https://store.steampowered.com/api/appdetails"


def fetch_owned_games(api_key: str, steam_id: str, opener=urlopen) -> dict[int, str] | None:
    """Return ``{app_id: name}``, or ``None`` when the library is not visible."""
    if not api_key or not steam_id:
        return None
    query = urlencode({
        "key": api_key,
        "steamid": steam_id,
        "include_appinfo": "1",
        "include_played_free_games": "1",
    })
    try:
        with opener(f"{OWNED_GAMES_URL}?{query}", timeout=20) as response:
            payload = json.load(response)
    except Exception as exc:
        log.warning("Steam library scan failed for %s: %s", steam_id, exc)
        return None
    data = payload.get("response", {})
    if "games" not in data:
        # Private game details return an empty response object.
        return None
    games: dict[int, str] = {}
    for game in data.get("games", []):
        app_id = int(game.get("appid", 0))
        if app_id > 0:
            games[app_id] = str(game.get("name") or f"Steam app {app_id}").strip()[:200]
    return games


def has_workshop(app_id: int, opener=urlopen) -> bool | None:
    """True/False from the Store categories, ``None`` when Steam did not answer."""
    query = urlencode({"appids": app_id, "l": "english"})
    try:
        with opener(f"{APP_DETAILS_URL}?{query}", timeout=20) as response:
            details = json.load(response)
    except Exception as exc:
        log.warning("Steam catalogue: could not inspect app_id=%s: %s", app_id, exc)
        return None
    data = (details or {}).get(str(app_id), {})
    if not data.get("success"):
        return False
    categories = data.get("data", {}).get("categories", [])
    return any(category.get("description") == "Steam Workshop" for category in categories)
