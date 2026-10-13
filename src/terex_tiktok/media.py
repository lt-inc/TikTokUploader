from __future__ import annotations

import hashlib
import ipaddress
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Config
from .metrics import TRANSFER


def download_media(url: str, expected_sha: str, destination: Path, config: Config, guard) -> int:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.username or not parsed.hostname:
        raise ValueError("Invalid media URL")
    if not config.media_hosts or parsed.hostname not in config.media_hosts:
        raise ValueError("The media host is not on TIKTOK_MEDIA_HOSTS allowlist")
    if len(expected_sha) != 64 or any(x not in "0123456789abcdef" for x in expected_sha.lower()):
        raise ValueError("The publication requires a SHA-256 checksum")
    if shutil.disk_usage(destination.parent).free < config.max_media_bytes + 2 * 1024**3:
        raise TimeoutError("Not enough temporary disk space for publication")
    total = 0
    digest = hashlib.sha256()
    started = time.monotonic()
    with httpx.Client(timeout=httpx.Timeout(30, connect=10), trust_env=False) as client:
        with client.stream("GET", url, follow_redirects=False) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length", 0))
            if declared > config.max_media_bytes:
                raise ValueError("Media exceeds the download limit")
            with destination.open("wb") as file:
                for chunk in response.iter_bytes(1024 * 1024):
                    guard()
                    if time.monotonic() - started > 300:
                        raise TimeoutError("Media download deadline exceeded")
                    total += len(chunk)
                    if total > config.max_media_bytes:
                        raise ValueError("Media exceeds the download limit")
                    digest.update(chunk)
                    file.write(chunk)
    if not total or digest.hexdigest() != expected_sha.lower():
        raise ValueError("Media checksum does not match the scheduled revision")
    TRANSFER.labels("download").inc(total)
    return total
