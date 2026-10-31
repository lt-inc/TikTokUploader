from __future__ import annotations

import time
import base64
import json
import threading
from types import SimpleNamespace
from datetime import datetime, timezone
from dataclasses import replace

import pytest
import httpx
from fastapi.testclient import TestClient

from autotok.uploader import Client
from autotok.accounts import Account
from terex_tiktok.config import Config, parse_studio_account_ids
from terex_tiktok.connections import Connections
from terex_tiktok.identity import Identity, SessionRejected, checked_cookies
from terex_tiktok.main_client import Fenced, Lease
from terex_tiktok.reconcile import matching_post, response_post_ids
from terex_tiktok.store import EncryptedStore
from terex_tiktok.worker import Worker
from terex_tiktok.app import create_app
from terex_tiktok.ownership import ProcessOwnership
from terex_tiktok.browser import browser_permit


def config(tmp_path):
    return Config("test-token", b"k" * 32, "http://main", "worker-token", "amqp://localhost", "https://admin.example",
                  tmp_path / "state", tmp_path / "scratch", media_hosts=("media.example",))


def cookies(value="own-session"):
    return [{"name": "sessionid", "value": value, "domain": ".tiktok.com", "path": "/", "httpOnly": True}]


def test_pairing_single_use_and_wrong_identity_preserves_account(tmp_path):
    with Client(Account(name="browser-session", cookies=cookies(), user_agent="real browser agent"))._session() as session:
        assert not any(cookie.name == "tt-target-idc" for cookie in session.cookies)
    routed = cookies() + [{"name": "tt-target-idc", "value": "alisg", "domain": ".tiktok.com", "path": "/"}]
    with Client(Account(name="routed-session", cookies=routed, user_agent="real browser agent"))._session() as session:
        assert session.cookies.get("tt-target-idc", domain=".tiktok.com") == "alisg"
    cfg = config(tmp_path)
    store = EncryptedStore(cfg.state_dir, cfg.encryption_key)
    identity = [Identity("100", "alice", "Alice")]
    service = Connections(store, cfg, verify=lambda *args: identity[0])
    expiry = datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat()
    service.start(1, "x" * 32, expiry)
    result = service.receive("x" * 32, cookies(), "real browser agent", "https://www.tiktok.com")
    assert result["remote_account_id"] == "100" and result["session_version"] == 1
    assert "cookies" not in result
    assert b"own-session" not in next(cfg.state_dir.glob("*.enc")).read_bytes()
    with pytest.raises(ValueError, match="expired or already used"):
        service.receive("x" * 32, cookies(), "real browser agent", "https://www.tiktok.com")
    service.start(1, "y" * 32, expiry, "100")
    identity[0] = Identity("200", "bob", "Bob")
    with pytest.raises(ValueError, match="different TikTok account"):
        service.receive("y" * 32, cookies("other-session"), "real browser agent", "https://www.tiktok.com")
    row = store.get("account", 1)
    assert row["remote_account_id"] == "100" and row["session_version"] == 1
    assert row["cookies"][0]["value"] == "own-session"
    generation = row["pairing_generation"]
    service.start(1, "z" * 32, expiry, "100")
    with pytest.raises(ValueError, match="newer connection request"):
        service.accept(1, cookies(), "real browser agent", Identity("100", "alice", "Alice"), 1, generation)
    identity[0] = Identity("100", "alice", "Alice")
    assert service.receive("z" * 32, cookies(), "real browser agent", "https://www.tiktok.com")["session_version"] == 2
    with pytest.raises(ValueError, match="Only tiktok.com"):
        checked_cookies([{"name": "sessionid", "value": "x", "domain": "evil-tiktok.com"}])


def test_durable_barrier_fences_final_post_and_lease_expiry():
    client = object.__new__(Client)
    calls = []
    client._guard = lambda: calls.append("guard")
    client._submit_publish_request = lambda *args: calls.append("send")
    prepared = {"session": object(), "payload": {}, "request": ()}
    def rejected():
        calls.append("checkpoint")
        raise Fenced("old epoch")
    with pytest.raises(Fenced):
        client.submit(prepared, rejected)
    assert calls == ["guard", "checkpoint"]
    client.submit(prepared, lambda: calls.append("ack"))
    assert calls[-4:] == ["guard", "ack", "guard", "send"]
    client._prepare_publish_request = lambda session: ()
    client._payload = lambda creation, media, caption, markup, extra, *args: {"markup": markup, "extra": extra}
    caption = "Contact: ceo@legendary-trio.me · @twitch_streamer #twitch #моменты"
    caption_prepared = {**prepared, "creation_id": "project", "media_id": "media", "caption": caption,
                        "visibility": 1, "allow_comment": True, "allow_duet": False,
                        "allow_stitch": False, "ai_label": False}
    client.prepare_publish(caption_prepared)
    assert "ceo@legendary-trio.me" in caption_prepared["payload"]["markup"]
    assert "@twitch_streamer" in caption_prepared["payload"]["markup"]
    assert [extra["hashtag_name"] for extra in caption_prepared["payload"]["extra"]] == ["twitch", "моменты"]
    assert all(extra["type"] == 1 for extra in caption_prepared["payload"]["extra"])
    expiry = datetime.fromtimestamp(time.time() + 3, timezone.utc).isoformat()
    lease = Lease(object(), {"attempt_id": "a", "lease_token": "t", "expected_version": 1, "expires_at": expiry})
    with pytest.raises(Fenced):
        lease.check()


def test_redelivery_of_uncertain_publish_never_claims_or_uploads(tmp_path):
    cfg = config(tmp_path)
    store = EncryptedStore(cfg.state_dir, cfg.encryption_key)
    connections = Connections(store, cfg)
    worker = Worker(cfg, store, connections)
    store.put("publication", "12", {"publication_id": "12", "phase": "submitting", "expected_version": 1})
    calls = []
    worker.reconcile = lambda journal: calls.append("inspect")
    worker.main.call = lambda *args: pytest.fail("redelivery must not claim/upload")
    worker.execute({"publication_id": "12", "attempt_id": "a", "expected_version": 1})
    assert calls == ["inspect"]
    # A new process may release a crashed operation only after acquiring the
    # volume's real OS lock. Uncertainty remains; no upload/claim is performed.
    owner = ProcessOwnership(cfg.state_dir)
    owner.acquire()
    contender = ProcessOwnership(cfg.state_dir)
    with pytest.raises(RuntimeError, match="Another TikTok publisher"):
        contender.acquire()
    worker.owner = owner
    worker.main.call = lambda operation, fields: calls.append(operation) or {"accepted": True}
    store.put("publication", 13, {"publication_id": 13, "phase": "submitting", "expected_version": 1,
        "attempt_id": "old-attempt", "lease_token": "old-token", "worker_id": "terminated-process"})
    worker.drain_previous_owner()
    recovered = store.get("publication", 13)
    assert recovered["phase"] == "submitting" and recovered["operation_released"] is True
    assert calls == ["inspect", "release"]
    owner.release()
    contender.acquire()
    contender.release()
    worker.main.client.close()


def test_media_id_is_not_post_id_and_ambiguous_match_is_not_success():
    assert response_post_ids({"video_id": "123", "creation_id": "456"}) == set()
    now = time.time()
    journal = {"caption": "specific caption", "submitted_at": now, "media_id": "123", "post_ids": ["999", "998"]}
    row = {"remote_account_id": "100", "username": "alice"}
    item = {"id": "999", "desc": "specific caption", "create_time": now, "author": {"id": "100"}}
    assert matching_post([item], row, journal)["remote_id"] == "999"
    assert matching_post([item, {**item, "id": "998"}], row, journal) is None
    assert matching_post([{**item, "author": {"id": "200"}}], row, journal) is None
    assert matching_post([item], row, {**journal, "post_ids": []}) is None
    assert matching_post([{**item, "desc": "TikTok-normalized caption", "create_time": now + 7200}], row,
                         {**journal, "post_ids": ["999"], "operator_confirmed": True})["remote_id"] == "999"
    assert matching_post([{**item, "creation_id": "exact-creation"}], row,
                         {**journal, "post_ids": [], "creation_id": "exact-creation"})["remote_id"] == "999"


@pytest.mark.parametrize("upload_mode,global_mode,account_ids", [
    ("http", "http", ()), ("studio", "studio", ()),
    ("studio", "http", (18,)), ("http", "http", (23,)),
])
def test_go_python_wire_contract_through_real_http_serialization(tmp_path, monkeypatch, caplog, upload_mode, global_mode, account_ids):
    """Go int64 IDs/complete state and Python verify DTO cross the HTTP boundary."""
    cfg = replace(config(tmp_path), upload_mode=global_mode, studio_account_ids=account_ids)
    caplog.set_level("INFO", logger="tiktok.worker")
    cfg.scratch_dir.mkdir()
    store = EncryptedStore(cfg.state_dir, cfg.encryption_key)
    connections = Connections(store, cfg)
    store.put("account", 18, {"account_id": 18, "remote_account_id": "100", "session_version": 1,
        "status": "connected", "user_agent": "real browser agent", "username": "alice", "cookies": cookies()})
    worker = Worker(cfg, store, connections)
    expiry = datetime.fromtimestamp(time.time() + 120, timezone.utc).isoformat()
    seen = []
    def main_http(request):
        payload = json.loads(request.content)
        operation = request.url.path.rsplit("/", 1)[-1]
        seen.append(operation)
        if operation == "claim":
            assert type(payload["publication_id"]) is int and payload["publication_id"] == 12
            assert type(payload["expected_version"]) is int
            return httpx.Response(200, json={"granted": True, "attempt_id": "attempt-1", "lease_token": "lease",
                "expected_version": 2, "expires_at": expiry, "publication": {"publication_id": 12, "account_id": "18",
                    "remote_account_id": "100", "session_version": 1, "provider": "tiktok", "visibility": "private",
                    "media_url": "https://media.example/file.mp4", "media_sha256": "a" * 64, "metadata": {"caption": "hello"}}})
        if operation == "checkpoint":
            assert payload["attempt_id"] == "attempt-1" and payload["lease_token"] == "lease"
            assert type(payload["expected_version"]) is int
            if payload["state"] == "publishing":
                assert payload["final_request_started"] is True
                if upload_mode == "studio":
                    assert payload["provider_checkpoint"]["creation_id"] == "studio-native-project"
            return httpx.Response(200, json={"accepted": True})
        if operation == "complete":
            assert payload["state"] == "published" and payload["final_request_started"] is True
            assert payload["remote_id"] == "999" and payload["verified_at"]
            return httpx.Response(200, json={"accepted": True})
        if operation == "release":
            assert payload == {"attempt_id": "attempt-1", "lease_token": "lease", "expected_version": 2}
            return httpx.Response(200, json={"accepted": True})
        if operation == "reconcile":
            assert payload["publication_id"] == 12 and payload["remote_account_id"] == "100"
            return httpx.Response(200, json={"accepted": True})
        pytest.fail(f"unexpected operation {operation}")
    worker.main.client.close()
    worker.main.client = httpx.Client(base_url="http://main", transport=httpx.MockTransport(main_http))
    class BrowserSession:
        def __init__(self):
            self.permit = browser_permit(timeout=0)
            self.permit.__enter__()
            self.closed = False
        def close(self):
            if not self.closed:
                self.permit.__exit__(None, None, None)
                self.closed = True
        def export_cookies(self):
            return cookies("studio-renewed-session")
    class Provider:
        def __init__(self, *args, **kwargs):
            if upload_mode == "studio":
                assert kwargs["expected_remote_account_id"] == "100"
        def prepare(self, *args, **kwargs):
            return {"session": BrowserSession() if upload_mode == "studio" else SimpleNamespace(close=lambda: None)}
        def transfer(self, prepared): return "media-123"
        def prepare_publish(self, prepared): pass
        def submit(self, prepared, before_send):
            prepared["creation_id"] = "studio-native-project"
            before_send()
            return {"aweme_id": "999"}
    monkeypatch.setattr("terex_tiktok.worker.Client", Provider)
    monkeypatch.setattr("terex_tiktok.worker.BrowserStudioClient", Provider)
    def verify(*args):
        assert upload_mode == "http", "Studio verifies identity inside its own browser context"
        return Identity("100", "alice", "Alice")
    monkeypatch.setattr("terex_tiktok.worker.verify_identity", verify)
    monkeypatch.setattr("terex_tiktok.worker.download_media", lambda *args: 4)
    proof = {"remote_id": "999", "remote_url": "https://www.tiktok.com/@alice/video/999", "verified_at": expiry,
             "proof": "authenticated_studio_item"}
    def inspect(row, *args):
        # Studio must release the browser gate before reconciliation opens it.
        with browser_permit(timeout=0):
            if upload_mode == "studio":
                assert row["cookies"][0]["value"] == "studio-renewed-session"
            return proof
    monkeypatch.setattr("terex_tiktok.worker.inspect_posts", inspect)
    worker.execute({"publication_id": 12, "attempt_id": "attempt-1", "expected_version": 2})
    assert f"upload_mode={upload_mode}" in caplog.text
    assert seen == ["claim", "checkpoint", "checkpoint", "checkpoint", "checkpoint", "checkpoint", "complete", "release"]
    journal = store.get("publication", 12)
    assert type(journal["publication_id"]) is int and type(journal["account_id"]) is int
    assert journal["phase"] == "published"
    renewed = store.get("account", 18)
    renewed["session_version"] = 2
    store.put("account", 18, renewed)
    with pytest.raises(SessionRejected):
        connections.require(18, 1, "100")
    with pytest.raises(SessionRejected):
        connections.for_verification(18, "other-owner")
    assert worker.reconcile(journal) is True
    with TestClient(create_app(cfg, start_worker=False)) as api:
        response = api.post("/v1/publications/verify", headers={"Authorization": "Bearer test-token"},
            json={"publication_id": 12, "account_id": 18, "remote_account_id": "100", "remote_id": "999"})
        assert response.status_code == 200 and response.json()["verified"] is True
    worker.main.client.close()


def test_studio_account_opt_in_is_strict_and_loaded_from_environment(monkeypatch):
    env = {"TIKTOK_SESSION_ENCRYPTION_KEY": base64.urlsafe_b64encode(b"k" * 32).decode(),
           "TIKTOK_MAIN_BE_URL": "http://main", "APP_PUBLIC_URL": "https://admin.example",
           "TIKTOK_UPLOADER_API_TOKEN": "fixture", "TIKTOK_WORKER_TOKEN": "fixture", "RABBITMQ_URL": "amqp://localhost",
           "TIKTOK_UPLOAD_MODE": "http", "TIKTOK_STUDIO_ACCOUNT_IDS": "23", "TIKTOK_MEDIA_DOWNLOAD_ORIGIN": ""}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    cfg = Config.from_env()
    assert cfg.studio_account_ids == (23,)
    assert cfg.upload_mode_for(23) == "studio" and cfg.upload_mode_for(24) == "http"
    assert parse_studio_account_ids(None) == parse_studio_account_ids("") == ()
    for invalid in ("0", "-23", "+23", "23.0", "023", "23,", "23,,24", "True", "２３", "9223372036854775808"):
        with pytest.raises(ValueError, match="TIKTOK_STUDIO_ACCOUNT_IDS"):
            parse_studio_account_ids(invalid)


def test_publish_browser_wait_preserves_operation_and_stays_fenced():
    from terex_tiktok import browser
    waits, entered = [], []
    assert browser.GATE.acquire(blocking=False)
    threading.Timer(0.2, browser.GATE.release).start()
    # A guarded operation outlasts the ordinary timeout without losing its media.
    with browser_permit(timeout=0.01, guard=lambda: None, on_wait=lambda: waits.append(1), poll=0.02):
        entered.append(True)
    assert entered == [True] and waits == [1]
    assert browser.GATE.acquire(blocking=False)
    try:
        checks = []
        def guard():
            checks.append(1)
            if len(checks) > 3:
                raise Fenced("cancelled while waiting")
        with pytest.raises(Fenced):
            with browser_permit(guard=guard, poll=0.01):
                pytest.fail("a fenced operation must not acquire the browser")
        with pytest.raises(TimeoutError):
            with browser_permit(timeout=0):
                pass
    finally:
        browser.GATE.release()
    checks = []
    def fenced_after_acquire():
        checks.append(1)
        if len(checks) == 2:
            raise Fenced("cancelled just as the browser became available")
    with pytest.raises(Fenced):
        with browser_permit(guard=fenced_after_acquire):
            pytest.fail("a fenced operation must not use the acquired browser")
    assert not browser.GATE.locked()
