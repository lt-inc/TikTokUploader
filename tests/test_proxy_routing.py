from __future__ import annotations

import json

import pytest

from autotok.uploader import Client
from terex_tiktok.config import Config
from terex_tiktok.connections import Connections
from terex_tiktok.identity import Identity, SessionRejected
from terex_tiktok.store import EncryptedStore


GLOBAL_PROXY = "http://global-user:global-password@global.example:8080"
CUSTOM_PROXY = "http://custom-user:custom-password@account.example:8081"


@pytest.fixture
def connection(tmp_path):
    cfg = Config("test-token", b"k" * 32, "http://main", "worker-token", "amqp://localhost", "https://admin.example",
                 tmp_path / "state", tmp_path / "scratch", proxy_url=GLOBAL_PROXY)
    store = EncryptedStore(cfg.state_dir, cfg.encryption_key)
    store.put("account", 18, {"account_id": 18, "remote_account_id": "100", "status": "connected",
        "session_version": 7, "session_ref": "tiktok:18:7", "user_agent": "real browser agent", "username": "alice",
        "cookies": [{"name": "sessionid", "value": "own-session-secret", "domain": ".tiktok.com", "path": "/"}]})
    observed = []
    def verify(cookies, agent, proxy):
        observed.append(proxy)
        return Identity("100", "alice", "Alice")
    service = Connections(store, cfg, verify=verify)
    return service, store, cfg, observed


def test_identity_checks_use_default_direct_and_saved_account_route(connection):
    from terex_tiktok.routing import proxy_for
    service, store, cfg, observed = connection
    service.check(18)
    service.set_proxy(18, "direct")
    assert proxy_for(store.get("account", 18), cfg) is None
    service.check(18)
    service.set_proxy(18, "custom", CUSTOM_PROXY)
    service.check(18)
    service.set_proxy(18, "default")
    service.check(18)
    assert observed == [GLOBAL_PROXY, None, CUSTOM_PROXY, GLOBAL_PROXY]
    # Turning an override off does not destroy the credential needed to reuse it.
    service.set_proxy(18, "custom")
    service.check(18)
    assert observed[-1] == CUSTOM_PROXY
    assert store.get("account", 18)["remote_account_id"] == "100"


def test_explicit_direct_route_does_not_inherit_process_proxy(monkeypatch):
    from autotok.proxy import proxied_session
    monkeypatch.setenv("HTTP_PROXY", GLOBAL_PROXY)
    monkeypatch.setenv("HTTPS_PROXY", GLOBAL_PROXY)
    with proxied_session(None) as session:
        assert session.trust_env is False
        settings = session.merge_environment_settings("https://www.tiktok.com/", {}, None, None, None)
        assert not settings["proxies"]


def test_proxy_credentials_are_encrypted_and_never_returned_in_connection_status(connection, caplog):
    service, store, cfg, _ = connection
    result = service.set_proxy(18, "custom", CUSTOM_PROXY)
    assert result["proxy_mode"] == "custom"
    assert result["proxy_display"] in ("account.example:8081", "http://account.example:8081")
    assert result["custom_proxy_configured"] is True
    for public in (result, service.public(store.get("account", 18))):
        encoded = json.dumps(public)
        assert "proxy_url" not in public
        assert "custom-user" not in encoded and "custom-password" not in encoded
        assert "global-password" not in encoded
    persisted = EncryptedStore(cfg.state_dir, cfg.encryption_key).get("account", 18)
    assert persisted["proxy_mode"] == "custom" and persisted["proxy_url"] == CUSTOM_PROXY
    for encrypted in cfg.state_dir.glob("*.enc"):
        assert b"custom-password" not in encrypted.read_bytes()
    assert "custom-password" not in caplog.text and "custom-user" not in caplog.text


def test_proxy_change_between_checkpoint_and_final_send_fences_old_attempt(connection):
    service, store, _, _ = connection
    old = store.get("account", 18)
    client = object.__new__(Client)
    client._guard = lambda: service.require(18, old["session_version"], old["remote_account_id"])
    sent = []
    client._submit_publish_request = lambda *args: sent.append("publish")
    with pytest.raises(SessionRejected, match="fenced"):
        client.submit({"session": object(), "payload": {}, "request": ()},
                      lambda: service.set_proxy(18, "custom", CUSTOM_PROXY))
    current = store.get("account", 18)
    assert sent == [] and current["session_version"] > old["session_version"]
    assert current["status"] == "needs_reauth"
    assert current["cookies"] == old["cookies"] and current["remote_account_id"] == old["remote_account_id"]


def test_stale_verification_rejection_cannot_restore_previous_proxy_or_session(connection):
    service, store, _, _ = connection
    service.set_proxy(18, "custom", CUSTOM_PROXY)
    service.check(18)
    captured = {}
    def stale_verification(cookies, agent, proxy):
        assert proxy == CUSTOM_PROXY
        service.set_proxy(18, "direct")
        captured.update(store.get("account", 18))
        raise SessionRejected("The previous route refused this session")
    service.verify = stale_verification
    try:
        service.check(18)
    except ValueError:
        # A concurrent setting change may be returned as a conflict or as the
        # latest public status; neither permits writing the stale snapshot back.
        pass
    current = store.get("account", 18)
    assert current == captured
    assert current["proxy_mode"] == "direct" and current["proxy_url"] == CUSTOM_PROXY


def test_disconnect_preserves_disabled_custom_proxy_for_later_reuse(connection):
    from terex_tiktok.routing import proxy_for
    service, store, cfg, _ = connection
    service.set_proxy(18, "custom", CUSTOM_PROXY)
    service.set_proxy(18, "direct")
    result = service.disconnect(18)
    assert result["status"] == "disconnected" and result["proxy_mode"] == "direct"
    assert result["custom_proxy_configured"] is True
    assert proxy_for(store.get("account", 18), cfg) is None
    result = service.set_proxy(18, "custom")
    assert result["status"] == "disconnected"
    assert proxy_for(store.get("account", 18), cfg) == CUSTOM_PROXY
    assert store.get("account", 18)["remote_account_id"] == "100"


def test_pending_session_is_preserved_but_fenced_when_route_changes(connection):
    service, store, _, _ = connection
    row = store.get("account", 18)
    row.update(status="challenge", pending_cookies=row["cookies"], pending_user_agent="pending browser agent")
    store.put("account", 18, row)
    service.set_proxy(18, "direct")
    changed = store.get("account", 18)
    assert changed["status"] == "needs_reauth" and changed["session_version"] > row["session_version"]
    assert changed["pending_cookies"] == row["pending_cookies"]
    assert changed["remote_account_id"] == row["remote_account_id"]


def test_invalid_custom_proxy_fails_without_secret_disclosure_or_global_fallback(connection, caplog):
    from terex_tiktok.routing import proxy_for
    service, store, cfg, _ = connection
    before = store.get("account", 18)
    invalid = "http://private-user:private-password@account.example:not-a-port"
    with pytest.raises(ValueError) as rejected:
        service.set_proxy(18, "custom", invalid)
    assert "private-user" not in str(rejected.value) and "private-password" not in str(rejected.value)
    assert store.get("account", 18) == before
    assert "private-user" not in caplog.text and "private-password" not in caplog.text
    damaged = {**before, "proxy_mode": "custom", "proxy_url": invalid}
    with pytest.raises(ValueError):
        proxy_for(damaged, cfg)
