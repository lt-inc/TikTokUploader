from __future__ import annotations

import hashlib
import logging
import socket
import threading
from dataclasses import replace

import httpx
import pytest

from terex_tiktok.config import Config, parse_media_origin
from terex_tiktok.main_client import Fenced
from terex_tiktok.media import download_media

BODY = b"v" * 4096


@pytest.fixture
def serve():
    servers = []

    def start(head: bytes, body: bytes):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        servers.append(listener)

        def run():
            connection, _ = listener.accept()
            with connection:
                connection.recv(65536)
                connection.sendall(head + body)

        threading.Thread(target=run, daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}/secret-path/video.mp4?token=secret-query"

    yield start
    for listener in servers:
        listener.close()


def config(tmp_path, origin=None, hosts=("127.0.0.1",)):
    return Config("test-token", b"k" * 32, "http://main", "worker-token", "amqp://localhost", "https://admin.example",
                  tmp_path / "state", tmp_path / "scratch", media_hosts=hosts, max_media_bytes=1024 * 1024,
                  media_download_origin=origin)


def fetch(url, sha, tmp_path, guard=lambda: None):
    (tmp_path / "scratch").mkdir(exist_ok=True)
    return download_media(url, sha, tmp_path / "scratch" / "video.mp4", config(tmp_path), guard, "worker_id=tiktok-w1 publication_id=7")


def test_interrupted_body_is_classified_without_leaking_request_details(serve, tmp_path, caplog):
    url = serve(b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n", BODY[:1500])
    with caplog.at_level(logging.INFO, logger="terex_tiktok.media"), pytest.raises(httpx.RemoteProtocolError):
        fetch(url, hashlib.sha256(BODY).hexdigest(), tmp_path)
    message = caplog.records[-1].getMessage()
    for expected in ("worker_id=tiktok-w1", "publication_id=7", "stage=body", "reason=body_shorter_than_declared",
                     "error_type=RemoteProtocolError", "status=200", "http_version=HTTP/1.1",
                     "declared_bytes=4096", "received_bytes=1500", "written_bytes=1500"):
        assert expected in message
    for secret in ("secret-path", "secret-query", "video.mp4", "127.0.0.1", "vvvv", "peer closed"):
        assert secret not in message


def test_checksum_mismatch_is_rejected_and_complete_download_is_verified(serve, tmp_path):
    head = b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n"
    with pytest.raises(ValueError, match="checksum"):
        fetch(serve(head, BODY), "0" * 64, tmp_path)
    assert fetch(serve(head, BODY), hashlib.sha256(BODY).hexdigest(), tmp_path) == len(BODY)


def test_lost_lease_stops_download_as_fenced(serve, tmp_path, caplog):
    def guard():
        raise Fenced("lease")
    url = serve(b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n", BODY)
    with caplog.at_level(logging.INFO, logger="terex_tiktok.media"), pytest.raises(Fenced):
        fetch(url, hashlib.sha256(BODY).hexdigest(), tmp_path, guard)
    assert "reason=guard_rejected" in caplog.records[-1].getMessage()


@pytest.fixture
def capture():
    """Origin server that records the raw request it receives."""
    servers, seen = [], []

    def start(body: bytes = BODY):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(5)
        servers.append(listener)

        def run():
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                with connection:
                    seen.append(connection.recv(65536).decode("latin1"))
                    connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)

        threading.Thread(target=run, daemon=True).start()
        return f"http://127.0.0.1:{listener.getsockname()[1]}", seen

    yield start
    for listener in servers:
        listener.close()


SIGNED = "/bucket/key/video.mp4?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AK%2F20261030%2Fus%2Fs3%2Faws4_request&X-Amz-Signature=abc123"


def fetch_via(source, origin, tmp_path, caplog=None, sha=None):
    (tmp_path / "scratch").mkdir(exist_ok=True)
    return download_media(source, sha or hashlib.sha256(BODY).hexdigest(), tmp_path / "scratch" / "video.mp4",
                          config(tmp_path, origin, ("media.example.test",)), lambda: None, "worker_id=tiktok-w1")


def test_internal_origin_keeps_signed_path_query_and_original_host(capture, tmp_path, caplog):
    origin, seen = capture()
    with caplog.at_level(logging.INFO, logger="terex_tiktok.media"):
        assert fetch_via("https://media.example.test" + SIGNED + "#frag", origin, tmp_path) == len(BODY)
    lines = seen[0].split("\r\n")
    assert lines[0] == f"GET {SIGNED} HTTP/1.1"
    assert "host: media.example.test" in [x.lower() for x in lines]
    assert not any(x.lower().startswith("authorization") for x in lines)
    message = caplog.records[-1].getMessage()
    assert "media_route=internal" in message
    for secret in ("X-Amz", "abc123", "bucket", "127.0.0.1"):
        assert secret not in message


def test_public_route_is_reported_without_origin(serve, tmp_path, caplog):
    url = serve(b"HTTP/1.1 200 OK\r\nContent-Length: 4096\r\nConnection: close\r\n\r\n", BODY)
    with caplog.at_level(logging.INFO, logger="terex_tiktok.media"):
        fetch(url, hashlib.sha256(BODY).hexdigest(), tmp_path)
    assert "media_route=public" in caplog.records[-1].getMessage()


def test_internal_origin_still_requires_allowlisted_credential_free_source_and_sha(capture, tmp_path):
    origin, seen = capture()
    for source in ("https://evil.example.test" + SIGNED, "https://user:pw@media.example.test" + SIGNED,
                   "ftp://media.example.test" + SIGNED):
        with pytest.raises(ValueError):
            fetch_via(source, origin, tmp_path)
    with pytest.raises(ValueError, match="checksum"):
        fetch_via("https://media.example.test" + SIGNED, origin, tmp_path, sha="0" * 64)
    assert len(seen) == 1  # forbidden sources never reached the origin


@pytest.mark.parametrize("origin", [
    "ftp://minio:9000", "minio:9000", "http://", "http://u:p@minio:9000", "http://:pw@minio:9000",
    "http://minio:9000/bucket", "http://minio:9000?x=1", "http://minio:9000#f", "http://minio:99999",
    "http://minio :9000", "http://minio:9000\\x"])
def test_malformed_media_origin_is_rejected_without_echoing_it(origin, tmp_path, capture):
    with pytest.raises(ValueError, match="TIKTOK_MEDIA_DOWNLOAD_ORIGIN") as error:
        parse_media_origin(origin)
    assert "minio" not in str(error.value)
    # A Config built around the value directly is rejected at download time and sends nothing.
    _, seen = capture()
    with pytest.raises(ValueError):
        fetch_via("https://media.example.test" + SIGNED, origin, tmp_path)
    assert not seen


def test_valid_media_origins_are_normalized():
    assert parse_media_origin(None) is None and parse_media_origin("  ") is None
    assert parse_media_origin("http://minio.infra.svc:9000/") == "http://minio.infra.svc:9000"
    assert parse_media_origin("HTTPS://minio.infra.svc") == "https://minio.infra.svc"


def test_internal_failure_is_not_retried_or_sent_to_public(tmp_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{probe.getsockname()[1]}"
    with pytest.raises(httpx.ConnectError):
        fetch_via("https://media.example.test" + SIGNED, dead, tmp_path)

