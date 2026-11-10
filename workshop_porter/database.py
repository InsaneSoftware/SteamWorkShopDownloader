from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
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

                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    username_hash TEXT NOT NULL UNIQUE,
                    username_enc TEXT NOT NULL,
                    password_enc TEXT NOT NULL,
                    guard_code_enc TEXT,
                    status TEXT NOT NULL,
                    status_reason TEXT NOT NULL DEFAULT '',
                    last_error TEXT,
                    owned_app_ids TEXT NOT NULL DEFAULT '[]',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_checked_at TEXT,
                    last_ok_at TEXT,
                    broken_since TEXT,
                    next_check_at TEXT NOT NULL,
                    last_used_at TEXT,
                    steam_id TEXT,
                    library_visible INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_accounts_due
                    ON accounts(enabled, next_check_at);

                -- Steam apps seen in account libraries, with a cached answer
                -- to "does this game have a Steam Workshop?".
                CREATE TABLE IF NOT EXISTS app_catalog (
                    app_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    workshop INTEGER,
                    checked_at TEXT
                );

                -- Full app lists of Steam licences (packages), from
                -- package_info_print. licenses_print cuts long lists short.
                CREATE TABLE IF NOT EXISTS package_apps (
                    package_id INTEGER PRIMARY KEY,
                    app_ids TEXT NOT NULL,
                    checked_at TEXT NOT NULL
                );

                -- Admin choices to hide or show a game in the public list.
                CREATE TABLE IF NOT EXISTS game_visibility (
                    app_id INTEGER PRIMARY KEY,
                    hidden INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                );

                -- Who did what, by client IP. Kept for 90 days.
                CREATE TABLE IF NOT EXISTS activity_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    bytes INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_activity_at ON activity_log(at);
                CREATE INDEX IF NOT EXISTS idx_activity_ip ON activity_log(ip, at);

                -- Blocked IPs or ranges (CIDR). expires_at NULL means permanent.
                CREATE TABLE IF NOT EXISTS ip_blocks (
                    network TEXT PRIMARY KEY,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    expires_at TEXT
                );

                -- config.toml logins already imported into the pool. They are
                -- never imported again, so renames and deletions in the admin
                -- dashboard survive restarts.
                CREATE TABLE IF NOT EXISTS config_imports (
                    username_hash TEXT PRIMARY KEY,
                    imported_at TEXT NOT NULL
                );
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


    # ----- Community Steam accounts -------------------------------------

    ACCOUNT_FIELDS = {
        "display_name", "username_enc", "password_enc", "guard_code_enc",
        "status", "status_reason", "last_error", "owned_app_ids", "enabled",
        "consecutive_failures", "last_checked_at", "last_ok_at",
        "broken_since", "next_check_at", "last_used_at", "steam_id",
        "library_visible",
    }

    @staticmethod
    def _account(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        account = dict(row)
        account["owned_app_ids"] = json.loads(account["owned_app_ids"] or "[]")
        account["enabled"] = bool(account["enabled"])
        return account

    def create_account(
        self, account: dict[str, Any], max_pending: int
    ) -> tuple[dict[str, Any], bool]:
        """Insert an account, or replace the credentials of a broken one.

        Returns ``(account, created)``. Raises ``DuplicateAccountError`` when
        the login is already in the pool and not broken, so nobody can
        overwrite a working account's password.
        """
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM accounts WHERE username_hash = ?",
                (account["username_hash"],),
            ).fetchone()
            if existing and existing["status"] != "broken":
                connection.rollback()
                raise DuplicateAccountError(existing["status"])

            pending = connection.execute(
                "SELECT COUNT(*) FROM accounts WHERE status = 'pending'"
            ).fetchone()[0]
            if pending >= max_pending:
                connection.rollback()
                raise QueueFullError(
                    "Too many accounts are waiting for validation. Try again later."
                )

            if existing:
                connection.execute(
                    """
                    UPDATE accounts
                    SET display_name = ?, username_enc = ?, password_enc = ?,
                        guard_code_enc = ?, status = 'pending',
                        status_reason = 'Waiting for validation', last_error = NULL,
                        consecutive_failures = 0, enabled = 1, next_check_at = ?
                    WHERE id = ?
                    """,
                    (
                        account["display_name"], account["username_enc"],
                        account["password_enc"], account["guard_code_enc"],
                        account["next_check_at"], existing["id"],
                    ),
                )
                account_id = existing["id"]
                created = False
            else:
                columns = ", ".join(account)
                placeholders = ", ".join("?" for _ in account)
                connection.execute(
                    f"INSERT INTO accounts ({columns}) VALUES ({placeholders})",
                    tuple(account.values()),
                )
                account_id = account["id"]
                created = True
            row = connection.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            connection.commit()
            return self._account(row), created

    def account_exists(self, username_hash: str) -> bool:
        with closing(self.connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM accounts WHERE username_hash = ?", (username_hash,)
            ).fetchone() is not None

    def config_import_done(self, username_hash: str) -> bool:
        with closing(self.connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM config_imports WHERE username_hash = ?", (username_hash,)
            ).fetchone() is not None

    def mark_config_imported(self, username_hash: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO config_imports (username_hash, imported_at) "
                "VALUES (?, ?)",
                (username_hash, utc_now()),
            )
            connection.commit()

    def get_account(self, account_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            return self._account(
                connection.execute(
                    "SELECT * FROM accounts WHERE id = ?", (account_id,)
                ).fetchone()
            )

    def list_accounts(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM accounts
                ORDER BY CASE
                    WHEN enabled = 0 THEN 3
                    WHEN status = 'working' THEN 0
                    WHEN status = 'pending' THEN 1
                    ELSE 2 END,
                    display_name COLLATE NOCASE
                """
            ).fetchall()
            return [self._account(row) for row in rows]

    def next_due_account(self, now: str) -> dict[str, Any] | None:
        """Accounts with a fresh Steam Guard code first (codes expire fast),
        then other pending validations, then the most overdue periodic check."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM accounts
                WHERE enabled = 1 AND next_check_at <= ?
                ORDER BY CASE
                    WHEN guard_code_enc IS NOT NULL THEN 0
                    WHEN status = 'pending' THEN 1
                    ELSE 2 END,
                    next_check_at
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            return self._account(row)

    def update_account(self, account_id: str, **fields: Any) -> None:
        if not fields:
            return
        if not set(fields).issubset(self.ACCOUNT_FIELDS):
            raise ValueError("Unsupported account field")
        if "owned_app_ids" in fields:
            fields["owned_app_ids"] = json.dumps(sorted(set(fields["owned_app_ids"])))
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with closing(self.connect()) as connection:
            connection.execute(
                f"UPDATE accounts SET {assignments} WHERE id = ?",
                [*fields.values(), account_id],
            )
            connection.commit()

    def delete_account(self, account_id: str) -> bool:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM accounts WHERE id = ?", (account_id,)
            )
            connection.commit()
            return cursor.rowcount > 0

    def working_accounts_for_app(self, app_id: int) -> list[dict[str, Any]]:
        """Working accounts that own ``app_id``, least recently used first."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM accounts
                WHERE enabled = 1 AND status = 'working'
                ORDER BY COALESCE(last_used_at, ''), created_at
                """
            ).fetchall()
        accounts = [self._account(row) for row in rows]
        return [a for a in accounts if app_id in a["owned_app_ids"]]


    # ----- Steam app catalogue ------------------------------------------

    def remember_apps(self, names: dict[int, str]) -> None:
        if not names:
            return
        with closing(self.connect()) as connection:
            connection.executemany(
                """
                INSERT INTO app_catalog (app_id, name) VALUES (?, ?)
                ON CONFLICT(app_id) DO UPDATE SET name = excluded.name
                """,
                list(names.items()),
            )
            connection.commit()

    def set_app_workshop(self, app_id: int, workshop: bool | None) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                "UPDATE app_catalog SET workshop = ?, checked_at = ? WHERE app_id = ?",
                (None if workshop is None else int(workshop), utc_now(), app_id),
            )
            connection.commit()

    def app_catalog(self, app_ids: list[int] | None = None) -> dict[int, dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM app_catalog").fetchall()
        result = {row["app_id"]: dict(row) for row in rows}
        if app_ids is not None:
            wanted = set(app_ids)
            result = {key: value for key, value in result.items() if key in wanted}
        return result

    def save_app_info(self, apps: dict[int, tuple[str, bool]]) -> None:
        """Store ``{app_id: (name, has_workshop)}`` from SteamCMD app info."""
        if not apps:
            return
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.executemany(
                """
                INSERT INTO app_catalog (app_id, name, workshop, checked_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    name = excluded.name, workshop = excluded.workshop,
                    checked_at = excluded.checked_at
                """,
                [(a, name, int(workshop), now) for a, (name, workshop) in apps.items()],
            )
            connection.commit()

    def save_packages(self, packages: dict[int, list[int]]) -> None:
        if not packages:
            return
        now = utc_now()
        with closing(self.connect()) as connection:
            connection.executemany(
                """
                INSERT INTO package_apps (package_id, app_ids, checked_at)
                VALUES (?, ?, ?)
                ON CONFLICT(package_id) DO UPDATE SET
                    app_ids = excluded.app_ids, checked_at = excluded.checked_at
                """,
                [(p, json.dumps(sorted(set(apps))), now) for p, apps in packages.items()],
            )
            connection.commit()

    def package_apps(self, package_ids: list[int], max_age_days: int = 30) -> dict[int, list[int]]:
        """Cached app lists, ignoring entries older than ``max_age_days``
        (bundles can gain apps)."""
        if not package_ids:
            return {}
        cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT package_id, app_ids FROM package_apps WHERE checked_at >= ?",
                (cutoff,),
            ).fetchall()
        wanted = set(package_ids)
        return {
            row["package_id"]: json.loads(row["app_ids"])
            for row in rows if row["package_id"] in wanted
        }

    def workshop_app_ids(self) -> list[int]:
        with closing(self.connect()) as connection:
            return [
                row[0] for row in connection.execute(
                    "SELECT app_id FROM app_catalog WHERE workshop = 1"
                ).fetchall()
            ]


    # ----- Game visibility ----------------------------------------------

    def game_visibility(self) -> dict[int, bool]:
        """``{app_id: hidden}`` for games the admin hid or showed explicitly."""
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT app_id, hidden FROM game_visibility").fetchall()
        return {row["app_id"]: bool(row["hidden"]) for row in rows}

    def set_game_visibility(self, app_id: int, hidden: bool) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO game_visibility (app_id, hidden, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(app_id) DO UPDATE SET
                    hidden = excluded.hidden, updated_at = excluded.updated_at
                """,
                (app_id, int(hidden), utc_now()),
            )
            connection.commit()

    # ----- Activity log and IP blocks -----------------------------------

    def log_activity(
        self, ip: str, action: str, status: str, detail: str = "", bytes_: int | None = None
    ) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO activity_log (at, ip, action, status, detail, bytes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (utc_now(), ip, action, status, detail[:300], bytes_),
            )
            connection.commit()

    def recent_activity(
        self, since: str, ip: str | None = None, limit: int = 300
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM activity_log WHERE at >= ?"
        params: list[Any] = [since]
        if ip:
            query += " AND ip = ?"
            params.append(ip)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with closing(self.connect()) as connection:
            return [dict(row) for row in connection.execute(query, params).fetchall()]

    def activity_by_ip(self, since: str, limit: int = 100) -> list[dict[str, Any]]:
        """Per-IP totals, busiest first."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT ip,
                    COUNT(*) AS events,
                    SUM(action = 'request' AND status = 'queued') AS requests,
                    SUM(action = 'request' AND status != 'queued') AS rejected,
                    SUM(action = 'download' AND status = 'ok') AS downloads,
                    COALESCE(SUM(CASE WHEN action = 'download' AND status = 'ok'
                                      THEN bytes END), 0) AS bytes,
                    SUM(action = 'share') AS shares,
                    SUM(action IN ('bad_password', 'admin_login_failed')) AS bad_passwords,
                    SUM(action = 'blocked') AS blocked_hits,
                    MAX(at) AS last_seen
                FROM activity_log
                WHERE at >= ?
                GROUP BY ip
                ORDER BY downloads + requests DESC, events DESC
                LIMIT ?
                """,
                (since, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def prune_activity(self, before: str) -> None:
        with closing(self.connect()) as connection:
            connection.execute("DELETE FROM activity_log WHERE at < ?", (before,))
            connection.execute(
                "DELETE FROM ip_blocks WHERE expires_at IS NOT NULL AND expires_at < ?",
                (utc_now(),),
            )
            connection.commit()

    def add_block(self, network: str, reason: str, expires_at: str | None) -> None:
        with closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO ip_blocks (network, reason, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(network) DO UPDATE SET
                    reason = excluded.reason, created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (network, reason[:200], utc_now(), expires_at),
            )
            connection.commit()

    def remove_block(self, network: str) -> bool:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "DELETE FROM ip_blocks WHERE network = ?", (network,)
            )
            connection.commit()
            return cursor.rowcount > 0

    def active_blocks(self) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM ip_blocks
                WHERE expires_at IS NULL OR expires_at > ?
                ORDER BY created_at DESC
                """,
                (utc_now(),),
            ).fetchall()
            return [dict(row) for row in rows]


class DuplicateAccountError(RuntimeError):
    pass


class QueueFullError(RuntimeError):
    pass


class ActiveJobError(RuntimeError):
    pass
