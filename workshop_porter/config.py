from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Game:
    app_id: int
    name: str


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
        admin_password = os.getenv(
            "ADMIN_PASSWORD", str(security.get("admin_password", ""))
        ).strip()

        if not admin_token and not testing:
            raise RuntimeError("[security].admin_token must be configured.")

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
        if not games:
            raise RuntimeError("At least one enabled [[games]] entry is required.")

        return cls(
            host=str(server.get("host", "0.0.0.0")),
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
        )
