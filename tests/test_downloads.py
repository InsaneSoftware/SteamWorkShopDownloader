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
    def test_limits_total_downloads_and_connections_per_job(self) -> None:
        limiter = DownloadLimiter(8, 5)

        for _ in range(5):
            self.assertTrue(limiter.acquire("job-1"))
        self.assertFalse(limiter.acquire("job-1"))
        for job_id in ("job-2", "job-3", "job-4"):
            self.assertTrue(limiter.acquire(job_id))
        self.assertFalse(limiter.acquire("job-5"))

        limiter.release("job-1")
        self.assertTrue(limiter.acquire("job-5"))


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
        app, job = self.create_ready_app(
            max_concurrent_downloads=1, max_downloads_per_job=1
        )
        limiter = app.extensions["workshop_download_limiter"]
        self.assertTrue(limiter.acquire(job["id"]))

        response = app.test_client().get(
            f"/download/{job['id']}?token={job['download_token']}"
        )

        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers["Retry-After"], "30")
        limiter.release(job["id"])

    def test_direct_download_releases_slot_when_response_closes(self) -> None:
        app, job = self.create_ready_app(
            max_concurrent_downloads=1, max_downloads_per_job=1
        )
        limiter = app.extensions["workshop_download_limiter"]

        response = app.test_client().get(
            f"/download/{job['id']}?token={job['download_token']}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"zip-data")
        self.assertFalse(limiter.acquire(job["id"]))

        response.close()
        self.assertTrue(limiter.acquire(job["id"]))
        limiter.release(job["id"])

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
