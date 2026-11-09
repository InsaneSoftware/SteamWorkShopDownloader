from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path


@dataclass(frozen=True)
class Game:
    app_id: int
    name: str


@dataclass(frozen=True)
class SteamAccount:
    name: str
    username: str
    password: str
    guard_code: str
    steam_id: str


@dataclass(frozen=True)
class Config:
    host: str
    port: int
    data_dir: Path
    database_path: Path
    archive_dir: Path
    steamcmd_path: Path
    steam_content_root: Path
    steam_username: str
    steam_password: str
    steam_guard_code: str
    admin_token: str
    admin_password: str
    public_base_url: str
    file_ttl_hours: int
    max_queue_size: int
    max_archive_gb: int
    poll_seconds: float
    games: tuple[Game, ...]
    steam_web_api_key: str = ""
    steam_accounts: tuple[SteamAccount, ...] = ()
    threads: int = 16
    connection_limit: int = 200
    channel_timeout: int = 120
    max_downloads_per_ip: int = 2
    trusted_proxy_hops: int = 0
    x_accel_redirect_prefix: str = ""
    credentials_key: str = ""
    account_check_minutes: int = 15
    broken_recheck_minutes: int = 360
    max_pending_accounts: int = 25
    steamcmd_timeout_seconds: int = 120
    max_store_lookups_per_check: int = 60

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "Config":
        config_path = Path(
            path or os.getenv("WORKSHOP_PORTER_CONFIG", "config.toml")
        ).resolve()
        if not config_path.is_file():
            raise RuntimeError(
                f"Configuration not found: {config_path}. "
                "Copy config.example.toml to config.toml and edit it."
            )

        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)

        root = config_path.parent
        server = raw.get("server", {})
        security = raw.get("security", {})
        steam = raw.get("steam", {})
        platform_name = "windows" if os.name == "nt" else "linux"
        platform_steam = steam.get(platform_name, {})
        storage = raw.get("storage", {})
        worker = raw.get("worker", {})
        pool = raw.get("accounts", {})
        testing = bool(raw.get("testing", False))

        def resolve(value: str) -> Path:
            candidate = Path(value)
            return (candidate if candidate.is_absolute() else root / candidate).resolve()

        data_dir = resolve(str(storage.get("data_dir", "data")))
        default_executable = (
            "SteamCMD/steamcmd.exe"
            if platform_name == "windows"
            else "steamcmd"
        )
        steamcmd_path = resolve(
            os.getenv(
                "WORKSHOP_PORTER_STEAMCMD_PATH",
                str(
                    platform_steam.get(
                        "steamcmd_path",
                        steam.get("steamcmd_path", default_executable),
                    )
                ),
            )
        )
        if not steamcmd_path.is_file():
            names = (
                ("steamcmd.exe",)
                if platform_name == "windows"
                else ("steamcmd", "steamcmd.sh")
            )
            found = next(
                (path for name in names if (path := shutil.which(name))),
                None,
            )
            if not found and platform_name == "linux":
                found = next(
                    (
                        str(path)
                        for path in (
                            Path("/usr/games/steamcmd"),
                            Path("/usr/bin/steamcmd"),
                            Path("/usr/local/bin/steamcmd"),
                        )
                        if path.is_file()
                    ),
                    None,
                )
            if found:
                steamcmd_path = Path(found).resolve()
        content_default = steamcmd_path.parent / "steamapps" / "workshop" / "content"
        if platform_name == "linux":
            home_content_root = (
                Path.home() / ".local" / "share" / "Steam" / "steamapps" / "workshop" / "content"
            )
            if home_content_root.exists():
                content_default = home_content_root.resolve()
        steam_content_root = resolve(
            os.getenv(
                "WORKSHOP_PORTER_STEAM_CONTENT_ROOT",
                str(
                    platform_steam.get(
                        "content_root",
                        steam.get("content_root", content_default),
                    )
                ),
            )
        )

        admin_token = os.getenv(
            "WORKSHOP_PORTER_ADMIN_TOKEN", str(security.get("admin_token", ""))
        ).strip()
        username = os.getenv(
            "WORKSHOP_PORTER_STEAM_USERNAME", str(steam.get("username", ""))
        ).strip()
        password = os.getenv(
            "WORKSHOP_PORTER_STEAM_PASSWORD", str(steam.get("password", ""))
        )
        guard_code = os.getenv(
            "WORKSHOP_PORTER_STEAM_GUARD_CODE", str(steam.get("guard_code", ""))
        ).strip()
        web_api_key = os.getenv(
            "WORKSHOP_PORTER_STEAM_WEB_API_KEY", str(steam.get("web_api_key", ""))
        ).strip()
        admin_password = os.getenv(
            "ADMIN_PASSWORD", str(security.get("admin_password", ""))
        ).strip()

        if not admin_token and not testing:
            raise RuntimeError("[security].admin_token must be configured.")

        threads = int(server.get("threads", 16))
        connection_limit = int(server.get("connection_limit", 200))
        channel_timeout = int(server.get("channel_timeout", 120))
        max_downloads_per_ip = int(server.get("max_downloads_per_ip", 2))
        trusted_proxy_hops = int(server.get("trusted_proxy_hops", 0))
        if threads < 4:
            raise RuntimeError("[server].threads must be at least 4.")
        if connection_limit <= threads:
            raise RuntimeError(
                "[server].connection_limit must be greater than [server].threads."
            )
        if channel_timeout < 30:
            raise RuntimeError("[server].channel_timeout must be at least 30 seconds.")
        if max_downloads_per_ip < 1:
            raise RuntimeError("[server].max_downloads_per_ip must be at least 1.")
        if not 0 <= trusted_proxy_hops <= 5:
            raise RuntimeError("[server].trusted_proxy_hops must be between 0 and 5.")
        configured_host = str(server.get("host", "0.0.0.0"))
        try:
            proxy_host_is_safe = ip_address(configured_host).is_loopback
        except ValueError:
            proxy_host_is_safe = configured_host.lower() == "localhost"
        if trusted_proxy_hops and not proxy_host_is_safe:
            raise RuntimeError(
                "[server].trusted_proxy_hops requires [server].host to be a "
                "loopback address such as 127.0.0.1."
            )
        x_accel_redirect_prefix = str(
            server.get("x_accel_redirect_prefix", "")
        ).strip().rstrip("/")
        if x_accel_redirect_prefix and (
            not x_accel_redirect_prefix.startswith("/")
            or x_accel_redirect_prefix.startswith("//")
            or any(
                character in x_accel_redirect_prefix
                for character in ("\r", "\n", "?", "#")
            )
        ):
            raise RuntimeError(
                "[server].x_accel_redirect_prefix must be an internal URL path."
            )

        accounts: list[SteamAccount] = []
        for item in steam.get("accounts", []):
            account_username = str(item.get("username", "")).strip()
            account_password = str(item.get("password", ""))
            if not account_username or not account_password:
                raise RuntimeError(
                    "Every [[steam.accounts]] entry needs username and password."
                )
            accounts.append(
                SteamAccount(
                    name=(
                        str(item.get("name", account_username)).strip()
                        or account_username
                    ),
                    username=account_username,
                    password=account_password,
                    guard_code=str(item.get("guard_code", "")).strip(),
                    steam_id=str(item.get("steam_id", "")).strip(),
                )
            )
        if not accounts and username and password:
            accounts.append(
                SteamAccount(
                    name=username,
                    username=username,
                    password=password,
                    guard_code=guard_code,
                    steam_id=str(steam.get("steam_id", "")).strip(),
                )
            )

        games: list[Game] = []
        seen_app_ids: set[int] = set()
        for item in raw.get("games", []):
            if not item.get("enabled", True):
                continue
            app_id = int(item["app_id"])
            name = str(item["name"]).strip()
            if app_id <= 0 or not name:
                raise RuntimeError("Every enabled game needs a valid app_id and name.")
            if app_id in seen_app_ids:
                raise RuntimeError(f"Duplicate game app_id in configuration: {app_id}")
            seen_app_ids.add(app_id)
            games.append(Game(app_id=app_id, name=name))

        return cls(
            host=configured_host,
            port=int(server.get("port", 8765)),
            data_dir=data_dir,
            database_path=resolve(
                str(storage.get("database_path", data_dir / "jobs.sqlite3"))
            ),
            archive_dir=resolve(
                str(storage.get("archive_dir", data_dir / "archives"))
            ),
            steamcmd_path=steamcmd_path,
            steam_content_root=steam_content_root,
            steam_username=username,
            steam_password=password,
            steam_guard_code=guard_code,
            admin_token=admin_token,
            admin_password=admin_password,
            public_base_url=str(server.get("public_base_url", "")).strip().rstrip("/"),
            file_ttl_hours=max(1, int(storage.get("file_ttl_hours", 24))),
            max_queue_size=max(1, int(worker.get("max_queue_size", 100))),
            max_archive_gb=max(1, int(worker.get("max_archive_gb", 25))),
            poll_seconds=max(0.2, float(worker.get("poll_seconds", 1))),
            games=tuple(games),
            steam_web_api_key=web_api_key,
            steam_accounts=tuple(accounts),
            threads=threads,
            connection_limit=connection_limit,
            channel_timeout=channel_timeout,
            max_downloads_per_ip=max_downloads_per_ip,
            trusted_proxy_hops=trusted_proxy_hops,
            x_accel_redirect_prefix=x_accel_redirect_prefix,
            credentials_key=os.getenv(
                "WORKSHOP_PORTER_CREDENTIALS_KEY",
                str(security.get("credentials_key", "")),
            ).strip(),
            account_check_minutes=max(1, int(pool.get("check_interval_minutes", 15))),
            broken_recheck_minutes=max(5, int(pool.get("broken_recheck_minutes", 360))),
            max_pending_accounts=max(1, int(pool.get("max_pending", 25))),
            steamcmd_timeout_seconds=max(30, int(pool.get("login_timeout_seconds", 120))),
            max_store_lookups_per_check=max(
                1, int(pool.get("max_store_lookups_per_check", 60))
            ),
        )
