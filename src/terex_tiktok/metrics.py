from prometheus_client import Counter, Gauge, Histogram

UPLOADS = Counter("tiktok_upload_attempts_total", "Upload attempt outcomes", ["outcome"])
STAGES = Histogram("tiktok_upload_stage_seconds", "Time spent in upload stage", ["stage"], buckets=(1, 5, 10, 30, 60, 120, 300, 600))
TRANSFER = Counter("tiktok_media_bytes_total", "Media bytes downloaded or transferred", ["direction"])
ACTIVE = Gauge("tiktok_upload_active", "Currently executing uploads")
CONNECTED = Gauge("tiktok_sessions", "Session counts", ["state"])
BROKER = Gauge("tiktok_worker_broker_connected", "RabbitMQ worker connection")
HELP = Gauge("tiktok_browser_help_active", "Active operator browser sessions")
