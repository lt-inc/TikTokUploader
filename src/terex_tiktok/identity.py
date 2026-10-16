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
        data = body.get("data", {})
        if body.get("message") not in (None, "success") or not isinstance(data, dict):
            # TikTok's own status fields only; never the cookies or the response body.
            fields = data if isinstance(data, dict) else {}
            log.warning("identity check not authenticated message=%r error_code=%r description=%r via_proxy=%s",
                        str(body.get("message"))[:80], fields.get("error_code"), str(fields.get("description"))[:120], bool(proxy))
            raise SessionRejected("TikTok did not confirm an authenticated account")
        user = data.get("user", data.get("user_info", data))
        uid = user.get("user_id_str", user.get("user_id", user.get("uid")))
        username = user.get("username", user.get("screen_name", user.get("unique_id")))
        if not uid or not username:
            raise SessionRejected("TikTok did not return a verified account identity")
        return Identity(str(uid), str(username), str(user.get("name", user.get("nickname", username))))
