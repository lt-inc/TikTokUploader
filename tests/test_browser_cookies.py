from types import SimpleNamespace
from contextlib import nullcontext
import threading
import time

import pytest

from terex_tiktok import help as browser_help
from terex_tiktok.browser import session_cookies
from terex_tiktok.connections import Connections
from terex_tiktok.help import BrowserHelp
from terex_tiktok.identity import Identity, checked_cookies
from terex_tiktok.store import EncryptedStore

from test_critical import config


def test_browser_session_excludes_regional_and_third_party_cookies():
    own = [
        {"name": "sessionid", "value": "test-session", "domain": ".tiktok.com", "path": "/", "httpOnly": True},
        {"name": "msToken", "value": "scoped", "domain": "www.tiktok.com", "path": "/tiktokstudio", "expires": 2000000000},
    ]
    foreign = [
        {"name": "regional", "value": "test", "domain": ".tiktokw.eu"},
        {"name": "third-party", "value": "test", "domain": ".example.com", "partitionKey": "https://www.tiktok.com"},
        {"name": "sessionid", "value": "other", "domain": ".evil-tiktok.com"},
    ]
    context = SimpleNamespace(cookies=lambda: own + foreign)
    assert session_cookies(context) == checked_cookies(own)
    with pytest.raises(ValueError, match="Only tiktok.com"):
        checked_cookies(own + foreign)
    with pytest.raises(ValueError, match="session cookie is missing"):
        session_cookies(SimpleNamespace(cookies=lambda: own[1:] + foreign))
    with pytest.raises(ValueError, match="Partitioned"):
        session_cookies(SimpleNamespace(cookies=lambda: [{**own[0], "partitionKey": "https://www.tiktok.com"}]))


@pytest.mark.parametrize("publication", [False, True])
def test_both_verification_paths_accept_browser_with_regional_cookie(tmp_path, monkeypatch, publication):
    cfg = config(tmp_path)
    store = EncryptedStore(cfg.state_dir, cfg.encryption_key)
    own = [{"name": "sessionid", "value": "test-session", "domain": ".tiktok.com", "path": "/"}]
    row = {"account_id": 1, "status": "connected", "session_version": 1, "remote_account_id": "100",
           "user_agent": "test browser agent", "cookies": own}
    store.put("account", 1, row)
    connections = Connections(store, cfg)
    helper = BrowserHelp(connections)
    page = SimpleNamespace(
        url="https://www.tiktok.com/tiktokstudio/upload",
        goto=lambda *args, **kwargs: None, wait_for_timeout=lambda *args: None,
        locator=lambda *args: SimpleNamespace(is_visible=lambda: False),
        evaluate=lambda *args: row["user_agent"],
    )
    context = SimpleNamespace(
        cookies=lambda: own + [{"name": "regional", "value": "test", "domain": ".tiktokw.eu"}],
        new_page=lambda: page, close=lambda: None,
    )
    monkeypatch.setattr(browser_help, "sync_playwright", lambda: nullcontext(None))
    monkeypatch.setattr(browser_help, "open_context", lambda *args, **kwargs: (SimpleNamespace(close=lambda: None), context))
    monkeypatch.setattr(browser_help, "verify_identity", lambda *args: Identity("100", "alice", "Alice"))
    monkeypatch.setattr(browser_help.CaptchaProvider, "try_challenge", lambda *args: False)
    if publication:
        helper.verify_for_publish(row, lambda: None, lambda: pytest.fail("No challenge is visible"))
    else:
        session = {"account_id": 1, "version": 1, "pairing_generation": 0, "expires_at": time.time() + 60,
                   "stop": threading.Event(), "finish": threading.Event(), "ready": threading.Event()}
        session["finish"].set()
        helper.sessions["test"] = session
        helper._browser("test", session, row)
        assert session["status"] == "completed"
    saved = store.get("account", 1)
    assert saved["status"] == "connected" and saved["remote_account_id"] == "100"
    assert saved["cookies"] == checked_cookies(own)
