from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone


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
    handler.setFormatter(JsonFormatter() if os.environ.get("LOG_FORMAT", "json") == "json" else logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "info").upper(), handlers=[handler], force=True)
    for name in ("httpx", "httpcore", "pika"):
        logging.getLogger(name).setLevel(logging.WARNING)
