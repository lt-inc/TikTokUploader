from __future__ import annotations

from dataclasses import dataclass
from http.cookiejar import Cookie
from urllib.parse import urlparse

import logging

import requests

log = logging.getLogger("tiktok.identity")


class SessionRejected(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    remote_account_id: str
    username: str
    display_name: str


def checked_cookies(cookies: list[dict]) -> list[dict]:
    if not 1 <= len(cookies) <= 200:
        raise ValueError("Expected 1–200 TikTok cookies")
    result = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if domain != "tiktok.com" and not domain.endswith(".tiktok.com"):
            raise ValueError("Only tiktok.com cookies are accepted")
        name, value = cookie.get("name"), cookie.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or len(name) > 256 or len(value) > 16384:
            raise ValueError("Invalid cookie")
        normalized = {"name": name, "value": value, "domain": cookie["domain"], "path": cookie.get("path", "/"),
                      "secure": bool(cookie.get("secure", True)), "httpOnly": bool(cookie.get("httpOnly", False)),
                      "sameSite": {"no_restriction": "None", "lax": "Lax", "strict": "Strict"}.get(cookie.get("sameSite"), cookie.get("sameSite", "Lax"))}
        if normalized["sameSite"] not in ("Strict", "Lax", "None"):
            normalized["sameSite"] = "Lax"
        expiry = cookie.get("expirationDate", cookie.get("expires"))
        if isinstance(expiry, (int, float)) and expiry > 0:
            normalized["expires"] = expiry
        result.append(normalized)
    if not any(c["name"] == "sessionid" and c["value"] for c in result):
        raise ValueError("The logged-in TikTok session cookie is missing")
    return result


def authenticated_session(cookies: list[dict], user_agent: str, proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    session.headers.update({"User-Agent": user_agent, "Referer": "https://www.tiktok.com/", "Accept": "application/json"})
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"), secure=c.get("secure", True))
    return session


def _identity(body: dict, via_proxy: bool, client: str) -> Identity | None:
    """The verified identity, or None when TikTok says the session is not authenticated."""
    data = body.get("data", {})
    if body.get("message") not in (None, "success") or not isinstance(data, dict):
        # TikTok's own status fields only; never the cookies or the response body.
        fields = data if isinstance(data, dict) else {}
        log.warning("identity check not authenticated client=%s message=%r error_code=%r description=%r via_proxy=%s",
                    client, str(body.get("message"))[:80], fields.get("error_code"), str(fields.get("description"))[:120], via_proxy)
        return None
    user = data.get("user", data.get("user_info", data))
    uid = user.get("user_id_str", user.get("user_id", user.get("uid")))
    username = user.get("username", user.get("screen_name", user.get("unique_id")))
    if not uid or not username:
        raise SessionRejected("TikTok did not return a verified account identity")
    return Identity(str(uid), str(username), str(user.get("name", user.get("nickname", username))))


def _chrome_identity(cookies: list[dict], user_agent: str, proxy: str | None) -> Identity | None:
    """Same check over a Chrome TLS/HTTP2 fingerprint; TikTok may refuse the plain requests client."""
    from autotok.impersonate import impersonated_get
    from autotok.proxy import parse_proxy

    parsed = parse_proxy(proxy)
    jar = {c["name"]: c["value"] for c in cookies}
    try:
        response = impersonated_get("https://www.tiktok.com/passport/web/account/info/?aid=1459",
                                    headers={"User-Agent": user_agent, "Referer": "https://www.tiktok.com/", "Accept": "application/json"},
                                    cookies=jar, proxies=parsed.for_requests() if parsed else None, timeout=(10, 20), allow_redirects=False)
        return _identity(response.json(), bool(proxy), "chrome") if response.status_code == 200 else None
    except SessionRejected:
        raise
    except Exception as exc:
        log.warning("identity check with the Chrome client failed error_type=%s", type(exc).__name__)
        return None


def verify_identity(cookies: list[dict], user_agent: str, proxy: str | None = None) -> Identity:
    with authenticated_session(cookies, user_agent, proxy) as session:
        response = session.get("https://www.tiktok.com/passport/web/account/info/", params={"aid": 1459}, timeout=(10, 20), allow_redirects=False)
        if response.status_code != 200:
            log.warning("identity check rejected status=%s via_proxy=%s", response.status_code, bool(proxy))
            raise SessionRejected("TikTok did not accept the session; reconnect the account")
        try:
            body = response.json()
        except ValueError as exc:
            log.warning("identity check returned a non-JSON page via_proxy=%s", bool(proxy))
            raise SessionRejected("TikTok requested browser verification; reconnect or open browser help") from exc
    identity = _identity(body, bool(proxy), "requests")
    if identity is None:
        # The plain client may be refused although the browser session is valid.
        identity = _chrome_identity(cookies, user_agent, proxy)
        if identity is not None:
            log.info("identity confirmed by the Chrome client after the plain client was refused")
    if identity is None:
        raise SessionRejected("TikTok did not confirm an authenticated account")
    return identity
