from __future__ import annotations

import json
import os
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class EncryptedStore:
    """Only encrypted sessions/journals are persisted; files replace atomically."""

    def __init__(self, root: Path, key: bytes):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cipher = AESGCM(key)
        self.lock = threading.RLock()

    def cleanup_abandoned(self):
        # Caller must own the process lock; never unlink another process's
        # atomic write from an application factory/read-only startup path.
        with self.lock:
            for abandoned in self.root.glob("*.tmp"):
                abandoned.unlink(missing_ok=True)

    def _path(self, kind: str, identity: str | int) -> Path:
        value = str(identity)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("invalid identity")
        return self.root / f"{kind}-{value}.enc"

    def get(self, kind: str, identity: str | int) -> dict[str, Any] | None:
        path = self._path(kind, identity)
        with self.lock:
            if not path.exists():
                return None
            data = path.read_bytes()
            return json.loads(self.cipher.decrypt(data[:12], data[12:], path.name.encode()))

    def put(self, kind: str, identity: str | int, record: dict[str, Any]) -> None:
        path = self._path(kind, identity)
        with self.lock:
            nonce = secrets.token_bytes(12)
            data = nonce + self.cipher.encrypt(nonce, json.dumps(record, ensure_ascii=False).encode(), path.name.encode())
            temp = path.with_suffix(f".{secrets.token_hex(8)}.tmp")
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as file:
                    file.write(data)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)

    def delete(self, kind: str, identity: str | int) -> None:
        with self.lock:
            self._path(kind, identity).unlink(missing_ok=True)

    def all(self, kind: str) -> list[dict[str, Any]]:
        with self.lock:
            return [self.get(kind, p.stem[len(kind) + 1:]) for p in self.root.glob(f"{kind}-*.enc")]
