from __future__ import annotations

import copy
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

from autotok.accounts import Account
from autotok.errors import PublishUncertainError, UploadError
from terex_tiktok import studio
from terex_tiktok.main_client import Fenced
from terex_tiktok.identity import SessionRejected


def prepared():
    return {"caption": "A specific caption #test", "visibility": 1, "media_id": "video-1",
            "allow_comment": True, "allow_duet": False, "allow_stitch": False}


def native_payload():
    return {"post_common_info": {"creation_id": "native-project-1"},
            "feature_common_info_list": [{"vedit_common_info": {"video_id": "video-1"},
                "privacy_setting_info": {"visibility_type": 1, "allow_comment": 1, "allow_duet": 0, "allow_stitch": 0}}],
            "single_post_req_list": [{"video_id": "video-1", "single_post_feature_info": {"text": "A specific caption #test"}}]}


class Route:
    def __init__(self, payload=None, url="https://www.tiktok.com/tiktok/web/project/post/v1/", method="POST"):
        self.request = SimpleNamespace(url=url, method=method, post_data_json=payload or native_payload())
        self.continued = self.aborted = 0

    def continue_(self):
        self.continued += 1

    def abort(self, *args):
        self.aborted += 1


def test_unarmed_publish_and_following_valid_request_never_reach_network():
    gate = studio._PublishGate(prepared(), lambda: None)
    first = Route()
    gate.route(first)
    assert isinstance(gate.error, UploadError) and not first.continued and first.aborted == 1
    gate.armed = True
    gate.before_send = lambda: pytest.fail("A latched gate error cannot be cleared by arming")
    second = Route()
    gate.route(second)
    assert not second.continued and second.aborted == 1


@pytest.mark.parametrize("change", ["caption", "privacy", "media", "schedule", "multiple", "interactivity"])
def test_native_payload_mismatch_blocks_publication_before_checkpoint(change):
    payload = native_payload()
    if change == "caption":
        payload["single_post_req_list"][0]["single_post_feature_info"]["text"] = "different caption"
    elif change == "privacy":
        payload["feature_common_info_list"][0]["privacy_setting_info"]["visibility_type"] = 0
    elif change == "media":
        payload["single_post_req_list"][0]["video_id"] = "different-video"
    elif change == "schedule":
        payload["feature_common_info_list"][0]["schedule_time"] = 2000000000
    elif change == "interactivity":
        payload["feature_common_info_list"][0]["privacy_setting_info"]["allow_duet"] = 1
    else:
        payload["single_post_req_list"].append(copy.deepcopy(payload["single_post_req_list"][0]))
    gate = studio._PublishGate(prepared(), lambda: None)
    gate.armed = True
    gate.before_send = lambda: pytest.fail("Mismatched native payload cannot cross the checkpoint")
    route = Route(payload)
    gate.route(route)
    assert isinstance(gate.error, UploadError) and not gate.used and route.continued == 0


@pytest.mark.parametrize("failure_at", ["guard", "barrier", "second_guard"])
def test_fencing_failure_is_preserved_and_prevents_native_send(failure_at):
    calls = []
    failure = Fenced("attempt no longer owns the publication")
    def guard():
        calls.append("guard")
        if failure_at == "guard" or (failure_at == "second_guard" and calls.count("guard") == 2):
            raise failure
    gate = studio._PublishGate(prepared(), guard)
    gate.armed = True
    def barrier():
        calls.append("barrier")
        assert gate.prepared["creation_id"] == "native-project-1"
        if failure_at == "barrier":
            raise failure
    gate.before_send = barrier
    route = Route()
    gate.route(route)
    assert gate.error is failure and not gate.used and not route.continued
    if failure_at == "guard":
        assert calls == ["guard"]


def test_native_creation_is_checkpointed_before_one_send_and_duplicate_is_blocked():
    calls = []
    gate = studio._PublishGate(prepared(), lambda: calls.append("guard"))
    gate.armed = True
    gate.before_send = lambda: calls.append(("checkpoint", gate.prepared["creation_id"]))
    first = Route()
    gate.route(first)
    assert calls == ["guard", ("checkpoint", "native-project-1"), "guard"]
    assert gate.used and first.continued == 1 and not first.aborted
    second = Route()
    gate.route(second)
    assert second.continued == 0 and second.aborted == 1
    assert isinstance(gate.error, PublishUncertainError)


@pytest.mark.parametrize("url", [
    "http://www.tiktok.com/tiktok/web/project/post/v1/",
    "https://other.tiktok.com/tiktok/web/project/post/v1/",
    "https://www.tiktok.com:8443/tiktok/web/project/post/v1/",
    "https://www.tiktok.com/api/v2/item/publish",
    "https://other.example/api/v1/web/project/create/",
    "https://www.tiktok.com/api/v2/web/project/post/",
])
def test_unrecognized_origin_or_mutation_cannot_bypass_checkpoint(url):
    gate = studio._PublishGate(prepared(), lambda: None)
    gate.armed = True
    gate.before_send = lambda: pytest.fail("Unknown mutation must be blocked")
    route = Route(url=url)
    gate.route(route)
    assert route.continued == 0 and route.aborted == 1 and not gate.used


def session_without_browser():
    session = object.__new__(studio._StudioSession)
    session.prepared = prepared()
    session.guard = lambda: None
    session.gate = studio._PublishGate(session.prepared, session.guard)
    session.error = session.media_id = session.session_key = session.result = None
    session.committed = False
    return session


def native_response(action, payload=None, *, status=200, headers=None, request_payload=None):
    url = f"https://www.tiktok.com/top/v1?Action={action}&FileType=video"
    def parse():
        if payload is None:
            raise ValueError("non-JSON")
        return payload
    return SimpleNamespace(url=url, request=SimpleNamespace(url=url, method="POST", post_data_json=request_payload),
                           status=status, headers=headers or {}, json=parse,
                           text=lambda: json.dumps(payload) if payload is not None else "temporarily unavailable")


def test_observed_transfer_proof_is_bound_to_native_video_and_storage_path():
    session = session_without_browser()
    node = {"Vid": "video-1", "SessionKey": "upload-session-1", "UploadHost": "storage.tiktokcdn.com",
            "StoreInfos": [{"StoreUri": "specific/video"}]}
    session.on_response(native_response("ApplyUploadInner", {"Result": {"InnerUploadAddress": {"UploadNodes": [node]}}}))
    assert session.error is None and session.media_id == "video-1" and not session.committed
    allowed = Route(url="https://storage.tiktokcdn.com/specific/video?phase=transfer")
    blocked = Route(url="https://storage.tiktokcdn.com/another/video?phase=transfer")
    session.gate.route(allowed)
    session.gate.route(blocked)
    assert allowed.continued == 1 and blocked.continued == 0
    session.on_response(native_response("CommitUploadInner", {"Result": {"Results": [{"Vid": "video-1"}]}},
                                        request_payload={"SessionKey": "different-upload"}))
    assert not session.committed
    session.on_response(native_response("CommitUploadInner", {"Result": {"Results": [{"Vid": "video-1"}]}},
                                        request_payload={"SessionKey": "upload-session-1"}))
    assert session.error is None and session.committed


def test_non_json_browser_429_preserves_cooldown_and_lowercase_retry_header():
    session = session_without_browser()
    session.on_response(native_response("ApplyUploadInner", status=429, headers={"retry-after": "7200"}))
    assert isinstance(session.error, UploadError)
    assert session.error.rate_limited and session.error.retry_after_seconds == 7200


def test_lost_final_response_is_uncertain_and_never_arms_a_second_send():
    session = session_without_browser()
    session.gate.armed = True
    session.gate.before_send = lambda: None
    route = Route()
    session.gate.route(route)
    session.on_request_failed(route.request)
    assert session.gate.used and isinstance(session.error, PublishUncertainError)
    session.gate.guard = session._request_guard
    second = Route()
    session.gate.route(second)
    assert second.continued == 0


HTML = """<!doctype html><html><body>
<div id="inputs"></div><div contenteditable="true"></div>
<div data-e2e="video_visibility_container"><button role="combobox" onclick="document.querySelector('#options').hidden=false">Everyone</button></div>
<div id="options" hidden><div role="option" onclick="privacy(this)">Everyone</div><div role="option" onclick="privacy(this)">Only you</div></div>
<section><label>Comment</label><div><input type="checkbox" checked id="comment"></div></section>
<section><label>Duet</label><div><input type="checkbox" checked id="duet"></div></section>
<section><label>Stitch</label><div><input type="checkbox" checked id="stitch"></div></section>
<button data-e2e="post_video_button" disabled data-disabled="true" onclick="publish()">Post</button>
<script>
function privacy(el) {document.querySelector('[role=combobox]').textContent=el.textContent; document.querySelector('#options').hidden=true;}
setTimeout(() => {
 document.querySelector('#inputs').innerHTML='<input type="file" accept="video/mp4">';
 document.querySelector('input[type=file]').onchange=async () => {
  await fetch('/api/v1/web/project/create/', {method:'POST',body:'{}'});
  await fetch('/top/v1?Action=ApplyUploadInner&FileType=video');
  await fetch('/top/v1?Action=CommitUploadInner', {method:'POST',body:JSON.stringify({SessionKey:'upload-session-1'})});
  const post=document.querySelector('[data-e2e=post_video_button]'); post.disabled=false; post.dataset.disabled='false';
 };
}, 350);
fetch('/api/new-unknown-mutation', {method:'POST',body:'{}'}).catch(()=>{});
async function publish() {
 const id='video-1';
 const payload={post_common_info:{creation_id:'native-project-1'},
  feature_common_info_list:[{vedit_common_info:{video_id:id},privacy_setting_info:{
    visibility_type:document.querySelector('[role=combobox]').textContent==='Only you'?1:0,
    allow_comment:+document.querySelector('#comment').checked,allow_duet:+document.querySelector('#duet').checked,
    allow_stitch:+document.querySelector('#stitch').checked}}],
  single_post_req_list:[{video_id:id,single_post_feature_info:{text:document.querySelector('[contenteditable]').textContent}}]};
 await fetch('/tiktok/web/project/post/v1/',{method:'POST',body:JSON.stringify(payload)});
}
</script></body></html>"""


@pytest.mark.skipif(os.environ.get("TIKTOK_STUDIO_BROWSER_TESTS") != "1", reason="Opt-in local Chromium fixture; no TikTok requests")
@pytest.mark.parametrize("scenario", ["success", "wrong_identity", "checkpoint_rejected", "preparation_redirect", "publish_redirect"])
def test_local_chromium_native_upload_fences_and_redirects(tmp_path, monkeypatch, scenario):
    """Real Phantomwright routing/locators against a loopback-only fake Studio."""
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, body, status=200, location=None):
            encoded = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html" if isinstance(body, str) else "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path.startswith("/passport/"):
                return self.reply({"message": "success", "data": {"user_id_str": "100", "username": "alice"}})
            if "Action=ApplyUploadInner" in self.path:
                return self.reply({"Result": {"InnerUploadAddress": {"UploadNodes": [{"Vid": "video-1",
                    "SessionKey": "upload-session-1", "UploadHost": "storage.tiktokcdn.com",
                    "StoreInfos": [{"StoreUri": "specific/video"}]}]}}})
            return self.reply(HTML)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append((self.path, json.loads(body or b"{}")))
            if "Action=CommitUploadInner" in self.path:
                if scenario == "preparation_redirect":
                    return self.reply({}, 307, "/tiktok/web/project/post/v1/")
                return self.reply({"Result": {"Results": [{"Vid": "video-1"}]}})
            if self.path == "/tiktok/web/project/post/v1/":
                if scenario == "publish_redirect":
                    return self.reply({}, 307, "/redirect-target")
                return self.reply({"status_code": 0, "aweme_id": "999"})
            return self.reply({"project": {}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    original_origin = studio._studio_origin
    original_preparation = studio._PublishGate._allowed_preparation
    # Only the fixture origin is substituted. Production checks remain HTTPS
    # www.tiktok.com; separate unit tests exercise rejection of other origins.
    monkeypatch.setattr(studio, "STUDIO_URL", origin + "/tiktokstudio/upload")
    monkeypatch.setattr(studio, "_studio_origin", lambda url: url.startswith(origin + "/") or original_origin(url))
    def preparation(gate, request):
        if request.url.startswith(origin + "/"):
            request = SimpleNamespace(method=request.method, url=request.url.replace(origin, "https://www.tiktok.com", 1))
        return original_preparation(gate, request)
    monkeypatch.setattr(studio._PublishGate, "_allowed_preparation", preparation)
    account = Account("fixture", cookies=[{"name": "sessionid", "value": "fixture-only", "domain": ".tiktok.com", "path": "/"}],
                      user_agent="Mozilla/5.0 Chrome/140.0.0.0")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"synthetic video input")
    deadline = time.monotonic() + 15
    def guard():
        if time.monotonic() > deadline:
            raise Fenced("Local fixture exceeded its deadline")
    client = studio.BrowserStudioClient(account, expected_remote_account_id="200" if scenario == "wrong_identity" else "100",
                                       timeout=4, guard=guard)
    state = None
    checkpoints = []
    try:
        if scenario == "wrong_identity":
            with pytest.raises(SessionRejected, match="authorized TikTok account"):
                client.prepare(video, "A specific caption #test", visibility="private")
            assert not received
            return
        state = client.prepare(video, "A specific caption #test", visibility="private")
        if scenario == "preparation_redirect":
            with pytest.raises(UploadError, match="redirect"):
                client.transfer(state)
        else:
            assert client.transfer(state) == "video-1"
            client.prepare_publish(state)
            def barrier():
                checkpoints.append(state["creation_id"])
                assert not any(path == "/tiktok/web/project/post/v1/" for path, _ in received)
                if scenario == "checkpoint_rejected":
                    raise Fenced("checkpoint refused")
            if scenario == "checkpoint_rejected":
                with pytest.raises(Fenced, match="checkpoint refused"):
                    client.submit(state, barrier)
            elif scenario == "publish_redirect":
                with pytest.raises(PublishUncertainError):
                    client.submit(state, barrier)
            else:
                assert client.submit(state, barrier) == {"status_code": 0, "aweme_id": "999"}
                assert state["session"].export_cookies()[0]["value"] == "fixture-only"
        paths = [path for path, _ in received]
        assert "/api/new-unknown-mutation" not in paths and "/redirect-target" not in paths
        assert paths.count("/tiktok/web/project/post/v1/") == int(scenario in ("success", "publish_redirect"))
        assert checkpoints == ([] if scenario == "preparation_redirect" else ["native-project-1"])
    finally:
        if state:
            state["session"].close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
