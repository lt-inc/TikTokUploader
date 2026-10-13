from __future__ import annotations

import secrets
import time
from datetime import datetime, timezone
from typing import Callable

from .config import Config
from .identity import Identity, SessionRejected, checked_cookies, verify_identity
from .store import EncryptedStore


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Connections:
    def __init__(self, store: EncryptedStore, config: Config, verify: Callable = verify_identity):
        self.store, self.config, self.verify = store, config, verify

    def start(self, account_id: int, pairing_token: str, expires_at: str,
              expected_remote_account_id: str | None = None) -> dict:
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp()
        if not 16 <= len(pairing_token) <= 256 or not time.time() < expiry <= time.time() + 601:
            raise ValueError("Pairing token must expire within ten minutes")
        with self.store.lock:
            row = self.store.get("account", account_id) or {"account_id": account_id, "status": "disconnected", "session_version": 0}
            existing = row.get("remote_account_id")
            if existing and expected_remote_account_id and existing != expected_remote_account_id:
                raise ValueError("The existing account identity cannot be replaced")
            row.update(pairing_token=pairing_token, pairing_expires_at=expiry,
                       expected_remote_account_id=expected_remote_account_id or existing,
                       pairing_generation=row.get("pairing_generation", 0) + 1, status="pairing", error=None)
            self.store.put("account", account_id, row)
            return self.public(row)

    def receive(self, pairing_token: str, cookies: list[dict], user_agent: str, origin: str) -> dict:
        if origin != "https://www.tiktok.com":
            raise ValueError("Export must originate from www.tiktok.com")
        if not isinstance(user_agent, str) or not 10 <= len(user_agent) <= 1024:
            raise ValueError("The real browser user agent is required")
        normalized = checked_cookies(cookies)
        with self.store.lock:
            row = next((r for r in self.store.all("account")
                        if r.get("pairing_token") and secrets.compare_digest(r["pairing_token"], pairing_token)), None)
            if not row or row.get("pairing_expires_at", 0) <= time.time():
                raise ValueError("Pairing token is expired or already used")
            # Consume once before remote verification. Interrupted verification is
            # recoverable through check/browser help, never a second account bind.
            row.pop("pairing_token", None)
            row.update(pending_cookies=normalized, pending_user_agent=user_agent, status="challenge")
            self.store.put("account", row["account_id"], row)
        return self.check(row["account_id"])

    def check(self, account_id: int) -> dict:
        row = self.store.get("account", account_id)
        if not row or row["status"] == "disconnected":
            raise ValueError("Account is not connected")
        cookies = row.get("pending_cookies", row.get("cookies"))
        agent = row.get("pending_user_agent", row.get("user_agent"))
        if not cookies or not agent:
            return self.public(row)
        try:
            identity = self.verify(cookies, agent, self.config.proxy_url)
        except SessionRejected as exc:
            row.update(status="needs_reauth", error=str(exc))
            self.store.put("account", account_id, row)
            return self.public(row)
        # A connection exported from the extension may need server-side browser
        # verification; preserve its encrypted payload for the helper, not logs.
        return self.accept(account_id, cookies, agent, identity, row.get("session_version", 0), row.get("pairing_generation", 0))

    def accept(self, account_id: int, cookies: list[dict], agent: str, identity: Identity, expected_version: int,
               expected_pairing_generation: int | None = None) -> dict:
        with self.store.lock:
            row = self.store.get("account", account_id)
            if not row or row["status"] == "disconnected" or row.get("session_version", 0) != expected_version:
                raise ValueError("Connection changed while checking the session")
            if expected_pairing_generation is not None and row.get("pairing_generation", 0) != expected_pairing_generation:
                raise ValueError("A newer connection request replaced this verification")
            expected = row.get("remote_account_id") or row.get("expected_remote_account_id")
            if expected and str(expected) != identity.remote_account_id:
                row.update(status="needs_reauth", error="A different TikTok account is logged in. Connect the original account.")
                row.pop("pending_cookies", None)
                row.pop("pending_user_agent", None)
                self.store.put("account", account_id, row)
                raise ValueError(row["error"])
            changed = row.get("cookies") != cookies or row.get("user_agent") != agent or not row.get("session_ref")
            row.update(cookies=cookies, user_agent=agent, status="connected", error=None,
                       remote_account_id=identity.remote_account_id, username=identity.username,
                       display_name=identity.display_name, verified_at=iso_now())
            if changed:
                row["session_version"] = expected_version + 1
                row["session_ref"] = f"tiktok:{account_id}:{row['session_version']}"
            row.pop("pending_cookies", None)
            row.pop("pending_user_agent", None)
            row.pop("pairing_token", None)
            row.pop("pairing_expires_at", None)
            self.store.put("account", account_id, row)
            return self.public(row)

    def disconnect(self, account_id: int) -> dict:
        with self.store.lock:
            row = self.store.get("account", account_id) or {"account_id": account_id, "session_version": 0}
            row = {"account_id": account_id, "status": "disconnected", "session_version": row.get("session_version", 0) + 1,
                   "remote_account_id": row.get("remote_account_id")}
            self.store.put("account", account_id, row)
            return self.public(row)

    @staticmethod
    def public(row: dict) -> dict:
        keys = ("account_id", "status", "remote_account_id", "username", "display_name", "session_ref", "session_version", "error", "help_url")
        reply = {k: row.get(k) for k in keys}
        if row.get("pairing_expires_at"):
            reply["pairing_expires_at"] = datetime.fromtimestamp(row["pairing_expires_at"], timezone.utc).isoformat()
        return reply

    def require(self, account_id: int, version: int, remote_id: str) -> dict:
        row = self.store.get("account", account_id)
        if not row or row.get("status") != "connected" or row.get("session_version") != version or row.get("remote_account_id") != remote_id:
            raise SessionRejected("TikTok session identity/version changed; publishing is fenced")
        return row
