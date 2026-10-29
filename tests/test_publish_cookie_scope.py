from types import SimpleNamespace

import requests

from autotok.accounts import Account
from autotok.uploader import Client


def test_publish_signs_the_first_ms_token_applicable_to_the_actual_post_url():
    signed = []

    class Signer:
        def sign(self, url):
            signed.append(url)
            return SimpleNamespace(x_bogus="test-bogus", signature="test-signature")

    account = Account("account-24", cookies=[
        {"name": "sessionid", "value": "test-session", "domain": ".tiktok.com", "path": "/"}])
    client = Client(account, signer=Signer())
    with requests.Session() as session:
        for domain, path, value, expiry in (
            (".tiktok.com", "/", "root-token", None),
            ("www.tiktok.com", "/tiktok/web/project/post/", "post-token", None),
            ("www.tiktok.com", "/api/v1/", "wrong-path-token", None),
            ("shop.tiktok.com", "/", "wrong-host-token", None),
            ("www.tiktok.com", "/", "expired-token", 1),
        ):
            session.cookies.set("msToken", value, domain=domain, path=path, expires=expiry, secure=True)
        count = len(session.cookies)
        url, params, _ = client._prepare_publish_request(session)
        wire_cookies = session.prepare_request(requests.Request("POST", url)).headers["Cookie"]
        assert wire_cookies.split(";", 1)[0] == "msToken=post-token"
        assert params["msToken"] == "post-token"
        assert "msToken=post-token" in signed[0]
        assert "wrong-" not in wire_cookies and "expired-token" not in wire_cookies
        assert len(session.cookies) == count
