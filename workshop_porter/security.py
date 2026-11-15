"""IP blocking and activity logging helpers."""
from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address, ip_network
from typing import Any

from .database import Database


ACTIVITY_RETENTION_DAYS = 90
# Addresses a reverse proxy or LAN would show up as. Never blockable.
LOCAL_NETWORKS = tuple(ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
))


def normalize_ip(value: str | None) -> str:
    try:
        return str(ip_address((value or "").strip()))
    except ValueError:
        return (value or "unknown").strip()[:64] or "unknown"


def parse_block_target(value: str) -> str:
    """Validate an IP or CIDR range and return it in canonical form.

    Loopback and private ranges are refused. Behind a reverse proxy without
    ``trusted_proxy_hops`` every visitor shows up as 127.0.0.1, so blocking
    it would block the whole site.
    """
    text = value.strip()
    try:
        network = ip_network(text, strict=False)
    except ValueError:
        raise ValueError("Enter an IP address like 203.0.113.7 or a range like 203.0.113.0/24")
    local = any(
        network.version == lan.version and network.overlaps(lan) for lan in LOCAL_NETWORKS
    )
    if local or network.is_loopback or network.is_link_local or network.is_unspecified:
        raise ValueError(
            "Local and private addresses cannot be blocked. If every visitor shows "
            "as 127.0.0.1, set [server].trusted_proxy_hops = 1 in config.toml."
        )
    if network.num_addresses > 65536:
        raise ValueError("That range is too large. Use /16 or smaller (/48 for IPv6).")
    if network.num_addresses == 1:
        return str(network.network_address)
    return str(network)


UA_MATCH_TYPES = ("exact", "contains")
# Parts of nearly every browser's user agent. Blocking on them would block
# almost everyone, so "contains" refuses them.
TOO_BROAD_UA_PARTS = {
    "mozilla", "mozilla/5.0", "applewebkit", "khtml", "like gecko", "gecko",
    "chrome", "safari", "windows", "windows nt", "win64", "x64", "linux",
    "android", "iphone", "mac os", "macintosh", "firefox", "edg", "mobile",
}


def parse_ua_block(pattern: str, match: str) -> tuple[str, str]:
    """Validate a user agent block. An empty exact pattern blocks requests
    without a User-Agent header."""
    match = (match or "contains").strip().lower()
    if match not in UA_MATCH_TYPES:
        raise ValueError("Choose exact or contains")
    pattern = pattern.strip()[:400]
    if match == "contains":
        if len(pattern) < 4:
            raise ValueError("Use at least 4 characters for a contains match")
        if pattern.lower() in TOO_BROAD_UA_PARTS:
            raise ValueError(
                f'"{pattern}" is part of almost every browser and would block nearly '
                "everyone. Use a more specific part or an exact match."
            )
    return pattern, match


def ua_matches(user_agent: str, pattern: str, match: str) -> bool:
    if match == "exact":
        return user_agent == pattern
    return pattern.lower() in user_agent.lower()


def expiry_from_hours(hours: float | None) -> str | None:
    if not hours:
        return None  # permanent
    if hours < 0 or hours > 24 * 365 * 10:
        raise ValueError("Block duration must be between 1 hour and 10 years")
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


AUTO_BLOCK_FAILURES = 5
AUTO_BLOCK_WINDOW_SECONDS = 24 * 3600
AUTO_BLOCK_HOURS = 24
DAILY_DOWNLOAD_LIMIT = 25


def is_blockable(ip: str) -> bool:
    try:
        parse_block_target(ip)
    except ValueError:
        return False
    return True


class IpGuard:
    """Answers "is this IP blocked?" with a short cache, and throttles logs."""

    CACHE_SECONDS = 5

    def __init__(self, database: Database):
        self.database = database
        self._lock = threading.Lock()
        self._blocks: list[tuple[Any, dict[str, Any]]] = []
        self._ua_blocks: list[dict[str, Any]] = []
        self._loaded_at = 0.0
        self._last_logged: dict[tuple[str, str], float] = {}
        self._failures: dict[str, dict[str, float]] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._loaded_at = 0.0

    def ua_block_for(self, user_agent: str) -> dict[str, Any] | None:
        self._refresh()
        with self._lock:
            blocks = self._ua_blocks
        current = datetime.now(timezone.utc).isoformat()
        for row in blocks:
            if row["expires_at"] is not None and row["expires_at"] <= current:
                continue
            if ua_matches(user_agent, row["pattern"], row["match"]):
                return row
        return None

    def _refresh(self) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._loaded_at <= self.CACHE_SECONDS:
                return
            self._blocks = [
                (ip_network(row["network"], strict=False), row)
                for row in self.database.active_blocks()
            ]
            self._ua_blocks = self.database.active_ua_blocks()
            self._loaded_at = now

    def block_for(self, ip: str) -> dict[str, Any] | None:
        try:
            address = ip_address(ip)
        except ValueError:
            return None
        self._refresh()
        with self._lock:
            blocks = self._blocks
        current = datetime.now(timezone.utc).isoformat()
        for network, row in blocks:
            if address.version == network.version and address in network:
                if row["expires_at"] is None or row["expires_at"] > current:
                    return row
        return None

    def should_log(self, ip: str, action: str, every_seconds: float) -> bool:
        """True at most once per ``every_seconds`` for an IP and action."""
        now = time.monotonic()
        key = (ip, action)
        with self._lock:
            if now - self._last_logged.get(key, -1e9) < every_seconds:
                return False
            self._last_logged[key] = now
            if len(self._last_logged) > 10000:
                cutoff = now - 3600
                self._last_logged = {
                    k: v for k, v in self._last_logged.items() if v > cutoff
                }
            return True

    def password_failed(self, ip: str, attempt: str) -> bool:
        """Count a wrong password. Blocks the IP and returns True on the fifth.

        Only different passwords count. The page keeps polling with the saved
        password, so one stale password repeated must not block anyone. Every
        attempt counts, not only the ones that reach the activity log. Local
        addresses are never blocked, see ``parse_block_target``.
        """
        if not is_blockable(ip):
            return False
        key = hashlib.sha256(attempt.encode("utf-8", "replace")).hexdigest()[:16]
        now = time.monotonic()
        cutoff = now - AUTO_BLOCK_WINDOW_SECONDS
        with self._lock:
            seen = {k: t for k, t in self._failures.get(ip, {}).items() if t > cutoff}
            seen[key] = now
            if len(seen) < AUTO_BLOCK_FAILURES:
                self._failures[ip] = seen
                if len(self._failures) > 10000:
                    self._failures = {
                        k: v for k, v in self._failures.items()
                        if v and max(v.values()) > cutoff
                    }
                return False
            self._failures.pop(ip, None)
        return self.auto_block(ip, f"Auto: {AUTO_BLOCK_FAILURES} wrong passwords")

    def auto_block(self, ip: str, reason: str) -> bool:
        """Block an IP for AUTO_BLOCK_HOURS. False for local or already blocked IPs."""
        try:
            network = parse_block_target(ip)
        except ValueError:
            return False
        if self.block_for(ip):
            return False
        self.database.add_block(network, reason, expiry_from_hours(AUTO_BLOCK_HOURS))
        self.invalidate()
        return True

    def prune(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=ACTIVITY_RETENTION_DAYS)
        self.database.prune_activity(cutoff.isoformat())
