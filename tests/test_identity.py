"""Authenticated self-identity and cookie scope regressions; no TikTok traffic."""
import time
from types import SimpleNamespace

import pytest
import requests

from terex_tiktok.identity import Identity, SessionRejected, _identity, authenticated_session, checked_cookies, verify_identity


def cookies():
    return [
        {"name": "sessionid", "value": "owner", "domain": ".tiktok.com", "path": "/", "secure": True},
        {"name": "sessionid", "value": "studio-only", "domain": "www.tiktok.com", "path": "/tiktokstudio"},
        {"name": "sessionid", "value": "other-host", "domain": "shop.tiktok.com", "path": "/"},
        {"name": "expired", "value": "old", "domain": ".tiktok.com", "path": "/", "expires": time.time() - 60},
    ]


def test_chrome_identity_keeps_cookie_scope_expiry_and_selected_route(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://ambient.invalid:8080")
    def get(url, **kwargs):
        jar = kwargs["cookies"]
        assert not isinstance(jar, dict), "same-name scoped cookies must not collapse"
        with requests.Session() as session:
            session.cookies = jar
            header = session.prepare_request(requests.Request("GET", url)).headers["Cookie"]
        assert header == "sessionid=owner"
        assert len(list(jar)) == 4
        assert kwargs["allow_redirects"] is False
        assert kwargs["proxies"] is None
        return SimpleNamespace(status_code=200, json=lambda: {
            "message": "success", "data": {"user_id_str": "100", "username": "alice"}})
    monkeypatch.setattr("autotok.impersonate.impersonated_get", get)
    monkeypatch.setattr(requests.Session, "get", lambda *a, **kw: SimpleNamespace(
        status_code=200, json=lambda: {"message": "error", "data": {"error_code": 13}}))
    assert verify_identity(cookies(), "test browser", None) == Identity("100", "alice", "alice")
    with authenticated_session(cookies(), "test browser") as session:
        assert session.trust_env is False and not session.proxies
        assert session.prepare_request(requests.Request("GET", "https://www.tiktok.com/passport/web/account/info/")).headers["Cookie"] == "sessionid=owner"


@pytest.mark.parametrize("body", [
    {"message": "success", "data": {"error_code": 13, "user_id_str": "100", "username": "alice"}},
    {"message": "success", "status_code": 13, "data": {"user_id_str": "100", "username": "alice"}},
    {"data": {"user_id_str": "100", "username": "alice"}},
    {"message": "success", "data": {"user": []}},
    {"message": "success", "data": {"user_id_str": True, "username": {"cookie": "secret"}}},
    ["not an account response"],
])
def test_only_explicit_success_with_valid_self_identity_is_accepted(body):
    assert _identity(body, False, "requests") is None


def test_refused_session_diagnostics_never_echo_remote_text_or_network_secrets(monkeypatch, caplog):
    secret = "session-cookie-or-proxy-secret"
    body = {"message": secret, "data": {"error_code": secret, "description": secret}}
    monkeypatch.setattr(requests.Session, "get", lambda *a, **kw: SimpleNamespace(status_code=200, json=lambda: body))
    def failed(*args, **kwargs):
        raise requests.ConnectionError(secret)
    monkeypatch.setattr("autotok.impersonate.impersonated_get", failed)
    with pytest.raises(SessionRejected):
        verify_identity(cookies(), "test browser")
    assert secret not in caplog.text


def test_partitioned_cookie_import_cannot_become_unpartitioned():
    with pytest.raises(ValueError, match="[Pp]artitioned"):
        checked_cookies([{**cookies()[0], "partitionKey": {"topLevelSite": "https://tiktok.com"}}])
