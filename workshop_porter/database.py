from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    app_id INTEGER NOT NULL,
                    game_name TEXT NOT NULL,
                    workshop_id TEXT NOT NULL,
                    item_name TEXT,
                    source_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    error TEXT,
                    archive_name TEXT,
                    archive_bytes INTEGER,
                    download_token TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    expires_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_queue
                    ON jobs(status, created_at);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            if "item_name" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN item_name TEXT")
            connection.commit()

    def create_job(self, job: dict[str, Any], max_queue_size: int) -> tuple[dict[str, Any], bool]:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM jobs
                WHERE app_id = ? AND workshop_id = ?
                  AND status IN ('queued', 'downloading', 'packing')
                ORDER BY created_at LIMIT 1
                """,
                (job["app_id"], job["workshop_id"]),
            ).fetchone()
            if existing:
                connection.commit()
                return dict(existing), False

            queued = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
            ).fetchone()[0]
            if queued >= max_queue_size:
                connection.rollback()
                raise QueueFullError("The queue is full. Try again later.")

            columns = ", ".join(job)
            placeholders = ", ".join("?" for _ in job)
            connection.execute(
                f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
                tuple(job.values()),
            )
            connection.commit()
            return job, True

    def recover_interrupted(self) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'queued', progress = 0,
                    message = 'Recovered after a restart', started_at = NULL
                WHERE status IN ('downloading', 'packing')
                """
            )
            connection.commit()

    def claim_next(self) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if not row:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs
                SET status = 'downloading', progress = 5,
                    message = 'Starting SteamCMD', started_at = ?
                WHERE id = ?
                """,
                (utc_now(), row["id"]),
            )
            connection.commit()
            result = dict(row)
            result.update(status="downloading", progress=5, message="Starting SteamCMD")
            return result

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "status", "progress", "message", "error", "archive_name",
            "archive_bytes", "started_at", "completed_at", "expires_at",
        }
        if not set(fields).issubset(allowed):
            raise ValueError("Unsupported job field")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [job_id]
        with closing(self.connect()) as connection:
            connection.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
            connection.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None

    def delete_job(self, job_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                connection.commit()
                return None
            if row["status"] in {"downloading", "packing"}:
                connection.rollback()
                raise ActiveJobError
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            connection.commit()
            return dict(row)

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def expired_archives(self, now: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT id, archive_name FROM jobs
                WHERE status = 'ready' AND expires_at IS NOT NULL AND expires_at <= ?
                """,
                (now,),
            ).fetchall()
            return [dict(row) for row in rows]


class QueueFullError(RuntimeError):
    pass


class ActiveJobError(RuntimeError):
    pass
