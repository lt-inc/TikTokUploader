"""Chrome-impersonating HTTP and AWS SigV4 signing for TikTok's upload gateway.

TikTok answers ``requests``' TLS/HTTP1 client with CodeN 30411 on ApplyUploadInner
while the same request from Chrome succeeds. ``curl_cffi`` reproduces Chrome's
TLS and HTTP/2 fingerprints. It only supports Basic ``auth``, so SigV4 is
implemented here: for GET requests, ``host``, ``x-amz-date`` and
``x-amz-security-token`` are signed, as the browser does.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlsplit

IMPERSONATE = "chrome"  # curl_cffi alias for its newest Chrome profile


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def sigv4_headers(method: str, url: str, *, service: str, region: str, access_key: str,
                  secret_key: str, session_token: str, now: datetime | None = None) -> dict[str, str]:
    """Return the x-amz-date, x-amz-security-token and Authorization headers for a bodyless request."""
    parts = urlsplit(url)
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    amzdate, datestamp = moment.strftime("%Y%m%dT%H%M%SZ"), moment.strftime("%Y%m%d")
    # Values are percent-encoded from their decoded form, the way the server canonicalizes them.
    query = "&".join(f"{k}={v}" for k, v in sorted(
        (quote(k, safe="-_.~"), quote(v, safe="-_.~")) for k, v in parse_qsl(parts.query, keep_blank_values=True)))
    signed = {"host": parts.netloc, "x-amz-date": amzdate, "x-amz-security-token": session_token}
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in sorted(signed.items()))
    signed_names = ";".join(sorted(signed))
    canonical = "\n".join([method, parts.path or "/", query, canonical_headers, signed_names,
                           hashlib.sha256(b"").hexdigest()])
    scope = f"{datestamp}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = _hmac(_hmac(_hmac(_hmac(("AWS4" + secret_key).encode(), datestamp), region), service), "aws4_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "x-amz-date": amzdate,
        "x-amz-security-token": session_token,
        "Authorization": f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_names}, Signature={signature}",
    }


def impersonated_get(url: str, *, headers: dict[str, str], cookies: dict[str, str],
                     proxies: dict[str, str] | None, timeout: float):
    """GET with Chrome's TLS and HTTP/2 fingerprint. The result has status_code, text and json()."""
    from curl_cffi import requests as curl

    with curl.Session(impersonate=IMPERSONATE, trust_env=False) as session:
        return session.get(url, headers=headers, cookies=cookies, proxies=proxies, timeout=timeout)
