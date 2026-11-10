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

    def test_expired_block_is_ignored(self) -> None:
        self.database.add_block(BAD, "old", "2000-01-01T00:00:00+00:00")
        self.app.extensions["workshop_ip_guard"].invalidate()
        self.assertEqual(self.client(BAD).get("/").status_code, 200)
        self.assertEqual(self.admin().get("/api/admin/blocks").get_json()["blocks"], [])


if __name__ == "__main__":
    unittest.main()
