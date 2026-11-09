from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from workshop_porter.config import Game, SteamAccount
from workshop_porter.steamcmd import (
    AccountCheck,
    classify_login_failure,
    parse_owned_apps,
    parse_steam_id,
)
from workshop_porter.web import create_app

from tests.test_downloads import make_config


TOKEN = {"Authorization": "Bearer test-token"}
FETCH = {"X-Requested-With": "fetch"}
JOB_URL = "https://steamcommunity.com/sharedfiles/filedetails/?id=843577117"


class FakeSteam:
    """Stands in for SteamCMD; results are keyed by Steam login."""

    def __init__(self):
        self.results: dict[str, AccountCheck] = {}
        self.calls: list[tuple[SteamAccount, list[int]]] = []

    def check_account(self, account, app_ids):
        self.calls.append((account, app_ids))
        return self.results.get(account.username, AccountCheck(True, [107410]))


class FakeSteamApi:
    """Answers GetOwnedGames and Store appdetails requests."""

    def __init__(self, libraries: dict[str, list[tuple[int, str]]], workshop: set[int]):
        self.libraries = libraries
        self.workshop = workshop
        self.store_requests = 0

    def __call__(self, url, timeout=None):
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        if "GetOwnedGames" in parsed.path:
            games = self.libraries.get(query["steamid"][0])
            body = {"response": {} if games is None else {
                "games": [{"appid": a, "name": n} for a, n in games]}}
        else:
            self.store_requests += 1
            app_id = int(query["appids"][0])
            categories = [{"description": "Steam Workshop"}] if app_id in self.workshop else []
            body = {str(app_id): {"success": True, "data": {"categories": categories}}}
        return io.BytesIO(json.dumps(body).encode())


class AccountPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.steam = FakeSteam()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def make_app(self, **changes):
        changes.setdefault("games", (Game(107410, "Arma 3"), Game(227300, "ETS 2")))
        api = changes.pop("api", None)
        app = create_app(
            make_config(self.root, **changes),
            start_worker=False,
            metadata_fetcher=lambda _: None,
            steamcmd=self.steam,
            steam_api_opener=api,
        )
        return app, app.test_client(), app.extensions["workshop_accounts"]

    def submit(self, client, name, login, password="pw", guard=""):
        return client.post("/api/accounts", headers=TOKEN, json={
            "display_name": name, "username": login,
            "password": password, "guard_code": guard,
        })

    @staticmethod
    def drain(pool):
        while pool.check_due_account():
            pass

    def test_submission_validation_and_privacy(self) -> None:
        app, client, pool = self.make_app()
        self.assertEqual(client.get("/api/accounts").status_code, 401)

        response = self.submit(client, "Bob", "bob_login", "secret1", "AB12C")
        self.assertEqual(response.status_code, 201)
        self.assertNotIn("bob_login", response.get_data(as_text=True))
        account_id = response.get_json()["account"]["id"]

        # Same login (case-insensitive) cannot be submitted while not broken.
        self.assertEqual(self.submit(client, "Evil", "BOB_LOGIN", "x").status_code, 409)
        self.assertEqual(self.submit(client, "x", "a", "b").status_code, 400)

        self.steam.results["bob_login"] = AccountCheck(True, [107410, 227300])
        self.drain(pool)
        self.assertEqual(self.steam.calls[0][0].guard_code, "AB12C")
        database = app.extensions["workshop_database"]
        self.assertIsNone(database.get_account(account_id)["guard_code_enc"])

        account = client.get(f"/api/accounts/{account_id}", headers=TOKEN).get_json()["account"]
        self.assertEqual(account["status"], "working")
        self.assertEqual({g["app_id"] for g in account["games"]}, {107410, 227300})

        listing = client.get("/api/accounts", headers=TOKEN).get_data(as_text=True)
        for secret in ("bob_login", "secret1", "username", "password"):
            self.assertNotIn(secret, listing)

        raw = (self.root / "jobs.sqlite3").read_bytes()
        self.assertNotIn(b"bob_login", raw)
        self.assertNotIn(b"secret1", raw)

    def test_jobs_require_a_working_owner(self) -> None:
        _, client, pool = self.make_app()
        job = {"app_id": 107410, "url": JOB_URL}
        response = client.post("/api/jobs", headers=TOKEN, json=job)
        self.assertEqual(response.status_code, 400)
        self.assertIn("No working Steam account", response.get_json()["error"])

        self.submit(client, "Owner", "owner_l")
        self.drain(pool)
        self.assertEqual(client.post("/api/jobs", headers=TOKEN, json=job).status_code, 201)

    def test_failures_flag_accounts(self) -> None:
        _, client, pool = self.make_app()
        account_id = self.submit(client, "Bob", "bob_l").get_json()["account"]["id"]
        self.drain(pool)

        def status():
            return client.get(f"/api/accounts/{account_id}", headers=TOKEN).get_json()["account"]

        # Transient failures need three in a row.
        self.steam.results["bob_l"] = AccountCheck(False, reason="Steam rate limit reached", transient=True)
        for expected in ("working", "working", "broken"):
            pool.request_recheck(account_id)
            pool.check_due_account()
            self.assertEqual(status()["status"], expected)

        # A broken account can be resubmitted with a corrected password.
        self.steam.results["bob_l"] = AccountCheck(True, [227300])
        response = self.submit(client, "Bob", "bob_l", "new-password")
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.get_json()["created"])
        pool.check_due_account()
        self.assertEqual(status()["status"], "working")

        # Wrong password flags immediately.
        self.steam.results["bob_l"] = AccountCheck(False, reason="Wrong login or password")
        pool.request_recheck(account_id)
        pool.check_due_account()
        self.assertEqual(status()["status"], "broken")
        self.assertEqual(status()["status_reason"], "Wrong login or password")

        self.steam.results["empty_l"] = AccountCheck(True, [])
        self.submit(client, "Empty", "empty_l")
        self.drain(pool)
        empty = next(a for a in client.get("/api/accounts", headers=TOKEN).get_json()["accounts"]
                     if a["display_name"] == "Empty")
        self.assertEqual(empty["status"], "broken")

    def test_config_accounts_are_imported_once(self) -> None:
        accounts = (
            SteamAccount("main", "cfg_login", "cfg_pw", "", "76561197960265729"),
            SteamAccount("x", "your-steam-login", "placeholder", "", ""),
        )
        app, client, _ = self.make_app(steam_accounts=accounts)
        create_app(app.extensions["workshop_config"], start_worker=False, steamcmd=self.steam)
        names = [a["display_name"] for a in
                 client.get("/api/accounts", headers=TOKEN).get_json()["accounts"]]
        self.assertEqual(names, ["main"])

    def test_library_scan_adds_workshop_games(self) -> None:
        api = FakeSteamApi(
            {"76561197960265800": [(107410, "Arma 3"), (4000, "Garry's Mod"), (10, "No Workshop")]},
            workshop={107410, 4000},
        )
        _, client, pool = self.make_app(steam_web_api_key="key", api=api)
        self.steam.results["lib"] = AccountCheck(True, [], steam_id="76561197960265800")
        account_id = self.submit(client, "Library", "lib").get_json()["account"]["id"]
        self.drain(pool)

        account = client.get(f"/api/accounts/{account_id}", headers=TOKEN).get_json()["account"]
        self.assertEqual(account["status"], "working")
        self.assertEqual([g["name"] for g in account["games"]], ["Arma 3", "Garry's Mod"])
        games = {g["app_id"]: g["accounts"] for g in
                 client.get("/api/games", headers=TOKEN).get_json()["games"]}
        self.assertEqual(games, {107410: 1, 227300: 0, 4000: 1})

        # Store answers are cached: a recheck does not query them again.
        lookups = api.store_requests
        pool.request_recheck(account_id)
        pool.check_due_account()
        self.assertEqual(api.store_requests, lookups)

    def test_private_library_is_probed_with_known_workshop_games(self) -> None:
        api = FakeSteamApi({"1": [(4000, "Garry's Mod")]}, workshop={4000})
        _, client, pool = self.make_app(steam_web_api_key="key", api=api)
        self.steam.results["public_l"] = AccountCheck(True, [], steam_id="1")
        self.submit(client, "Public", "public_l")
        self.drain(pool)

        self.steam.results["private_l"] = AccountCheck(True, [4000], steam_id="2")
        self.submit(client, "Private", "private_l")
        self.drain(pool)
        probed = self.steam.calls[-1][1]
        self.assertIn(4000, probed)
        private = next(a for a in client.get("/api/accounts", headers=TOKEN).get_json()["accounts"]
                       if a["display_name"] == "Private")
        self.assertEqual(private["status"], "working")

    def test_admin_routes(self) -> None:
        _, client, pool = self.make_app()
        account_id = self.submit(client, "Bob", "bob_login").get_json()["account"]["id"]
        self.drain(pool)

        self.assertEqual(client.get("/api/admin/accounts").status_code, 403)
        self.assertEqual(client.get("/admin").status_code, 302)
        client.get("/?admin=test-admin")
        self.assertEqual(client.get("/admin").status_code, 200)
        data = client.get("/api/admin/accounts").get_json()
        self.assertEqual(data["accounts"][0]["username"], "bob_login")
        self.assertNotIn("_enc", json.dumps(data))

        url = f"/api/admin/accounts/{account_id}"
        self.assertEqual(client.post(f"{url}/enabled", json={"enabled": False}).status_code, 400)
        self.assertEqual(
            client.post(f"{url}/enabled", headers=FETCH, json={"enabled": False}).status_code, 200
        )
        status = client.get(f"/api/accounts/{account_id}", headers=TOKEN).get_json()["account"]["status"]
        self.assertEqual(status, "disabled")
        self.assertEqual(client.post(f"{url}/recheck", headers=FETCH).status_code, 200)
        # Rename: validated, cannot expose the login, needs the fetch header.
        self.assertEqual(
            client.post(f"{url}/name", json={"display_name": "New Name"}).status_code, 400
        )
        for bad in ("x", "BOB_LOGIN", "<script>"):
            response = client.post(f"{url}/name", headers=FETCH, json={"display_name": bad})
            self.assertEqual(response.status_code, 400, bad)
        response = client.post(f"{url}/name", headers=FETCH, json={"display_name": "  New   Name "})
        self.assertEqual(response.get_json()["display_name"], "New Name")
        public = client.get(f"/api/accounts/{account_id}", headers=TOKEN).get_json()["account"]
        self.assertEqual(public["display_name"], "New Name")
        self.assertEqual(
            client.post("/api/admin/accounts/missing/name", headers=FETCH,
                        json={"display_name": "Valid"}).status_code, 404
        )

        self.assertEqual(client.delete(url, headers=FETCH).status_code, 200)
        self.assertEqual(client.get(f"/api/accounts/{account_id}", headers=TOKEN).status_code, 404)


class SteamCmdParsingTests(unittest.TestCase):
    def test_app_status(self) -> None:
        lines = [
            "AppID 440 (Team Fortress 2):",
            "- release state: released (Subscribed,Permanent,)",
            "AppID 227300 ():",
            "- release state: unknown (No License)",
        ]
        self.assertEqual(parse_owned_apps(lines), [440])

    def test_steam_id(self) -> None:
        self.assertEqual(
            parse_steam_id(["Logging in user '[redacted]' [U:1:42] to Steam Public...OK"]),
            str(76561197960265728 + 42),
        )
        self.assertEqual(parse_steam_id(["Logging in user '[redacted]' [U:1:0] ..."]), "")

    def test_login_failures(self) -> None:
        self.assertEqual(
            classify_login_failure(["Logging in ...ERROR (Invalid Password)"], False),
            ("Wrong login or password", False),
        )
        self.assertTrue(classify_login_failure(["FAILED (Rate Limit Exceeded)"], False)[1])
        self.assertFalse(classify_login_failure(
            ["Please confirm the login in the Steam Mobile app on your phone."], True)[1])


if __name__ == "__main__":
    unittest.main()
