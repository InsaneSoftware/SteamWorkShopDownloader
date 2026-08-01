from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DETAILS_URL = (
    "https://api.steampowered.com/"
    "ISteamRemoteStorage/GetPublishedFileDetails/v1/"
)


@dataclass(frozen=True)
class WorkshopDetails:
    title: str
    consumer_app_id: int


def fetch_workshop_details(
    workshop_id: str,
    timeout: float = 6,
    opener=urlopen,
) -> WorkshopDetails | None:
    body = urlencode(
        {
            "itemcount": "1",
            "publishedfileids[0]": workshop_id,
        }
    ).encode("ascii")
    request = Request(
        DETAILS_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Workshop-Porter/1.0",
        },
    )
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.load(response)
        items = payload.get("response", {}).get("publishedfiledetails", [])
        if not items or int(items[0].get("result", 0)) != 1:
            return None
        item = items[0]
        title = str(item.get("title", "")).replace("\x00", "").strip()
        app_id = int(item.get("consumer_app_id", 0))
        if not title or app_id <= 0:
            return None
        return WorkshopDetails(
            title=title[:200],
            consumer_app_id=app_id,
        )
    except (HTTPError, URLError, TimeoutError, ValueError, TypeError, json.JSONDecodeError):
        return None
