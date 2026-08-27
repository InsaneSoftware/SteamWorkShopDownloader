from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from workshop_porter.config import Config, Game
from workshop_porter.database import utc_now
from workshop_porter.web import DownloadLimiter, create_app


def make_config(root: Path, **changes) -> Config:
    config = Config(
        host="127.0.0.1",
        port=8765,
        data_dir=root,
        database_path=root / "jobs.sqlite3",
        archive_dir=root / "archives",
        steamcmd_path=root / "steamcmd",
        steam_content_root=root / "content",
        steam_username="",
        steam_password="",
        steam_guard_code="",
        admin_token="test-token",
        admin_password="test-admin",
        public_base_url="",
        file_ttl_hours=24,
        max_queue_size=10,
        max_archive_gb=25,
        poll_seconds=1.0,
        games=(Game(app_id=107410, name="Arma 3"),),
    )
    return replace(config, **changes)


def ready_job(job_id: str = "job-1") -> dict:
    return {
        "id": job_id,
        "app_id": 107410,
        "game_name": "Arma 3",
        "workshop_id": "843577117",
        "item_name": "Test item",
        "source_url": (
            "https://steamcommunity.com/sharedfiles/filedetails/?id=843577117"
        ),
        "status": "ready",
        "progress": 100,
        "message": "Ready to share",
        "error": None,
        "archive_name": "arma-3_843577117.zip",
        "archive_bytes": 8,
        "download_token": "download-secret",
        "created_at": utc_now(),
        "started_at": None,
        "completed_at": utc_now(),
        "expires_at": None,
    }


class DownloadLimiterTests(unittest.TestCase):
    def test_limits_each_ip_independently(self) -> None:
        limiter = DownloadLimiter(2)

        first = limiter.acquire("192.0.2.10")
        second = limiter.acquire("192.0.2.10")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(limiter.acquire("192.0.2.10"))
        self.assertIsNotNone(limiter.acquire("192.0.2.11"))
        self.assertIsNotNone(limiter.acquire("192.0.2.11"))

        first()
        first()
        self.assertIsNotNone(limiter.acquire("192.0.2.10"))


class DownloadRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def create_ready_app(self, **config_changes):
        config = make_config(self.root, **config_changes)
        app = create_app(config, start_worker=False)
        job = ready_job()
        (config.archive_dir / job["archive_name"]).write_bytes(b"zip-data")
        app.extensions["workshop_database"].create_job(job, 10)
        return app, job

    def test_busy_download_returns_429_without_blocking(self) -> None:
        app, job = self.create_ready_app(max_downloads_per_ip=1)
        limiter = app.extensions["workshop_download_limiter"]
        release = limiter.acquire("127.0.0.1")
        self.assertIsNotNone(release)

        response = app.test_client().get(
            f"/download/{job['id']}?token={job['download_token']}"
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "30")
        release()

    def test_direct_download_releases_slot_when_response_closes(self) -> None:
        app, job = self.create_ready_app(max_downloads_per_ip=1)
        limiter = app.extensions["workshop_download_limiter"]

        response = app.test_client().get(
            f"/download/{job['id']}?token={job['download_token']}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"zip-data")
        release = limiter.acquire("127.0.0.1")
        self.assertIsNotNone(release)
        release()
        response.close()

    def test_trusted_proxy_uses_rightmost_forwarded_ip(self) -> None:
        app, job = self.create_ready_app(
            max_downloads_per_ip=1, trusted_proxy_hops=1
        )
        limiter = app.extensions["workshop_download_limiter"]
        release = limiter.acquire("198.51.100.9")
        self.assertIsNotNone(release)

        response = app.test_client().get(
            f"/download/{job['id']}?token={job['download_token']}",
            headers={"X-Forwarded-For": "203.0.113.7, 198.51.100.9"},
        )

        self.assertEqual(response.status_code, 429)
        release()

    def test_nginx_offload_returns_internal_redirect(self) -> None:
        app, job = self.create_ready_app(
            x_accel_redirect_prefix="/_protected_archives"
        )

        response = app.test_client().get(
            f"/download/{job['id']}?token={job['download_token']}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["X-Accel-Redirect"],
            "/_protected_archives/arma-3_843577117.zip",
        )
        self.assertEqual(response.data, b"")
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")


if __name__ == "__main__":
    unittest.main()
