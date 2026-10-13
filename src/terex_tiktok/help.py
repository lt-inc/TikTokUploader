from __future__ import annotations

import secrets
import contextlib
import threading
import time
from datetime import datetime, timezone

from autotok.browsers import sync_playwright

from .browser import browser_permit, open_context
from .captcha import CaptchaProvider
from .connections import Connections
from .identity import checked_cookies, verify_identity
from .metrics import HELP


class BrowserHelp:
    def __init__(self, connections: Connections):
        self.connections = connections
        self.config = connections.config
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}

    def start(self, account_id: int) -> dict:
        row = self.connections.store.get("account", account_id)
        if not row or row.get("status") == "disconnected":
            raise ValueError("Connect/export the account before opening browser help")
        with self.lock:
            if any(s["expires_at"] > time.time() and not s["stop"].is_set() for s in self.sessions.values()):
                raise ValueError("Another operator browser is open; close it first")
            token = secrets.token_urlsafe(32)
            session = {"account_id": account_id, "expires_at": time.time() + 600,
                       "version": row.get("session_version", 0), "pairing_generation": row.get("pairing_generation", 0),
                       "status": "starting", "stop": threading.Event(),
                       "finish": threading.Event(), "ready": threading.Event(), "error": None}
            self.sessions[token] = session
            thread = threading.Thread(target=self._browser, args=(token, session, row), daemon=True)
            thread.start()
        reply = self.connections.public(row)
        reply.update(help_url=f"{self.config.public_url}/api/tiktok/browser-help/{token}/", status="challenge",
                     expires_at=datetime.fromtimestamp(session["expires_at"], timezone.utc).isoformat())
        return reply

    def require(self, token: str) -> dict:
        with self.lock:
            session = self.sessions.get(token)
            if not session or session["expires_at"] <= time.time() or session["stop"].is_set():
                raise ValueError("Browser help expired or was closed")
            row = self.connections.store.get("account", session["account_id"])
            if not row or row.get("status") == "disconnected" or row.get("session_version", 0) != session["version"] or row.get("pairing_generation", 0) != session["pairing_generation"]:
                # A successfully completed session may expose its final state
                # briefly, but never an interactive socket after version change.
                if session["status"] != "completed":
                    session["stop"].set()
                    raise ValueError("Account connection changed; browser help closed")
            return session

    def cancel_account(self, account_id: int):
        with self.lock:
            for session in self.sessions.values():
                if session["account_id"] == account_id:
                    session["stop"].set()

    def _browser(self, token: str, session: dict, row: dict):
        try:
            with browser_permit(), sync_playwright() as pw:
                HELP.inc()
                browser, context = open_context(pw, row, self.config.proxy_url, headless=False)
                try:
                    page = context.new_page()
                    page.goto("https://www.tiktok.com/tiktokstudio/upload", wait_until="domcontentloaded", timeout=30000)
                    # A detector/selector/provider failure always leaves this
                    # same browser available to the operator.
                    with contextlib.suppress(Exception):
                        CaptchaProvider(self.config).try_challenge(page)
                    session["status"] = "active"
                    session["ready"].set()
                    while time.time() < session["expires_at"] and not session["stop"].is_set():
                        self.require(token)
                        if session["finish"].is_set():
                            cookies = checked_cookies(context.cookies())
                            agent = page.evaluate("navigator.userAgent")
                            identity = verify_identity(cookies, agent, self.config.proxy_url)
                            self.connections.accept(session["account_id"], cookies, agent, identity, session["version"], session["pairing_generation"])
                            session["status"] = "completed"
                            break
                        page.wait_for_timeout(500)
                    if session["status"] != "completed":
                        session["status"] = "expired"
                finally:
                    context.close()
                    browser.close()
                    HELP.dec()
        except Exception as exc:
            session.update(status="failed", error=f"Browser verification failed ({type(exc).__name__}); reconnect or try again")
            session["ready"].set()
