from __future__ import annotations

import hashlib
import ipaddress
import logging
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Config
from .metrics import MEDIA_FAILURES, TRANSFER

log = logging.getLogger(__name__)

# Exception class names are logged only from this closed set so a diagnostic can
# never carry a library- or proxy-defined name. Messages are never logged.
_ERROR_TYPES = frozenset({
    "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout", "ConnectError", "ReadError", "WriteError",
    "CloseError", "ProxyError", "UnsupportedProtocol", "RemoteProtocolError", "LocalProtocolError", "DecodingError",
    "TooManyRedirects", "HTTPStatusError", "StreamError", "TimeoutError", "OSError", "ValueError", "Fenced"})
_HTTP_VERSIONS = frozenset({"HTTP/1.0", "HTTP/1.1", "HTTP/2"})


class _Trace:
    """Per-download facts. Only numbers and members of closed sets are kept."""

    def __init__(self):
        self.started = time.monotonic()
        self.stage = "validate"  # validate -> request -> body -> verify
        self.reason = None  # set where the cause is known without an exception
        self.status = None
        self.http_version = None
        self.declared = None
        self.framing = None  # content_length | chunked | undeclared
        self.encoded = None  # True when the body is compressed on the wire
        self._response = None
        self.written = 0  # decoded bytes written to disk
        self.headers_at = None
        self.last_chunk_at = None

    @property
    def received(self) -> int:
        """Raw bytes read from the connection, including those read just before a failure."""
        return self._response.num_bytes_downloaded if self._response is not None else 0

    def response(self, response: httpx.Response) -> None:
        self._response = response
        self.status = response.status_code if 100 <= response.status_code <= 599 else None
        version = response.http_version
        self.http_version = version if version in _HTTP_VERSIONS else "other"
        self.headers_at = time.monotonic()
        self.encoded = response.headers.get("content-encoding", "identity").strip().lower() not in ("", "identity")
        if "chunked" in response.headers.get("transfer-encoding", "").lower():
            self.framing = "chunked"
        elif "content-length" in response.headers:
            self.framing = "content_length"
        else:
            self.framing = "undeclared"

    def classify(self, exc: BaseException) -> str:
        if self.reason:
            return self.reason
        if isinstance(exc, httpx.HTTPStatusError):
            return {3: "redirect_3xx", 4: "client_error_4xx", 5: "server_error_5xx"}.get((self.status or 0) // 100, "http_status")
        if isinstance(exc, httpx.ConnectTimeout):
            return "connect_timeout"
        if isinstance(exc, httpx.ReadTimeout):
            return "read_timeout"
        if isinstance(exc, httpx.TimeoutException):
            return "timeout_other"
        if isinstance(exc, httpx.ProxyError):
            return "proxy_error"
        if isinstance(exc, httpx.ConnectError):
            return "connect_error"
        if isinstance(exc, httpx.DecodingError):
            return "content_decoding_error"
        if isinstance(exc, httpx.RemoteProtocolError):
            if self.stage != "body":
                return "peer_closed_before_headers"
            if self.framing == "content_length" and not self.encoded and self.declared is not None and self.received < self.declared:
                return "body_shorter_than_declared"
            return "chunked_body_interrupted" if self.framing == "chunked" else "body_interrupted"
        if isinstance(exc, httpx.NetworkError):
            return "connection_error_in_body" if self.stage == "body" else "connection_error"
        return "unclassified"

    def fields(self, exc: BaseException | None = None) -> str:
        now = time.monotonic()
        ms = lambda value: None if value is None else round((value - self.started) * 1000)
        values = {
            "stage": self.stage,
            "status": self.status,
            "http_version": self.http_version,
            "framing": self.framing,
            "encoded": self.encoded,
            "declared_bytes": self.declared,
            "received_bytes": self.received,
            "written_bytes": self.written,
            "elapsed_ms": ms(now),
            "headers_ms": ms(self.headers_at),
            # Time since the last body chunk separates a stall from a reset.
            "idle_ms": round((now - self.last_chunk_at) * 1000) if self.last_chunk_at is not None else None,
        }
        if exc is not None:
            name = type(exc).__name__
            values = {"reason": self.classify(exc), "error_type": name if name in _ERROR_TYPES else "other", **values}
        return " ".join(f"{key}={value}" for key, value in values.items())


def download_media(url: str, expected_sha: str, destination: Path, config: Config, guard, context: str = "") -> int:
    """Download and verify media. `context` holds worker/publication IDs for log correlation."""
    trace = _Trace()
    try:
        total = _download(url, expected_sha, destination, config, guard, trace)
    except Exception as exc:
        reason = trace.classify(exc)
        MEDIA_FAILURES.labels(trace.stage, reason).inc()
        log.warning("media download failed %s %s", context, trace.fields(exc))
        raise
    log.info("media download finished %s %s", context, trace.fields())
    return total


def _download(url: str, expected_sha: str, destination: Path, config: Config, guard, trace: _Trace) -> int:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.username or not parsed.hostname:
        trace.reason = "invalid_url"
        raise ValueError("Invalid media URL")
    if not config.media_hosts or parsed.hostname not in config.media_hosts:
        trace.reason = "host_not_allowed"
        raise ValueError("The media host is not on TIKTOK_MEDIA_HOSTS allowlist")
    if len(expected_sha) != 64 or any(x not in "0123456789abcdef" for x in expected_sha.lower()):
        trace.reason = "invalid_checksum"
        raise ValueError("The publication requires a SHA-256 checksum")
    if shutil.disk_usage(destination.parent).free < config.max_media_bytes + 2 * 1024**3:
        trace.reason = "insufficient_disk"
        raise TimeoutError("Not enough temporary disk space for publication")
    total = 0
    digest = hashlib.sha256()
    started = time.monotonic()
    trace.stage = "request"
    with httpx.Client(timeout=httpx.Timeout(30, connect=10), trust_env=False) as client:
        with client.stream("GET", url, follow_redirects=False) as response:
            trace.response(response)
            response.raise_for_status()
            try:
                declared = int(response.headers.get("content-length", 0))
            except ValueError:
                declared = -1
            if declared < 0:
                trace.reason = "invalid_content_length"
                raise ValueError("Invalid media content length")
            trace.declared = declared if "content-length" in response.headers else None
            if declared > config.max_media_bytes:
                trace.reason = "declared_too_large"
                raise ValueError("Media exceeds the download limit")
            trace.stage = "body"
            with destination.open("wb") as file:
                for chunk in response.iter_bytes():
                    trace.last_chunk_at = time.monotonic()
                    try:
                        guard()
                    except Exception:
                        trace.reason = "guard_rejected"
                        raise
                    if time.monotonic() - started > 300:
                        trace.reason = "deadline_exceeded"
                        raise TimeoutError("Media download deadline exceeded")
                    total += len(chunk)
                    if total > config.max_media_bytes:
                        trace.reason = "body_too_large"
                        raise ValueError("Media exceeds the download limit")
                    digest.update(chunk)
                    try:
                        file.write(chunk)
                    except OSError:
                        trace.reason = "local_write_error"
                        raise
                    trace.written = total
    trace.stage = "verify"
    if not total:
        trace.reason = "empty_body"
        raise ValueError("Media checksum does not match the scheduled revision")
    if digest.hexdigest() != expected_sha.lower():
        trace.reason = "checksum_mismatch"
        raise ValueError("Media checksum does not match the scheduled revision")
    TRANSFER.labels("download").inc(total)
    return total

