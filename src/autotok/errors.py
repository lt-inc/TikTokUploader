"""Exception hierarchy.

Every error raised on purpose by autotok derives from :class:`AutotokError`, so
callers can catch one type. ``retryable`` tells automation (the scheduler, your
own scripts) whether running the same upload again is safe: it is only ``True``
when the failure happened before anything was sent to TikTok's publish
endpoint, so a retry cannot create a duplicate post.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def _retry_after_seconds(value: str | None) -> int:
    """Interpret either HTTP Retry-After form without shortening the server's wait."""
    if not value:
        return 0
    value = str(value).strip()
    if value.isascii() and value.isdigit():
        return int(value)
    try:
        until = parsedate_to_datetime(value)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return max(0, math.ceil((until - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError, OverflowError):
        return 0


def _provider_error(payload) -> dict | None:
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("ResponseMetadata")
    error = metadata.get("Error") if isinstance(metadata, dict) else payload.get("Error", payload)
    return error if isinstance(error, dict) else None


def _provider_code(payload) -> int | None:
    """Read the upload gateway's error envelope, never an arbitrary response field."""
    error = _provider_error(payload)
    code = error.get("CodeN") if error else None
    if isinstance(code, bool):
        return None
    try:
        number = int(code) if isinstance(code, (str, int)) else None
        return number if number is not None and 0 <= number <= 2**31 - 1 else None
    except ValueError:
        return None


# Keyword families, checked in order. Only the family name leaves this module,
# never the provider's Code/Message text. The mapping is a diagnostic heuristic.
_REASON_KEYWORDS = (
    ("rate_limited", ("too many", "rate limit", "throttl", "flowlimit", "frequen")),
    ("request_blocked", ("blocked request", "request blocked")),
    ("region_restricted", ("region", "country")),
    ("auth_or_session_expired", ("expire", "unauthor", "authenticat", "credential", "invalid token", "login")),
    ("access_denied", ("access denied", "accessdenied", "forbidden")),
    ("permission_denied", ("permission", "not allowed", "not permitted", "privilege")),
)


_PROVIDER_REASONS = frozenset({"none", "other"} | {name for name, _ in _REASON_KEYWORDS})


def provider_reason(payload) -> str:
    """Closed-set classification of an upload gateway error: none, other or a known family."""
    error = _provider_error(payload)
    if not error:
        return "none"
    text = " ".join(str(error.get(key, ""))[:256] for key in ("Code", "Message")).lower()
    return next((name for name, words in _REASON_KEYWORDS if any(word in text for word in words)), "other")


_TRANSPORT_CLASSES = frozenset({
    "ConnectTimeout", "ReadTimeout", "Timeout", "ProxyError", "SSLError", "ConnectionError",
    "ChunkedEncodingError", "ContentDecodingError", "TooManyRedirects", "InvalidURL", "RequestException"})


def transport_error_class(exc: BaseException) -> str:
    """Most specific known requests/curl_cffi exception class name, else ``other``; never exception text."""
    return next((cls.__name__ for cls in type(exc).__mro__ if cls.__name__ in _TRANSPORT_CLASSES), "other")


_STAGES = frozenset({
    "create project", "upload auth", "apply upload", "finish upload", "commit upload", "tiktok.com", "session check",
    "studio ApplyUploadInner", "studio CommitUploadInner", "studio transfer", "studio wait", "studio storage",
    "studio inline commit", "studio publish validation", "studio publish gate", "studio redirect",
    "studio redirect guard", "studio proxy auth", "studio privacy", "studio metadata", "studio onboarding",
    "studio submit"})


APPLY_STAGES = frozenset({"apply upload", "studio ApplyUploadInner"})


def safe_stage(stage: str | None) -> str:
    if stage in _STAGES:
        return stage
    if isinstance(stage, str) and re.fullmatch(r"upload part [1-9][0-9]{0,3}", stage):
        return stage
    return "other"


def upload_diagnostics(response, *, payload=None) -> dict:
    """Bounded upload response facts, shared by HTTP, Studio and operator probes.

    Never return provider text, URLs, header values or credentials. Hash the
    provider request ID so responses can be correlated without echoing it.
    """
    kind = "non_json"
    if payload is None:
        try:
            payload = response.json()
        except (TypeError, ValueError):
            pass
    metadata = payload.get("ResponseMetadata") if isinstance(payload, dict) else None
    if isinstance(payload, dict):
        error = metadata.get("Error") if isinstance(metadata, dict) else payload.get("Error")
        kind = "error" if error else "result" if "Result" in payload else "other_object"
    elif payload is not None:
        kind = "non_object"
    request_id = metadata.get("RequestId") if isinstance(metadata, dict) else None
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 512:
        request_id = None
    status = response.status_code
    return {
        "status": status if type(status) is int and 100 <= status <= 599 else None,
        "provider_code": _provider_code(payload),
        "provider_reason": provider_reason(payload),
        "response_kind": kind,
        "request_id_tag": hashlib.sha256(request_id.encode()).hexdigest()[:16] if request_id else None,
    }


class AutotokError(Exception):
    """Base class for all autotok errors."""

    retryable: bool = False


class ValidationError(AutotokError, ValueError):
    """Invalid input (caption too long, bad schedule, bad proxy string...)."""


class AccountNotFoundError(AutotokError):
    """No saved session exists for the requested account name."""


class NotLoggedInError(AutotokError):
    """The saved account has no TikTok ``sessionid`` cookie, or it expired."""


class LoginError(AutotokError):
    """The interactive login did not complete."""


class BrowserNotInstalledError(AutotokError):
    """Playwright's Chromium is missing. Run ``autotok install-browser``."""


class MissingDependencyError(AutotokError):
    """An optional extra (e.g. ``autotok[youtube]``) is not installed."""


class ConfigurationError(AutotokError):
    """A setting is missing or invalid (e.g. ``BROWSERBASE_API_KEY`` is not set)."""


class RemoteBrowserError(AutotokError):
    """A cloud browser (Browserbase) session could not be started or reached."""

    retryable = True


class SigningError(AutotokError):
    """Generating TikTok's request signatures failed (nothing was published)."""

    retryable = True


class UploadError(AutotokError):
    """The video could not be transferred to TikTok (nothing was published)."""

    retryable = True

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None,
                 provider_code: int | None = None, retry_after: str | None = None, stage: str | None = None,
                 rate_limited: bool = False, diagnostics: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body
        self.provider_code = provider_code
        self.retry_after_seconds = _retry_after_seconds(retry_after)
        self.stage = stage
        self._rate_limited = rate_limited
        self.diagnostics = diagnostics or {}
        if self.provider_code is None and body:
            try:
                self.provider_code = _provider_code(json.loads(body))
            except (TypeError, ValueError):
                pass

    @property
    def rate_limited(self) -> bool:
        return self._rate_limited or self.status == 429 or self.provider_code == 30411 or "too many request" in (self.body or "").lower()

    @property
    def apply_blocked(self) -> bool:
        """Exact demonstrated refusal: Apply answered CodeN 30522 with the known blocked-request message family."""
        return (self.stage in APPLY_STAGES and self.provider_code == 30522
                and self.diagnostics.get("provider_reason") == "request_blocked")

    @property
    def safe_reason(self) -> str | None:
        reason = self.diagnostics.get("provider_reason")
        return reason if reason in _PROVIDER_REASONS else None

    @property
    def safe_transport(self) -> str | None:
        transport = self.diagnostics.get("transport_class")
        return transport if transport in _TRANSPORT_CLASSES or transport == "other" else None

    def operator_summary(self) -> str:
        """Closed-set, value-free text for operators: stage, integer provider code, normalized reason, transport class."""
        parts = [f"stage={safe_stage(self.stage)}"]
        if type(self.provider_code) is int and 0 <= self.provider_code <= 2**31 - 1:
            parts.append(f"provider_code={self.provider_code}")
        if self.safe_reason not in (None, "none"):
            parts.append(f"reason={self.safe_reason}")
        if self.safe_transport:
            parts.append(f"transport={self.safe_transport}")
        return " ".join(parts)

    @classmethod
    def from_response(cls, stage: str, response, *, payload=None) -> "UploadError":
        if payload is None:
            try:
                payload = response.json()
            except ValueError:
                pass
        # Responses may contain temporary upload credentials. Retain only the
        # status, provider code and retry timing needed for recovery and logs.
        facts = upload_diagnostics(response, payload=payload)
        return cls(f"{stage}: TikTok rejected the upload request (HTTP {response.status_code})",
                   status=response.status_code, provider_code=_provider_code(payload),
                   retry_after=response.headers.get("Retry-After"), stage=stage,
                   rate_limited="too many request" in response.text.lower(), diagnostics=facts)


class PublishError(AutotokError):
    """TikTok rejected the post, e.g. invalid parameters or a guidelines block."""

    def __init__(self, message: str, *, status_code: int | None = None, status_msg: str | None = None,
                 response: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.status_msg = status_msg
        self.response = response or {}


class PublishUncertainError(AutotokError):
    """The publish request failed in transit, so the post may or may not exist.

    Check the account on TikTok before trying again.
    """
