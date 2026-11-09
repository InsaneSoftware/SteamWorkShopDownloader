from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .accounts import AccountPool
from .config import Config
from .database import Database, utc_now
from .steamcmd import SteamCmdError


log = logging.getLogger(__name__)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if path.stat().st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class QueueWorker:
    def __init__(self, config: Config, database: Database, pool: AccountPool):
        self.config = config
        self.database = database
        self.pool = pool
        self.steamcmd = pool.steamcmd
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock_path = config.data_dir / "single-worker.lock"
        self._next_cache_cleanup_at = datetime.min.replace(tzinfo=timezone.utc)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="workshop-queue", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        recovered = False
        while not self._stop.is_set():
            with exclusive_file_lock(self._lock_path):
                if not recovered:
                    self.database.recover_interrupted()
                    recovered = True
                self._expire_files()
                self._prune_old_workshop_content()
                # One account check per loop keeps downloads and checks
                # interleaved; SteamCMD can only run once at a time.
                try:
                    self.pool.check_due_account()
                except Exception:
                    log.exception("Account check failed")
                job = self.database.claim_next()
                if job:
                    self._process(job)
            self._stop.wait(self.config.poll_seconds)

    def _process(self, job: dict) -> None:
        job_id = job["id"]
        try:
            source_dir = self._download_with_pool(job)
            self.database.update_job(
                job_id, status="packing", progress=78, message="Creating ZIP archive"
            )
            game_slug = re.sub(
                r"[^a-z0-9]+", "-", job["game_name"].lower()
            ).strip("-") or f"game-{job['app_id']}"
            archive_name = f"{game_slug}_{job['workshop_id']}.zip"
            archive_path = self.config.archive_dir / archive_name
            archive_bytes = self._make_zip(
                source_dir,
                archive_path,
                job["workshop_id"],
                lambda value: self.database.update_job(
                    job_id, progress=value, message="Creating ZIP archive"
                ),
            )
            self._delete_workshop_content(source_dir)
            expires_at = datetime.now(timezone.utc) + timedelta(hours=self.config.file_ttl_hours)
            self.database.update_job(
                job_id,
                status="ready",
                progress=100,
                message="Ready to share",
                archive_name=archive_name,
                archive_bytes=archive_bytes,
                completed_at=utc_now(),
                expires_at=expires_at.isoformat(),
                error=None,
            )
        except Exception as exc:
            log.exception("Job %s failed", job_id)
            self.database.update_job(
                job_id,
                status="failed",
                message="Download failed",
                error=str(exc)[-4000:],
                completed_at=utc_now(),
            )

    def _download_with_pool(self, job: dict) -> Path:
        job_id = job["id"]
        accounts = self.pool.accounts_for_app(job["app_id"])
        if not accounts:
            raise RuntimeError(
                f"No working Steam account in the pool owns {job['game_name']}."
            )
        last_error: Exception | None = None
        for account in accounts[:3]:
            self.database.update_job(
                job_id, message=f"Downloading via {account['display_name']}"
            )
            self.pool.mark_used(account["id"])
            try:
                return self.steamcmd.download(
                    job["app_id"],
                    job["workshop_id"],
                    lambda value, message: self.database.update_job(
                        job_id, progress=value, message=message
                    ),
                    self.pool.credentials(account),
                )
            except SteamCmdError as exc:
                if not exc.login_failed:
                    raise
                # Let the checker confirm and flag it; try the next account.
                log.warning("Login failed for account %s", account["id"][:8])
                self.pool.request_recheck(account["id"])
                last_error = exc
        raise last_error or RuntimeError("No Steam account could log in")

    def _make_zip(self, source: Path, destination: Path, workshop_id: str, progress) -> int:
        self.config.archive_dir.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            raise RuntimeError("The downloaded workshop item is a symlink")
        source = source.resolve()
        if source.is_file():
            source_root = source.parent
            files = [source]
        elif source.is_dir():
            source_root = source
            files = [
                path for path in source_root.rglob("*")
                if path.is_file() and not path.is_symlink()
            ]
        else:
            files = []
            source_root = source.parent
        if not files:
            raise RuntimeError("The downloaded workshop item contains no files")

        part_path = destination.with_suffix(".zip.part")
        if part_path.exists():
            part_path.unlink()
        max_bytes = self.config.max_archive_gb * 1024**3
        written_source_bytes = 0
        try:
            with zipfile.ZipFile(
                part_path, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
            ) as archive:
                for index, path in enumerate(files, start=1):
                    resolved = path.resolve()
                    if source_root not in resolved.parents:
                        continue
                    written_source_bytes += path.stat().st_size
                    if written_source_bytes > max_bytes:
                        raise RuntimeError(
                            f"Workshop item exceeds the {self.config.max_archive_gb} GB limit"
                        )
                    relative = path.relative_to(source_root)
                    archive.write(path, Path(f"workshop_{workshop_id}") / relative)
                    if index == len(files) or index % 25 == 0:
                        progress(78 + int(index / len(files) * 20))
            os.replace(part_path, destination)
        finally:
            if part_path.exists():
                part_path.unlink()
        return destination.stat().st_size

    def _delete_workshop_content(self, source: Path) -> None:
        if source.is_symlink():
            return
        try:
            resolved_source = source.resolve(strict=True)
        except FileNotFoundError:
            return
        resolved_root = self.config.steam_content_root.resolve()
        if resolved_source != resolved_root and resolved_root not in resolved_source.parents:
            return
        if resolved_source.is_file():
            resolved_source.unlink(missing_ok=True)
            return
        if resolved_source.is_dir():
            shutil.rmtree(resolved_source)

    def _expire_files(self) -> None:
        for job in self.database.expired_archives(utc_now()):
            name = job.get("archive_name")
            if name and Path(name).name == name:
                path = self.config.archive_dir / name
                if path.is_file() and not path.is_symlink():
                    path.unlink()
            self.database.update_job(
                job["id"], status="expired", message="Download link expired"
            )

    def _prune_old_workshop_content(self) -> None:
        now = datetime.now(timezone.utc)
        if now < self._next_cache_cleanup_at:
            return
        self._next_cache_cleanup_at = now + timedelta(hours=1)

        root = self.config.steam_content_root.resolve()
        if not root.exists() or not root.is_dir():
            return

        cutoff = now.timestamp() - (self.config.file_ttl_hours * 3600)
        for app_dir in root.iterdir():
            if app_dir.is_symlink() or not app_dir.is_dir():
                continue
            for item_dir in app_dir.iterdir():
                if item_dir.is_symlink() or not item_dir.exists():
                    continue
                if self._latest_mtime(item_dir) < cutoff:
                    self._delete_workshop_content(item_dir)
            try:
                next(app_dir.iterdir())
            except StopIteration:
                app_dir.rmdir()

    def _latest_mtime(self, path: Path) -> float:
        latest = path.stat().st_mtime
        if path.is_dir():
            for child in path.rglob("*"):
                if child.is_symlink():
                    continue
                try:
                    latest = max(latest, child.stat().st_mtime)
                except FileNotFoundError:
                    continue
        return latest
