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
from .identity import SessionRejected, checked_cookies, verify_identity
from .metrics import HELP
from .routing import proxy_for


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
            for token, active in self.sessions.items():
                if active["account_id"] == account_id and active.get("publication") and active["expires_at"] > time.time() and not active["stop"].is_set():
                    reply = self.connections.public(row)
                    reply.update(help_url=f"{self.config.public_url}/api/tiktok/browser-help/{token}/", status="challenge",
                                 expires_at=datetime.fromtimestamp(active["expires_at"], timezone.utc).isoformat())
                    return reply
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

    def verify_for_publish(self, row: dict, guard, on_wait) -> dict:
        """Pre-submit device checks keep the already transferred media intact.

        Browser operations stay on the publishing thread. HTTP helpers only
        signal finish/stop; they never call Playwright on a foreign thread.
        """
        with browser_permit(), sync_playwright() as pw:
            browser, context = open_context(pw, row, proxy_for(row, self.config), headless=False)
            token = None
            try:
                guard()
                page = context.new_page()
                page.goto("https://www.tiktok.com/tiktokstudio/upload", wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)
                challenge = page.locator("img#captcha-verify-image")
                pending = "/login" in page.url or challenge.is_visible()
                if pending:
                    with contextlib.suppress(Exception):
                        if CaptchaProvider(self.config).try_challenge(page):
                            pending = False
                if pending:
                    token = secrets.token_urlsafe(32)
                    session = {"account_id": row["account_id"], "expires_at": time.time() + 600,
                               "version": row["session_version"], "pairing_generation": row.get("pairing_generation", 0),
                               "status": "active", "stop": threading.Event(), "finish": threading.Event(),
                               "ready": threading.Event(), "error": None, "publication": True}
                    session["ready"].set()
                    with self.lock:
                        self.sessions[token] = session
                    HELP.inc()
                    on_wait()
                    while time.time() < session["expires_at"] and not session["stop"].is_set():
                        guard()
                        self.require(token)
                        if session["finish"].is_set():
                            break
                        page.wait_for_timeout(500)
                    if not session["finish"].is_set() or challenge.is_visible() or "/login" in page.url:
                        raise SessionRejected("Complete the TikTok verification before publishing")
                guard()
                cookies = checked_cookies(context.cookies())
                identity = verify_identity(cookies, row["user_agent"], proxy_for(row, self.config))
                if identity.remote_account_id != row["remote_account_id"]:
                    raise SessionRejected("A different TikTok account is logged in")
                with self.connections.store.lock:
                    current = self.connections.require(row["account_id"], row["session_version"], row["remote_account_id"])
                    # Verification enriches this connection's cookies; an
                    # exported/reconnected session still increments its version.
                    current["cookies"] = cookies
                    self.connections.store.put("account", row["account_id"], current)
                if token:
                    session["status"] = "completed"
                return current
            except Exception:
                if token:
                    session["status"] = "failed"
                raise
            finally:
                if token:
                    session["stop"].set()
                    HELP.dec()
                context.close()
                browser.close()

    def cancel_account(self, account_id: int):
        with self.lock:
            for session in self.sessions.values():
                if session["account_id"] == account_id:
                    session["stop"].set()

    def _browser(self, token: str, session: dict, row: dict):
        try:
            with browser_permit(), sync_playwright() as pw:
                HELP.inc()
                browser, context = open_context(pw, row, proxy_for(row, self.config), headless=False)
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
                            identity = verify_identity(cookies, agent, proxy_for(row, self.config))
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
