FROM python:3.12.10-slim-bookworm
ARG SOURCE_REVISION=unknown
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp/home XDG_RUNTIME_DIR=/tmp/runtime DISPLAY=:99 \
    TIKTOK_STATE_DIR=/data/sessions TIKTOK_SCRATCH_DIR=/tmp/tiktok PHANTOMWRIGHT_BROWSERS_PATH=/opt/browsers \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers TIKTOK_SOURCE_REVISION=$SOURCE_REVISION
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends xvfb x11-utils x11vnc novnc websockify ca-certificates ffmpeg tini \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml LICENSE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN pip install --no-cache-dir . && phantomwright_driver install --with-deps chromium \
    && useradd --uid 10001 --create-home --home-dir /tmp/home app \
    && mkdir -p /data/sessions && chown -R app:app /data /opt/browsers
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod 755 /app/entrypoint.sh
USER app
EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
