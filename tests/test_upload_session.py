"""Session continuity across the two HTTP clients; no TikTok requests."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import requests

from autotok.accounts import Account
from autotok.impersonate import impersonated_get
from autotok.uploader import Client


def test_chrome_transport_preserves_session_scope_rotation_and_deletion():
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(self.headers.get("Cookie", ""))
            self.send_response(200)
            self.send_header("Set-Cookie", "sessionid=rotated; Path=/top; HttpOnly")
            self.send_header("Set-Cookie", "obsolete=; Path=/top; Max-Age=0")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with requests.Session() as session:
            jar = session.cookies
            jar.set("sessionid", "upload-owner", domain="127.0.0.1", path="/top")
            jar.set("sessionid", "other-path-owner", domain="127.0.0.1", path="/passport")
            jar.set("sessionid", "other-domain-owner", domain="example.test", path="/top")
            jar.set("obsolete", "old", domain="127.0.0.1", path="/top")
            url = f"http://127.0.0.1:{server.server_port}/top/v1"
            response = impersonated_get(url, headers={}, cookies=jar, proxies=None,
                                        timeout=5, allow_redirects=False)
            assert response.status_code == 200
            assert "sessionid=upload-owner" in observed[0]
            assert "other-path-owner" not in observed[0]
            assert "other-domain-owner" not in observed[0]
            # The next requests-based call must use the session updated by curl.
            next_request = session.prepare_request(requests.Request("GET", url))
            assert "sessionid=rotated" in next_request.headers["Cookie"]
            assert "obsolete=" not in next_request.headers["Cookie"]
            assert jar.get("sessionid", domain="127.0.0.1", path="/passport") == "other-path-owner"
            assert jar.get("sessionid", domain="example.test", path="/top") == "other-domain-owner"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_apply_keeps_verified_cookie_jar_and_does_not_follow_redirects(monkeypatch):
    account = Account(name="test", cookies=[
        {"name": "sessionid", "value": "owner", "domain": ".tiktok.com", "path": "/"},
    ], user_agent="test browser")
    client = Client(account, proxy=None)
    with client._session() as session:
        session.cookies.set("sessionid", "passport-only", domain="www.tiktok.com", path="/passport")

        def get(url, *, cookies, allow_redirects, **kwargs):
            assert cookies is session.cookies
            assert allow_redirects is False
            assert cookies.get("sessionid", domain=".tiktok.com", path="/") == "owner"
            assert cookies.get("sessionid", domain="www.tiktok.com", path="/passport") == "passport-only"
            response = requests.Response()
            response.status_code = 200
            return response

        monkeypatch.setattr("autotok.uploader.impersonated_get", get)
        client._apply_request(session, "https://www.tiktok.com/top/v1?Action=ApplyUploadInner",
                              {"access_key": "test", "secret_key": "test", "session_token": "test"})
