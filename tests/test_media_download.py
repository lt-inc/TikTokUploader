from __future__ import annotations

import hashlib
import logging
import socket
import threading
from dataclasses import replace

import httpx
import pytest

from terex_tiktok.config import Config
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


def config(tmp_path):
    return Config("test-token", b"k" * 32, "http://main", "worker-token", "amqp://localhost", "https://admin.example",
                  tmp_path / "state", tmp_path / "scratch", media_hosts=("127.0.0.1",), max_media_bytes=1024 * 1024)


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
