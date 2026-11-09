"""Encryption for community-submitted Steam credentials.

Logins and passwords are encrypted with Fernet (AES-128-CBC + HMAC-SHA256).
The key comes from ``[security].credentials_key`` or, when that is empty, from
``<data_dir>/credentials.key`` which is generated on first start.

Losing the key makes every stored account unreadable, so back it up privately.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialVault:
    def __init__(self, key: str | bytes):
        raw = key.encode() if isinstance(key, str) else key
        self._fernet = Fernet(raw)
        # Separate key for username fingerprints so duplicates can be found
        # without decrypting every row.
        self._lookup_key = hashlib.sha256(b"username-lookup:" + raw).digest()

    @classmethod
    def from_config(cls, configured_key: str, data_dir: Path) -> "CredentialVault":
        if configured_key:
            return cls(configured_key)
        key_path = data_dir / "credentials.key"
        if key_path.is_file():
            return cls(key_path.read_bytes().strip())
        data_dir.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(key_path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key)
        return cls(key)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str) -> str:
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise RuntimeError(
                "Stored credentials cannot be decrypted. The credentials key changed."
            ) from exc

    def fingerprint(self, username: str) -> str:
        normalized = username.strip().lower().encode("utf-8")
        return hmac.new(self._lookup_key, normalized, hashlib.sha256).hexdigest()
