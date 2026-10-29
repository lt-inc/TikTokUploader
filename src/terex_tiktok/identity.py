from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import requests

log = logging.getLogger("tiktok.identity")
IDENTITY_URL = "https://www.tiktok.com/passport/web/account/info/"


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
        if not isinstance(cookie, dict):
            raise ValueError("Invalid cookie")
        if cookie.get("partitionKey") is not None:
            # Neither HTTP client can reproduce a browser cookie partition.
            raise ValueError("Partitioned TikTok cookies cannot be imported; use browser verification")
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
        if type(expiry) in (int, float) and math.isfinite(expiry) and expiry > 0:
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
        expiry = c.get("expires", c.get("expirationDate"))
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"),
                            secure=c.get("secure", True),
                            expires=int(expiry) if type(expiry) in (int, float) and math.isfinite(expiry) and expiry > 0 else None)
    return session


def _identity(body: object, via_proxy: bool, client: str) -> Identity | None:
    """Accept only a successful authenticated self-account API response."""
    fields = body if isinstance(body, dict) else {}
    data = fields.get("data")
    data = data if isinstance(data, dict) else {}
    codes = [obj[key] for obj in (fields, data) for key in ("error_code", "status_code") if key in obj]
    if fields.get("message") != "success" or any(type(code) not in (int, str) or code not in (0, "0") for code in codes):
        # Even server error text can echo credentials. Log bounded numeric codes,
        # never message/description, response bodies or exception text.
        code = next((int(c) for c in codes if type(c) is int and 0 < c <= 999999), None)
        log.warning("identity check not authenticated client=%s error_code=%s via_proxy=%s", client, code, via_proxy)
        return None
    user = data.get("user", data.get("user_info", data))
    if not isinstance(user, dict):
        return None
    uid = user.get("user_id_str", user.get("user_id", user.get("uid")))
    username = user.get("username", user.get("screen_name", user.get("unique_id")))
    if (type(uid) not in (str, int) or not str(uid).isascii() or not str(uid).isdigit()
            or not 0 < len(str(uid)) <= 32 or int(uid) <= 0
            or not isinstance(username, str) or not username.strip() or len(username) > 128):
        log.warning("identity check missing self identity client=%s via_proxy=%s", client, via_proxy)
        return None
    name = user.get("name", user.get("nickname", username))
    return Identity(str(uid), username, name if isinstance(name, str) and len(name) <= 256 else username)


def _chrome_identity(cookies: list[dict], user_agent: str, proxy: str | None) -> Identity | None:
    """Same check over a Chrome TLS/HTTP2 fingerprint; TikTok may refuse the plain requests client."""
    from autotok.impersonate import impersonated_get
    from autotok.proxy import parse_proxy

    parsed = parse_proxy(proxy)
    try:
        with authenticated_session(cookies, user_agent, proxy) as session:
            # A dictionary would overwrite same-name cookies from other scopes
            # and send unrelated/expired cookies to this authenticated endpoint.
            response = impersonated_get(f"{IDENTITY_URL}?aid=1459", headers=dict(session.headers),
                                        cookies=session.cookies, proxies=parsed.for_requests() if parsed else None,
                                        timeout=(10, 20), allow_redirects=False)
        return _identity(response.json(), bool(proxy), "chrome") if response.status_code == 200 else None
    except SessionRejected:
        raise
    except Exception as exc:
        log.warning("identity check with the Chrome client failed error_type=%s", type(exc).__name__)
        return None


def verify_identity(cookies: list[dict], user_agent: str, proxy: str | None = None) -> Identity:
    with authenticated_session(cookies, user_agent, proxy) as session:
        response = session.get(IDENTITY_URL, params={"aid": 1459}, timeout=(10, 20), allow_redirects=False)
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
