from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone


class CredentialURLFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        message = re.sub(r"(/api/tiktok/browser-help/)[^/\s?\"'<>]+", r"\1[redacted]", message)
        message = re.sub(r"(https?://[^\s?\"'<>]+)\?[^\s\"'<>]+", r"\1?[redacted]", message)
        record.msg, record.args = message, ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname,
                "service": "terex-tiktok-uploader", "msg": record.getMessage()}
        for key in ("publication_id", "attempt_id", "account_id", "phase", "duration_ms", "size_bytes", "result"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(data, ensure_ascii=False)


def configure_logging():
    handler = logging.StreamHandler()
    handler.addFilter(CredentialURLFilter())
    handler.setFormatter(JsonFormatter() if os.environ.get("LOG_FORMAT", "json") == "json" else logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper(), handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "pika"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # Uvicorn WebSocket connection messages include the browser-help bearer URL.
    # Keep protocol failures, while leaving publication stage logs at LOG_LEVEL.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.WARNING)
