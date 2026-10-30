from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime

import httpx
import pytest
import requests

from autotok.errors import UploadError
from autotok.uploader import Client
from terex_tiktok.config import Config
from terex_tiktok.connections import Connections
from terex_tiktok.identity import Identity
from terex_tiktok.store import EncryptedStore
from terex_tiktok.worker import Worker


def response(status, payload=None, retry_after=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b""
    if retry_after is not None:
        result.headers["Retry-After"] = retry_after
    return result


@pytest.fixture
def publishing(tmp_path, monkeypatch):
    cfg = Config("test-token", b"k" * 32, "http://main", "worker-token", "amqp://localhost", "https://admin.example",
                 tmp_path / "state", tmp_path / "scratch", media_hosts=("media.example",))
    cfg.scratch_dir.mkdir()
    store = EncryptedStore(cfg.state_dir, cfg.encryption_key)
    store.put("account", 18, {"account_id": 18, "remote_account_id": "100", "session_version": 1,
        "status": "connected", "user_agent": "real browser agent", "username": "alice",
        "cookies": [{"name": "sessionid", "value": "session-secret", "domain": ".tiktok.com", "path": "/"}]})
    worker = Worker(cfg, store, Connections(store, cfg))
    seen = []
    def main_call(operation, payload):
        seen.append((operation, payload))
        if operation == "claim":
            return {"granted": True, "attempt_id": payload["attempt_id"], "lease_token": "lease", "expected_version": 1,
                "expires_at": datetime.fromtimestamp(time.time() + 120, timezone.utc).isoformat(),
                "publication": {"publication_id": payload["publication_id"], "account_id": 18,
                    "remote_account_id": "100", "session_version": store.get("account", 18)["session_version"],
                    "provider": "tiktok", "visibility": "private", "media_url": "https://media.example/video.mp4",
                    "media_sha256": "a" * 64, "metadata": {"caption": "hello"}}}
        if operation in ("checkpoint", "fail", "release"):
            return {"accepted": True}
        pytest.fail(f"Unexpected main operation: {operation}")
    worker.main.call = main_call
    monkeypatch.setattr("terex_tiktok.worker.verify_identity", lambda *args: Identity("100", "alice", "Alice"))
    monkeypatch.setattr("terex_tiktok.worker.download_media", lambda url, sha, path, *args: path.write_bytes(b"video"))
    def request(session, method, url, **kwargs):
        if "/project/create/" in url:
            return response(200, {"project": {}})
        if "/video/upload/auth/" in url:
            return response(200, {"video_token_v5": {"access_key_id": "access-secret", "secret_acess_key": "signing-secret",
                                                     "session_token": "temporary-secret"}})
        pytest.fail(f"Unexpected TikTok request: {method}")
    monkeypatch.setattr(requests.Session, "request", request)
    yield worker, store, seen
    worker.main.client.close()


@pytest.mark.parametrize("stage,status,header,minimum,maximum", [
    ("prepare", 429, "60", 1800, 1800),
    ("apply", 429, "http-date", 3595, 3600),
    ("apply", 200, None, 1800, 1800),
    ("apply", 200, "7200", 7200, 7200),
    ("apply", 429, "invalid", 1800, 1800),
])
def test_pre_submit_refusal_defers_account_without_sending_or_losing_session(
        publishing, monkeypatch, caplog, stage, status, header, minimum, maximum):
    worker, store, seen = publishing
    if header == "http-date":
        header = format_datetime(datetime.fromtimestamp(time.time() + 3600, timezone.utc), usegmt=True)
    # Error metadata lies beyond the old 500-character diagnostic excerpt.
    request_id = "https://provider.invalid/?token=do-not-log"
    payload = {"padding": "x" * 600, "ResponseMetadata": {
        "RequestId": request_id, "Error": {"CodeN": 30411, "Message": "do-not-log"}},
        "token": "do-not-log"} if status == 200 else None
    refused = response(status, payload, header)
    calls = []
    def refuse(*args, **kwargs):
        calls.append("request")
        # A concurrent account refresh must survive persistence of the wait.
        current = store.get("account", 18)
        current.update(session_version=2, user_agent="renewed browser agent")
        store.put("account", 18, current)
        return refused
    if stage == "prepare":
        monkeypatch.setattr(requests.Session, "request", refuse)
    else:
        monkeypatch.setattr("autotok.uploader.impersonated_get", refuse)
    monkeypatch.setattr(Client, "submit", lambda *args: pytest.fail("A refused upload must not publish"))
    started = time.time()
    worker.execute({"publication_id": 12, "attempt_id": "attempt-12", "expected_version": 1})
    failed = [payload for operation, payload in seen if operation == "fail"]
    assert len(failed) == 1 and failed[0]["code"] == "tiktok_rate_limited"
    assert failed[0]["retryable"] is True
    assert minimum <= failed[0]["retry_after_seconds"] <= maximum
    account = store.get("account", 18)
    assert store.get("upload-cooldown", 18)["not_before"] >= started + minimum
    assert account["session_version"] == 2 and account["user_agent"] == "renewed browser agent"
    assert calls == ["request"]
    assert "do-not-log" not in caplog.text and "session-secret" not in caplog.text
    assert "account_id=18 session_version=1 upload_mode=http route_mode=default proxied=False" in caplog.text
    if payload is not None:
        assert "response_kind=error" in caplog.text
        assert "request_id_tag=" + hashlib.sha256(request_id.encode()).hexdigest()[:16] in caplog.text


def test_media_download_failure_is_distinct_retryable_and_never_reaches_tiktok(publishing, monkeypatch, caplog):
    worker, store, seen = publishing
    def interrupted(*args):
        raise httpx.RemoteProtocolError("peer closed https://media.example/video.mp4?token=do-not-log")
    monkeypatch.setattr("terex_tiktok.worker.download_media", interrupted)
    monkeypatch.setattr(Client, "prepare", lambda *args, **kwargs: pytest.fail("A failed download must not reach TikTok"))
    worker.execute({"publication_id": 14, "attempt_id": "attempt-14", "expected_version": 1})
    failed = [payload for operation, payload in seen if operation == "fail"]
    assert len(failed) == 1 and failed[0]["code"] == "upload_preflight_failed" and failed[0]["retryable"] is True
    assert failed[0]["message"].startswith("Media download failed")
    assert store.get("publication", 14)["phase"] == "preparing"
    assert f"worker_id={worker.worker_id} account_id=18" in caplog.text
    assert "do-not-log" not in caplog.text and "session-secret" not in caplog.text


def test_account_cooldown_survives_reconnect_restart_and_blocks_sibling_without_extension(publishing, monkeypatch):
    worker, store, seen = publishing
    until = time.time() + 2700
    store.put("upload-cooldown", 18, {"remote_account_id": "100", "not_before": until})
    cookies = store.get("account", 18)["cookies"]
    worker.connections.disconnect(18)
    worker.connections.verify = lambda *args: Identity("100", "alice", "Alice")
    worker.connections.start(18, "x" * 32, datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat())
    worker.connections.receive("x" * 32, cookies, "renewed browser agent", "https://www.tiktok.com")
    # Use a new store/worker, as happens on process restart.
    restored = EncryptedStore(worker.config.state_dir, worker.config.encryption_key)
    replacement = Worker(worker.config, restored, Connections(restored, worker.config))
    replacement.main.call = worker.main.call
    def forbidden(*args, **kwargs):
        pytest.fail("Cooldown must precede every TikTok request and media download")
    monkeypatch.setattr("terex_tiktok.worker.verify_identity", forbidden)
    monkeypatch.setattr("terex_tiktok.worker.download_media", forbidden)
    try:
        replacement.execute({"publication_id": 13, "attempt_id": "attempt-13", "expected_version": 1})
    finally:
        replacement.main.client.close()
    failed = [payload for operation, payload in seen if operation == "fail"]
    assert len(failed) == 1 and failed[0]["code"] == "tiktok_account_cooldown"
    assert failed[0]["state"] == "retry_wait"
    assert 2695 <= failed[0]["retry_after_seconds"] <= 2700
    assert restored.get("upload-cooldown", 18)["not_before"] == until


def test_rate_limit_after_submit_barrier_stays_uncertain_and_redelivery_never_reuploads(publishing, monkeypatch):
    worker, store, seen = publishing
    sends = []
    monkeypatch.setattr(Client, "transfer", lambda *args: "media-id")
    monkeypatch.setattr(Client, "prepare_publish", lambda self, prepared: prepared.update(payload={}, request=()))
    def submit(self, *args):
        sends.append("send")
        raise UploadError.from_response("post response", response(429, retry_after="7200"))
    monkeypatch.setattr(Client, "_submit_publish_request", submit)
    command = {"publication_id": 12, "attempt_id": "attempt-12", "expected_version": 1}
    worker.execute(command)
    failed = [payload for operation, payload in seen if operation == "fail"]
    assert len(failed) == 1 and failed[0]["state"] == "needs_attention"
    assert failed[0]["code"] == "upload_unknown" and failed[0]["retryable"] is False
    assert "retry_after_seconds" not in failed[0]
    assert store.get("upload-cooldown", 18) is None
    assert store.get("publication", 12)["phase"] == "submitting"
    inspected = []
    worker.reconcile = lambda journal: inspected.append(journal["publication_id"])
    before = len(seen)
    worker.execute(command)
    assert inspected == [12] and sends == ["send"] and len(seen) == before


def test_disconnected_browser_cleanup_does_not_strand_operation(publishing, monkeypatch):
    worker, store, seen = publishing
    worker.config = replace(worker.config, upload_mode="studio")
    class BrokenBrowser:
        def close(self):
            raise RuntimeError("Browser disconnected")
    class Studio:
        def __init__(self, *args, **kwargs): pass
        def prepare(self, *args, **kwargs): return {"session": BrokenBrowser()}
        def transfer(self, prepared):
            raise UploadError("Transfer stopped before final submission", stage="studio transfer")
    monkeypatch.setattr("terex_tiktok.worker.BrowserStudioClient", Studio)
    worker.execute({"publication_id": 12, "attempt_id": "attempt-12", "expected_version": 1})
    assert [operation for operation, _ in seen][-2:] == ["fail", "release"]
    journal = store.get("publication", 12)
    assert journal["operation_ended"] is True and journal["operation_released"] is True
