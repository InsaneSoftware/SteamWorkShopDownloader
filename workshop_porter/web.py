from __future__ import annotations

import hmac
import hashlib
import re
import secrets
import threading
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote as url_quote

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.http import dump_options_header

from .config import Config
from .catalog import load_catalog
from .database import ActiveJobError, Database, QueueFullError, utc_now
from .metadata import fetch_workshop_details
from .worker import QueueWorker


WORKSHOP_ID_RE = re.compile(r"^\d{5,20}$")
ALLOWED_HOSTS = {"steamcommunity.com", "www.steamcommunity.com"}


class DownloadLimiter:
    """Reserve a bounded number of WSGI workers for direct file transfers."""

    def __init__(self, limit: int):
        self.limit = limit
        self._active_jobs: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self, job_id: str) -> bool:
        with self._lock:
            if job_id in self._active_jobs or len(self._active_jobs) >= self.limit:
                return False
            self._active_jobs.add(job_id)
            return True

    def release(self, job_id: str) -> None:
        with self._lock:
            self._active_jobs.discard(job_id)


def parse_workshop_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError("Use a steamcommunity.com Workshop link")
    if parsed.path.rstrip("/") not in {
        "/sharedfiles/filedetails",
        "/workshop/filedetails",
    }:
        raise ValueError("This does not look like a Steam Workshop item link")
    workshop_id = parse_qs(parsed.query).get("id", [""])[0]
    if not WORKSHOP_ID_RE.fullmatch(workshop_id):
        raise ValueError("The Workshop link has no valid item ID")
    return workshop_id


def create_app(
    config: Config | None = None,
    start_worker: bool = True,
    metadata_fetcher=None,
) -> Flask:
    config = config or Config.from_file()
    metadata_fetcher = metadata_fetcher or fetch_workshop_details
    catalog = load_catalog(config)
    games_by_app_id = {game.app_id: game.name for game in catalog.games}
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)
    database = Database(config.database_path)
    database.initialize()

    app = Flask(__name__)
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024, JSON_SORT_KEYS=False)
    app.extensions["workshop_config"] = config
    app.extensions["workshop_database"] = database
    worker = QueueWorker(config, database, account_for=catalog.account_for)
    app.extensions["workshop_worker"] = worker
    download_limiter = DownloadLimiter(config.max_concurrent_downloads)
    app.extensions["workshop_download_limiter"] = download_limiter
    if start_worker:
        worker.start()

    def require_admin():
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {config.admin_token}"):
            return jsonify(error="A valid admin token is required"), 401
        return None

    def admin_cookie_value():
        return hmac.new(
            config.admin_token.encode(),
            config.admin_password.encode(),
            hashlib.sha256,
        ).hexdigest()

    def has_admin_access():
        if not config.admin_password:
            return False
        return hmac.compare_digest(
            request.cookies.get("rin_admin", ""),
            admin_cookie_value(),
        )

    def serialize_job(
        job: dict,
        include_download_link: bool = False,
        include_error: bool = False,
    ) -> dict:
        result = {
            key: job.get(key)
            for key in (
                "id", "app_id", "game_name", "workshop_id", "source_url",
                "item_name",
                "status", "progress", "message", "error", "archive_bytes",
                "created_at", "started_at", "completed_at", "expires_at",
            )
        }
        if not include_error:
            result["error"] = None
            if job["status"] == "failed":
                result["message"] = "Failed"
        if include_download_link and job["status"] == "ready":
            base = config.public_base_url or request.url_root.rstrip("/")
            result["download_url"] = (
                f"{base}/download/{job['id']}?token={job['download_token']}"
            )
        return result

    @app.get("/")
    def index():
        secret = request.args.get("admin")
        if secret is not None:
            if not config.admin_password or not hmac.compare_digest(
                secret, config.admin_password
            ):
                return "No", 403
            response = redirect(url_for("index"))
            response.set_cookie(
                "rin_admin",
                admin_cookie_value(),
                max_age=60 * 60 * 8,
                httponly=True,
                secure=request.is_secure,
                samesite="Lax",
            )
            return response
        return render_template("index.html", games=games_by_app_id)

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.get("/api/admin")
    def admin_status():
        return jsonify(admin=has_admin_access())

    @app.get("/api/jobs")
    def list_jobs():
        auth_error = require_admin()
        if auth_error:
            return auth_error
        show_errors = has_admin_access()
        return jsonify(
            jobs=[
                serialize_job(
                    job,
                    include_download_link=True,
                    include_error=show_errors,
                )
                for job in database.list_jobs()
            ]
        )

    @app.post("/api/jobs")
    def create_job_route():
        auth_error = require_admin()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        try:
            app_id = int(payload.get("app_id", 0))
            if app_id not in games_by_app_id:
                raise ValueError("Choose one of the configured games")
            source_url = str(payload.get("url", "")).strip()
            workshop_id = parse_workshop_url(source_url)
            details = metadata_fetcher(workshop_id)
            if details and details.consumer_app_id != app_id:
                actual_game = games_by_app_id.get(
                    details.consumer_app_id,
                    f"Steam app {details.consumer_app_id}",
                )
                raise ValueError(
                    f'This Workshop item belongs to "{actual_game}", '
                    f'not "{games_by_app_id[app_id]}".'
                )
            job = {
                "id": uuid.uuid4().hex,
                "app_id": app_id,
                "game_name": games_by_app_id[app_id],
                "workshop_id": workshop_id,
                "item_name": details.title if details else None,
                "source_url": source_url,
                "status": "queued",
                "progress": 0,
                "message": "Waiting in queue",
                "error": None,
                "archive_name": None,
                "archive_bytes": None,
                "download_token": secrets.token_urlsafe(32),
                "created_at": utc_now(),
                "started_at": None,
                "completed_at": None,
                "expires_at": None,
            }
            stored, created = database.create_job(job, config.max_queue_size)
            return (
                jsonify(
                    job=serialize_job(
                        stored,
                        include_download_link=True,
                        include_error=has_admin_access(),
                    ),
                    created=created,
                ),
                201 if created else 200,
            )
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        except QueueFullError as exc:
            return jsonify(error=str(exc)), 429

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        auth_error = require_admin()
        if auth_error:
            return auth_error
        job = database.get_job(job_id)
        if not job:
            return jsonify(error="Job not found"), 404
        return jsonify(
            job=serialize_job(
                job,
                include_download_link=True,
                include_error=has_admin_access(),
            )
        )

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id: str):
        if not has_admin_access():
            return jsonify(error="Admin required"), 403
        try:
            job = database.delete_job(job_id)
        except ActiveJobError:
            return jsonify(error="Wait for the active download to finish"), 409
        if not job:
            return jsonify(error="Job not found"), 404
        name = job.get("archive_name")
        if name and Path(name).name == name:
            archive = config.archive_dir / name
            if archive.is_file() and not archive.is_symlink():
                archive.unlink()
        return jsonify(deleted=True)

    @app.get("/download/<job_id>")
    def download(job_id: str):
        job = database.get_job(job_id)
        supplied = request.args.get("token", "")
        if not job or not hmac.compare_digest(supplied, job.get("download_token", "")):
            return jsonify(error="Download not found"), 404
        if job["status"] != "ready" or not job["archive_name"]:
            return jsonify(error="This download is not available"), 410
        if Path(job["archive_name"]).name != job["archive_name"]:
            return jsonify(error="Invalid archive"), 500
        archive = config.archive_dir / job["archive_name"]
        if not archive.is_file():
            return jsonify(error="Archive no longer exists"), 410

        if config.x_accel_redirect_prefix:
            response = app.response_class(status=200, mimetype="application/zip")
            response.headers["X-Accel-Redirect"] = (
                f"{config.x_accel_redirect_prefix}/"
                f"{url_quote(job['archive_name'], safe='')}"
            )
            response.headers["Content-Disposition"] = dump_options_header(
                "attachment", {"filename": job["archive_name"]}
            )
        else:
            if not download_limiter.acquire(job_id):
                response = jsonify(
                    error=(
                        "This archive is already being downloaded or all download "
                        "slots are busy. Try again shortly."
                    )
                )
                response.status_code = 429
                response.headers["Retry-After"] = "30"
                return response
            try:
                response = send_file(
                    archive,
                    as_attachment=True,
                    download_name=job["archive_name"],
                    conditional=True,
                    max_age=0,
                )
            except Exception:
                download_limiter.release(job_id)
                raise
            response.call_on_close(lambda: download_limiter.release(job_id))

        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="Request is too large"), 413

    return app
