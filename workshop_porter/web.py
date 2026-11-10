from __future__ import annotations

import hmac
import hashlib
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.parse import quote as url_quote

from flask import Flask, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.http import dump_options_header
from werkzeug.middleware.proxy_fix import ProxyFix

from .accounts import AccountPool
from .config import Config
from .database import (
    ActiveJobError, Database, DuplicateAccountError, QueueFullError, utc_now,
)
from .metadata import fetch_workshop_details
from .security import IpGuard, expiry_from_hours, normalize_ip, parse_block_target
from .steamcmd import SteamCmd
from .vault import CredentialVault
from .worker import QueueWorker


WORKSHOP_ID_RE = re.compile(r"^\d{5,20}$")
ALLOWED_HOSTS = {"steamcommunity.com", "www.steamcommunity.com"}


class DownloadLimiter:
    """Limit simultaneous direct file transfers by client IP address."""

    def __init__(self, per_ip_limit: int):
        self.per_ip_limit = per_ip_limit
        self._active_by_ip: dict[str, int] = {}
        self._lock = threading.Lock()

    def acquire(self, client_ip: str) -> Callable[[], None] | None:
        with self._lock:
            active_for_ip = self._active_by_ip.get(client_ip, 0)
            if active_for_ip >= self.per_ip_limit:
                return None
            self._active_by_ip[client_ip] = active_for_ip + 1

        released = False

        def release() -> None:
            nonlocal released
            with self._lock:
                if released:
                    return
                released = True
                active = self._active_by_ip.get(client_ip, 0)
                if active <= 1:
                    self._active_by_ip.pop(client_ip, None)
                else:
                    self._active_by_ip[client_ip] = active - 1

        return release


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
    steamcmd: SteamCmd | None = None,
    steam_api_opener=None,
) -> Flask:
    config = config or Config.from_file()
    metadata_fetcher = metadata_fetcher or fetch_workshop_details
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.archive_dir.mkdir(parents=True, exist_ok=True)
    database = Database(config.database_path)
    database.initialize()
    vault = CredentialVault.from_config(config.credentials_key, config.data_dir)
    pool_options = {"opener": steam_api_opener} if steam_api_opener else {}
    pool = AccountPool(config, database, vault, steamcmd, **pool_options)
    pool.import_config_accounts()

    app = Flask(__name__)
    if config.trusted_proxy_hops:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=config.trusted_proxy_hops,
            x_proto=config.trusted_proxy_hops,
        )
    app.config.update(MAX_CONTENT_LENGTH=32 * 1024, JSON_SORT_KEYS=False)
    app.extensions["workshop_config"] = config
    app.extensions["workshop_database"] = database
    app.extensions["workshop_accounts"] = pool
    worker = QueueWorker(config, database, pool)
    app.extensions["workshop_worker"] = worker
    download_limiter = DownloadLimiter(config.max_downloads_per_ip)
    app.extensions["workshop_download_limiter"] = download_limiter
    ip_guard = IpGuard(database)
    app.extensions["workshop_ip_guard"] = ip_guard
    ip_guard.prune()
    next_prune = [time.monotonic() + 6 * 3600]

    def client_ip() -> str:
        # request.remote_addr is already the real visitor when
        # [server].trusted_proxy_hops is set (ProxyFix above).
        return normalize_ip(request.remote_addr)

    def log_activity(action: str, status: str, detail: str = "", bytes_: int | None = None):
        try:
            database.log_activity(client_ip(), action, status, detail, bytes_)
        except Exception:
            app.logger.exception("Could not write the activity log")

    @app.before_request
    def enforce_ip_blocks():
        if time.monotonic() > next_prune[0]:
            next_prune[0] = time.monotonic() + 6 * 3600
            ip_guard.prune()
        ip = client_ip()
        block = ip_guard.block_for(ip)
        if not block or has_admin_access():
            return None
        # Let the admin log in from a blocked address, and keep the page styled.
        if request.path.startswith("/static/") or (
            request.path == "/" and "admin" in request.args
        ):
            return None
        if ip_guard.should_log(ip, "blocked", 300):
            log_activity("blocked", "denied", f"{request.method} {request.path}"[:120])
        until = (
            "permanently" if block["expires_at"] is None
            else "until " + block["expires_at"][:16].replace("T", " ") + " UTC"
        )
        message = f"Your IP address is blocked {until}."
        if request.path.startswith("/api/") or request.path.startswith("/download/"):
            return jsonify(error=message), 403
        return app.response_class(message, status=403, mimetype="text/plain")
    if start_worker:
        worker.start()

    def require_admin():
        supplied = request.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied, f"Bearer {config.admin_token}"):
            if ip_guard.should_log(client_ip(), "bad_password", 60):
                log_activity("bad_password", "denied", f"{request.method} {request.path}")
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

    def require_admin_session():
        if not has_admin_access():
            return jsonify(error="Admin required"), 403
        # Custom header blocks cross-site form posts riding on the cookie.
        if request.method != "GET" and request.headers.get("X-Requested-With") != "fetch":
            return jsonify(error="Bad request"), 400
        return None

    def account_status(account: dict) -> str:
        return account["status"] if account["enabled"] else "disabled"

    def serialize_account(account: dict, names: dict[int, str]) -> dict:
        """Public view: never includes the Steam login or password."""
        games = [
            {"app_id": app_id, "name": names[app_id]}
            for app_id in account["owned_app_ids"]
            if app_id in names
        ]
        games.sort(key=lambda game: game["name"].lower())
        return {
            "id": account["id"],
            "display_name": account["display_name"],
            "status": account_status(account),
            "status_reason": account["status_reason"],
            "games": games,
            "created_at": account["created_at"],
            "last_checked_at": account["last_checked_at"],
            "last_ok_at": account["last_ok_at"],
        }

    def serialize_admin_account(account: dict, names: dict[int, str]) -> dict:
        result = serialize_account(account, names)
        try:
            username = vault.decrypt(account["username_enc"])
        except RuntimeError:
            username = "(cannot decrypt)"
        visible = account.get("library_visible")
        result.update(
            username=username,
            steam_id=account.get("steam_id"),
            library_visible=None if visible is None else bool(visible),
            enabled=account["enabled"],
            raw_status=account["status"],
            last_error=account["last_error"],
            consecutive_failures=account["consecutive_failures"],
            broken_since=account["broken_since"],
            next_check_at=account["next_check_at"],
            last_used_at=account["last_used_at"],
        )
        return result

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
                if ip_guard.should_log(client_ip(), "admin_login_failed", 10):
                    log_activity("admin_login_failed", "denied")
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
        games = {game["app_id"]: game["name"] for game in pool.supported_games()}
        return render_template("index.html", games=games)

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
        games_by_app_id = pool.known_games()
        try:
            app_id = int(payload.get("app_id", 0))
            if app_id not in games_by_app_id:
                raise ValueError("Choose one of the supported games")
            if not pool.accounts_for_app(app_id):
                raise ValueError(
                    f"No working Steam account owns {games_by_app_id[app_id]} yet. "
                    "Add an account that owns it to the pool."
                )
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
            log_activity(
                "request", "queued" if created else "duplicate",
                f"{job['game_name']} · {job['item_name'] or workshop_id} ({workshop_id})",
            )
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
            log_activity("request", "rejected", f"{exc} · {payload.get('url', '')}"[:300])
            return jsonify(error=str(exc)), 400
        except QueueFullError as exc:
            log_activity("request", "queue_full", str(payload.get("url", ""))[:300])
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
            if ip_guard.should_log(client_ip(), "download_not_found", 30):
                log_activity("download", "not_found", job_id[:40])
            return jsonify(error="Download not found"), 404
        label = f"{job['game_name']} · {job.get('item_name') or job['workshop_id']}"
        # Resumed transfers send several ranged requests. Count the first one.
        first_part = request.range is None or (
            request.range.ranges and request.range.ranges[0][0] == 0
        )
        if job["status"] != "ready" or not job["archive_name"]:
            return jsonify(error="This download is not available"), 410
        if Path(job["archive_name"]).name != job["archive_name"]:
            return jsonify(error="Invalid archive"), 500
        archive = config.archive_dir / job["archive_name"]
        if not archive.is_file():
            return jsonify(error="Archive no longer exists"), 410
        if first_part:
            log_activity("download", "ok", label, job.get("archive_bytes"))

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
            visitor_ip = client_ip()
            release_download = download_limiter.acquire(visitor_ip)
            if release_download is None:
                if ip_guard.should_log(visitor_ip, "download_busy", 30):
                    log_activity("download", "busy", label)
                response = jsonify(
                    error=(
                        "This IP address already has the maximum number of active "
                        "downloads. Try again when one has finished."
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
                release_download()
                raise
            original_iterator = response.response

            def release_after_iteration():
                try:
                    yield from original_iterator
                finally:
                    release_download()

            response.response = release_after_iteration()
            response.call_on_close(release_download)

        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    # ----- Community account pool ---------------------------------------

    @app.get("/api/games")
    def list_games():
        auth_error = require_admin()
        if auth_error:
            return auth_error
        return jsonify(games=pool.supported_games())

    @app.get("/api/accounts")
    def list_accounts():
        auth_error = require_admin()
        if auth_error:
            return auth_error
        names = pool.known_games()
        return jsonify(
            accounts=[serialize_account(a, names) for a in database.list_accounts()],
            check_interval_minutes=config.account_check_minutes,
        )

    @app.get("/api/accounts/<account_id>")
    def get_account(account_id: str):
        auth_error = require_admin()
        if auth_error:
            return auth_error
        account = database.get_account(account_id)
        if not account:
            return jsonify(error="Account not found"), 404
        return jsonify(account=serialize_account(account, pool.known_games()))

    @app.post("/api/accounts")
    def submit_account():
        auth_error = require_admin()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        try:
            account, created = pool.submit(
                str(payload.get("display_name", "")),
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                str(payload.get("guard_code", "")),
            )
        except ValueError as exc:
            log_activity("share", "rejected", f"{payload.get('display_name', '')} · {exc}"[:300])
            return jsonify(error=str(exc)), 400
        except DuplicateAccountError as exc:
            log_activity("share", "duplicate", str(payload.get("display_name", ""))[:60])
            state = (
                "is waiting for validation" if str(exc) == "pending"
                else "is already in the pool"
            )
            return jsonify(error=f"This Steam account {state}."), 409
        except QueueFullError as exc:
            log_activity("share", "queue_full", str(payload.get("display_name", ""))[:60])
            return jsonify(error=str(exc)), 429
        log_activity("share", "submitted", account["display_name"])
        return jsonify(
            account=serialize_account(account, pool.known_games()), created=created
        ), 201

    # ----- Admin dashboard ----------------------------------------------

    @app.get("/admin")
    def admin_page():
        if not has_admin_access():
            return redirect(url_for("index"))
        return render_template("admin.html")

    @app.get("/api/admin/accounts")
    def admin_accounts():
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        names = pool.known_games()
        return jsonify(
            accounts=[
                serialize_admin_account(a, names) for a in database.list_accounts()
            ],
            games=pool.supported_games(),
            check_interval_minutes=config.account_check_minutes,
            web_api_key=bool(config.steam_web_api_key),
        )

    @app.post("/api/admin/accounts/<account_id>/recheck")
    def admin_recheck(account_id: str):
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        if not database.get_account(account_id):
            return jsonify(error="Account not found"), 404
        pool.request_recheck(account_id)
        return jsonify(ok=True)

    @app.post("/api/admin/accounts/<account_id>/enabled")
    def admin_set_enabled(account_id: str):
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        if not database.get_account(account_id):
            return jsonify(error="Account not found"), 404
        enabled = bool((request.get_json(silent=True) or {}).get("enabled"))
        fields = {"enabled": enabled}
        if enabled:
            fields["next_check_at"] = utc_now()
        database.update_account(account_id, **fields)
        return jsonify(ok=True)

    @app.post("/api/admin/accounts/<account_id>/name")
    def admin_rename(account_id: str):
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        account = database.get_account(account_id)
        if not account:
            return jsonify(error="Account not found"), 404
        payload = request.get_json(silent=True) or {}
        try:
            name = pool.rename(account, str(payload.get("display_name", "")))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(ok=True, display_name=name)

    @app.delete("/api/admin/accounts/<account_id>")
    def admin_delete_account(account_id: str):
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        if not database.delete_account(account_id):
            return jsonify(error="Account not found"), 404
        return jsonify(deleted=True)

    # ----- Admin: activity and IP blocks --------------------------------

    def serialize_block(block: dict) -> dict:
        return {
            "network": block["network"],
            "reason": block["reason"],
            "created_at": block["created_at"],
            "expires_at": block["expires_at"],
        }

    @app.get("/api/admin/activity")
    def admin_activity():
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        try:
            hours = min(max(int(request.args.get("hours", 24)), 1), 24 * 90)
        except ValueError:
            hours = 24
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        ip_filter = request.args.get("ip", "").strip() or None
        ips = database.activity_by_ip(since)
        for row in ips:
            block = ip_guard.block_for(row["ip"])
            row["blocked"] = serialize_block(block) if block else None
        return jsonify(
            hours=hours,
            ips=ips,
            events=database.recent_activity(since, ip_filter),
            your_ip=client_ip(),
            proxy_configured=bool(config.trusted_proxy_hops),
        )

    @app.get("/api/admin/blocks")
    def admin_blocks():
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        return jsonify(blocks=[serialize_block(b) for b in database.active_blocks()])

    @app.post("/api/admin/blocks")
    def admin_add_block():
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        try:
            network = parse_block_target(str(payload.get("target", "")))
            hours = payload.get("hours")
            expires_at = expiry_from_hours(float(hours) if hours else None)
        except (TypeError, ValueError) as exc:
            return jsonify(error=str(exc)), 400
        database.add_block(network, str(payload.get("reason", "")).strip(), expires_at)
        ip_guard.invalidate()
        app.logger.info("Admin blocked %s until %s", network, expires_at or "forever")
        return jsonify(ok=True, network=network, expires_at=expires_at)

    @app.post("/api/admin/blocks/remove")
    def admin_remove_block():
        auth_error = require_admin_session()
        if auth_error:
            return auth_error
        network = str((request.get_json(silent=True) or {}).get("network", ""))
        if not database.remove_block(network):
            return jsonify(error="Block not found"), 404
        ip_guard.invalidate()
        return jsonify(ok=True)

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error="Request is too large"), 413

    return app
