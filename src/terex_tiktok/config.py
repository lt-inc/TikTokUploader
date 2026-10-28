from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


@dataclass(frozen=True)
class Config:
    api_token: str
    encryption_key: bytes
    main_url: str
    worker_token: str
    rabbit_url: str
    public_url: str
    state_dir: Path
    scratch_dir: Path
    proxy_url: str | None = None
    captcha_key: str | None = None
    captcha_model: str | None = None
    captcha_object_model: str | None = None
    max_media_bytes: int = 512 * 1024 * 1024
    media_hosts: tuple[str, ...] = ()
    upload_mode: str = "http"

    @classmethod
    def from_env(cls) -> "Config":
        def required(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise ValueError(f"{name} must be configured")
            return value

        raw = required("TIKTOK_SESSION_ENCRYPTION_KEY")
        try:
            key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError as exc:
            raise ValueError("TIKTOK_SESSION_ENCRYPTION_KEY must be base64url") from exc
        if len(key) != 32:
            raise ValueError("TIKTOK_SESSION_ENCRYPTION_KEY must encode 32 bytes")
        main = required("TIKTOK_MAIN_BE_URL").rstrip("/")
        public = required("APP_PUBLIC_URL").rstrip("/")
        if urlparse(main).scheme not in ("http", "https") or urlparse(public).scheme != "https":
            raise ValueError("main URL must be HTTP(S); public URL must be HTTPS")
        upload_mode = os.environ.get("TIKTOK_UPLOAD_MODE", "http").strip().lower()
        if upload_mode not in ("http", "studio"):
            raise ValueError("TIKTOK_UPLOAD_MODE must be http or studio")
        return cls(
            required("TIKTOK_UPLOADER_API_TOKEN"), key, main,
            required("TIKTOK_WORKER_TOKEN"), required("RABBITMQ_URL"), public,
            Path(os.environ.get("TIKTOK_STATE_DIR", "/var/lib/tiktok-uploader")),
            Path(os.environ.get("TIKTOK_SCRATCH_DIR", "/scratch")),
            os.environ.get("TIKTOK_PROXY_URL") or None,
            os.environ.get("TIKTOK_CAPTCHA_API_KEY") or None,
            os.environ.get("TIKTOK_CAPTCHA_MODEL_ID") or None,
            os.environ.get("TIKTOK_CAPTCHA_OBJECT_MODEL_ID") or None,
            int(os.environ.get("TIKTOK_MAX_MEDIA_BYTES", 512 * 1024 * 1024)),
            tuple(x.strip() for x in os.environ.get("TIKTOK_MEDIA_HOSTS", "").split(",") if x.strip()),
            upload_mode,
        )
