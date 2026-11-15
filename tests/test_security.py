from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workshop_porter.web import create_app

from tests.test_accounts import FETCH, JOB_URL, TOKEN, FakeSteam
from tests.test_downloads import make_config, ready_job


BAD = "203.0.113.7"
GOOD = "198.51.100.20"


class ActivityAndBlockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.app = create_app(
            make_config(self.root), start_worker=False,
            metadata_fetcher=lambda _: None, steamcmd=FakeSteam(),
        )
        self.database = self.app.extensions["workshop_database"]

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def client(self, ip: str, admin: bool = False):
        client = self.app.test_client()
        client.environ_base["REMOTE_ADDR"] = ip
        if admin:
            client.get("/?admin=test-admin")
        return client

    def admin(self):
        return self.client(GOOD, admin=True)

    def test_actions_are_logged_per_ip(self) -> None:
        visitor = self.client(BAD)
        # A working account so job requests are accepted.
        visitor.post("/api/accounts", headers=TOKEN, json={
            "display_name": "Donor", "username": "donor_l", "password": "pw"})
        pool = self.app.extensions["workshop_accounts"]
        while pool.check_due_account():
            pass

        other_item = JOB_URL.replace("843577117", "111222333")
        self.assertEqual(visitor.post("/api/jobs", headers=TOKEN, json={
            "app_id": 107410, "url": other_item}).status_code, 201)
        visitor.post("/api/jobs", headers=TOKEN, json={"app_id": 1, "url": other_item})

        job = ready_job()
        (self.root / "archives" / job["archive_name"]).write_bytes(b"zip-data")
        self.database.create_job(job, 10)
        url = f"/download/{job['id']}?token={job['download_token']}"
        response = visitor.get(url)
        response.close()
        # Resumed transfer: only the first part counts.
        visitor.get(url, headers={"Range": "bytes=4-"}).close()

        # Wrong password while polling: logged once, not on every poll.
        for _ in range(5):
            self.assertEqual(visitor.get("/api/jobs", headers={
                "Authorization": "Bearer nope"}).status_code, 401)

        data = self.admin().get("/api/admin/activity?hours=24").get_json()
        row = next(r for r in data["ips"] if r["ip"] == BAD)
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["rejected"], 1)
        self.assertEqual(row["downloads"], 1)
        self.assertEqual(row["bytes"], 8)
        self.assertEqual(row["shares"], 1)
        self.assertEqual(row["bad_passwords"], 1)

        only_bad = self.admin().get(f"/api/admin/activity?ip={BAD}").get_json()["events"]
        self.assertTrue(only_bad and all(e["ip"] == BAD for e in only_bad))
        self.assertIn("Arma 3", next(e for e in only_bad if e["action"] == "download")["detail"])

    def test_blocking_and_unblocking(self) -> None:
        admin = self.admin()
        url = "/api/admin/blocks"
        self.assertEqual(self.client(BAD).post(url, json={"target": BAD}).status_code, 403)
        self.assertEqual(admin.post(url, json={"target": BAD}).status_code, 400)  # no fetch header

        for refused in ("127.0.0.1", "10.0.0.0/8", "192.168.1.5", "not-an-ip", "1.0.0.0/8"):
            response = admin.post(url, headers=FETCH, json={"target": refused})
            self.assertEqual(response.status_code, 400, refused)

        response = admin.post(url, headers=FETCH, json={
            "target": BAD, "hours": 2, "reason": "spam"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.get_json()["expires_at"])

        blocked = self.client(BAD)
        response = blocked.get("/api/jobs", headers=TOKEN)
        self.assertEqual(response.status_code, 403)
        self.assertIn("blocked until", response.get_json()["error"])
        self.assertEqual(blocked.get("/").status_code, 403)
        self.assertEqual(blocked.get("/static/app.css").status_code, 200)
        self.assertEqual(self.client(GOOD).get("/api/jobs", headers=TOKEN).status_code, 200)

        # The admin is never locked out, even from a blocked address.
        admin_on_bad_ip = self.client(BAD, admin=True)
        self.assertEqual(admin_on_bad_ip.get("/api/admin/blocks").status_code, 200)

        # The blocked attempt shows up in the log.
        events = admin.get(f"/api/admin/activity?ip={BAD}").get_json()["events"]
        self.assertTrue(any(e["action"] == "blocked" for e in events))

        self.assertEqual(admin.post(f"{url}/remove", headers=FETCH,
                                    json={"network": BAD}).status_code, 200)
        self.assertEqual(self.client(BAD).get("/api/jobs", headers=TOKEN).status_code, 200)

    def test_permanent_range_block(self) -> None:
        admin = self.admin()
        response = admin.post("/api/admin/blocks", headers=FETCH,
                              json={"target": "203.0.113.99/24"})
        self.assertEqual(response.get_json()["network"], "203.0.113.0/24")
        self.assertIsNone(response.get_json()["expires_at"])
        self.assertEqual(self.client("203.0.113.200").get("/").status_code, 403)
        self.assertEqual(self.client("203.0.114.1").get("/").status_code, 200)
        blocks = admin.get("/api/admin/blocks").get_json()["blocks"]
        self.assertEqual([b["network"] for b in blocks], ["203.0.113.0/24"])

    def test_user_agents_are_logged_and_blockable(self) -> None:
        bot = "EvilScraper/2.1 (+proxy)"
        visitor = self.client(BAD)
        visitor.environ_base["HTTP_USER_AGENT"] = bot
        visitor.get("/api/jobs", headers={"Authorization": "Bearer nope"})
        other = self.client("198.51.100.77")
        other.environ_base["HTTP_USER_AGENT"] = bot
        other.get("/api/jobs", headers={"Authorization": "Bearer nope"})

        admin = self.admin()
        data = admin.get("/api/admin/activity").get_json()
        row = next(r for r in data["user_agents"] if r["user_agent"] == bot)
        self.assertEqual((row["ips"], row["bad_passwords"]), (2, 2))
        events = admin.get("/api/admin/activity", query_string={"ua": bot}).get_json()["events"]
        self.assertEqual({e["ip"] for e in events}, {BAD, "198.51.100.77"})

        url = "/api/admin/ua-blocks"
        for pattern, match in (("Mozilla", "contains"), ("abc", "contains"), ("x", "regex")):
            self.assertEqual(admin.post(url, headers=FETCH, json={
                "pattern": pattern, "match": match}).status_code, 400, pattern)
        response = admin.post(url, headers=FETCH, json={
            "pattern": "evilscraper", "match": "contains", "hours": 1, "reason": "proxies"})
        self.assertEqual(response.status_code, 200)

        # Any IP with that browser is blocked. Others and the admin are not.
        new_ip = self.client("192.0.2.123")
        new_ip.environ_base["HTTP_USER_AGENT"] = bot
        response = new_ip.get("/api/jobs", headers=TOKEN)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Your browser is blocked", response.get_json()["error"])
        self.assertEqual(self.client("192.0.2.124").get("/api/jobs", headers=TOKEN).status_code, 200)
        admin.environ_base["HTTP_USER_AGENT"] = bot
        self.assertEqual(admin.get("/api/admin/blocks").status_code, 200)

        blocks = admin.get("/api/admin/blocks").get_json()["ua_blocks"]
        self.assertEqual([(b["pattern"], b["match"]) for b in blocks], [("evilscraper", "contains")])
        self.assertEqual(admin.post(f"{url}/remove", headers=FETCH, json={
            "pattern": "evilscraper", "match": "contains"}).status_code, 200)
        self.assertEqual(new_ip.get("/api/jobs", headers=TOKEN).status_code, 200)

        # An exact empty pattern blocks requests that send no User-Agent at all.
        admin.post(url, headers=FETCH, json={"pattern": "", "match": "exact"})
        no_agent = self.client("192.0.2.125")
        no_agent.environ_base["HTTP_USER_AGENT"] = ""
        self.assertEqual(no_agent.get("/").status_code, 403)
        self.assertEqual(self.client("192.0.2.126").get("/").status_code, 200)

    def test_five_different_wrong_passwords_block_for_a_day(self) -> None:
        guesser = self.client(BAD)
        for attempt in ["a1", "b2", "c3", "d4"]:
            self.assertEqual(guesser.get("/api/jobs", headers={
                "Authorization": f"Bearer {attempt}"}).status_code, 401)
        # Repeating a password (a stale saved one while polling) does not count.
        for _ in range(10):
            guesser.get("/api/jobs", headers={"Authorization": "Bearer d4"})
        self.assertEqual(guesser.get("/api/jobs", headers=TOKEN).status_code, 200)

        fifth = guesser.get("/api/jobs", headers={"Authorization": "Bearer e5"})
        self.assertEqual(fifth.status_code, 403)
        self.assertTrue(fifth.get_json()["blocked"])
        self.assertIn("Anti spam", fifth.get_json()["error"])
        later = guesser.get("/api/jobs", headers=TOKEN)
        self.assertEqual(later.status_code, 403)
        self.assertIn("Anti spam: 5 wrong passwords", later.get_json()["error"])
        blocks = self.admin().get("/api/admin/blocks").get_json()["blocks"]
        block = next(b for b in blocks if b["network"].startswith(BAD))
        self.assertIn("wrong passwords", block["reason"])
        self.assertIsNotNone(block["expires_at"])
        events = self.admin().get(f"/api/admin/activity?ip={BAD}").get_json()["events"]
        self.assertTrue(any(e["action"] == "auto_block" for e in events))

        # Wrong admin passwords count too, and local addresses are never blocked.
        local = self.client("127.0.0.1")
        for attempt in range(6):
            local.get(f"/?admin=wrong{attempt}")
        self.assertEqual(local.get("/api/jobs", headers=TOKEN).status_code, 200)
        other = self.client("192.0.2.99")
        for attempt in range(4):
            other.get(f"/?admin=wrong{attempt}")
        self.assertIn(b"Anti spam", other.get("/?admin=wrong4").data)
        self.assertEqual(other.get("/api/jobs", headers=TOKEN).status_code, 403)

    def test_more_than_25_downloads_a_day_blocks_the_ip(self) -> None:
        archives = self.root / "archives"
        archives.mkdir(exist_ok=True)
        jobs = []
        for number in range(27):
            job = ready_job(f"job-{number}")
            job["workshop_id"] = str(1000 + number)
            job["archive_name"] = f"item_{number}.zip"
            job["download_token"] = f"t{number}"
            (archives / job["archive_name"]).write_bytes(b"zip-data")
            self.database.create_job(job, 100)
            jobs.append(job)

        def fetch(client, job):
            response = client.get(f"/download/{job['id']}?token={job['download_token']}")
            response.close()
            return response.status_code

        visitor = self.client(BAD)
        # Clicking the same file many times counts once.
        for _ in range(5):
            self.assertEqual(fetch(visitor, jobs[0]), 200)
        for job in jobs[1:25]:
            self.assertEqual(fetch(visitor, job), 200)
        data = self.admin().get(f"/api/admin/activity?ip={BAD}").get_json()
        self.assertEqual(next(r for r in data["ips"] if r["ip"] == BAD)["downloads"], 25)
        # A file already downloaded still works, the 26th new one blocks.
        self.assertEqual(fetch(visitor, jobs[3]), 200)
        self.assertEqual(fetch(visitor, jobs[25]), 403)
        self.assertEqual(visitor.get("/api/jobs", headers=TOKEN).status_code, 403)
        blocks = self.admin().get("/api/admin/blocks").get_json()["blocks"]
        self.assertIn("downloads", next(b for b in blocks if b["network"].startswith(BAD))["reason"])
        # Other visitors are not affected.
        self.assertEqual(fetch(self.client("192.0.2.80"), jobs[26]), 200)

    def test_finished_item_is_reused_instead_of_downloaded_again(self) -> None:
        visitor = self.client(BAD)
        visitor.post("/api/accounts", headers=TOKEN, json={
            "display_name": "Donor", "username": "donor_l", "password": "pw"})
        pool = self.app.extensions["workshop_accounts"]
        while pool.check_due_account():
            pass
        job = ready_job()
        (self.root / "archives").mkdir(exist_ok=True)
        (self.root / "archives" / job["archive_name"]).write_bytes(b"zip-data")
        self.database.create_job(job, 10)
        response = visitor.post("/api/jobs", headers=TOKEN, json={
            "app_id": 107410, "url": JOB_URL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["job"]["id"], job["id"])
        # Without the archive on disk a new job is queued.
        (self.root / "archives" / job["archive_name"]).unlink()
        response = visitor.post("/api/jobs", headers=TOKEN, json={
            "app_id": 107410, "url": JOB_URL})
        self.assertEqual(response.status_code, 201)

    def test_expired_block_is_ignored(self) -> None:
        self.database.add_block(BAD, "old", "2000-01-01T00:00:00+00:00")
        self.app.extensions["workshop_ip_guard"].invalidate()
        self.assertEqual(self.client(BAD).get("/").status_code, 200)
        self.assertEqual(self.admin().get("/api/admin/blocks").get_json()["blocks"], [])


if __name__ == "__main__":
    unittest.main()
