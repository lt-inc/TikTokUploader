"""Exception hierarchy.

Every error raised on purpose by autotok derives from :class:`AutotokError`, so
callers can catch one type. ``retryable`` tells automation (the scheduler, your
own scripts) whether running the same upload again is safe: it is only ``True``
when the failure happened before anything was sent to TikTok's publish
endpoint, so a retry cannot create a duplicate post.
"""
from __future__ import annotations

import json
import math
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


def _provider_code(payload) -> int | None:
    """Read the upload gateway's error envelope, never an arbitrary response field."""
    if not isinstance(payload, dict):
        return None
    metadata = payload.get("ResponseMetadata")
    error = metadata.get("Error") if isinstance(metadata, dict) else payload.get("Error", payload)
    code = error.get("CodeN") if isinstance(error, dict) else None
    if isinstance(code, bool):
        return None
    try:
        return int(code) if isinstance(code, (str, int)) else None
    except ValueError:
        return None


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
                 rate_limited: bool = False):
        super().__init__(message)
        self.status = status
        self.body = body
        self.provider_code = provider_code
        self.retry_after_seconds = _retry_after_seconds(retry_after)
        self.stage = stage
        self._rate_limited = rate_limited
        if self.provider_code is None and body:
            try:
                self.provider_code = _provider_code(json.loads(body))
            except (TypeError, ValueError):
                pass

    @property
    def rate_limited(self) -> bool:
        return self._rate_limited or self.status == 429 or self.provider_code == 30411 or "too many request" in (self.body or "").lower()

    @classmethod
    def from_response(cls, stage: str, response, *, payload=None) -> "UploadError":
        if payload is None:
            try:
                payload = response.json()
            except ValueError:
                pass
        # Responses may contain temporary upload credentials. Retain only the
        # status, provider code and retry timing needed for recovery and logs.
        return cls(f"{stage}: TikTok rejected the upload request (HTTP {response.status_code})",
                   status=response.status_code, provider_code=_provider_code(payload),
                   retry_after=response.headers.get("Retry-After"), stage=stage,
                   rate_limited="too many request" in response.text.lower())


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
