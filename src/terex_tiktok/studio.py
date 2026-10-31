"""Experimental local TikTok Studio uploader.

The live site constructs upload and publish requests. The final request is
validated and fenced before the browser is permitted to send it; UI messages
and redirects are never evidence that a post exists.
"""
from __future__ import annotations

import base64
import json
import time
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from requests.structures import CaseInsensitiveDict

from autotok.accounts import Account
from autotok.browsers import launch_chromium, sync_playwright
from autotok.errors import PublishError, PublishUncertainError, UploadError, ValidationError, upload_diagnostics
from autotok.uploader import MAX_CAPTION_LENGTH, _visibility, resolve_video_path

from .browser import browser_permit, session_cookies
from .identity import SessionRejected, _identity, checked_cookies

STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload"
POST_PATHS = {"/tiktok/web/project/post/v1", "/api/v1/web/project/post"}
POST_BUTTON = "button[data-e2e='post_video_button']"
PROJECT_PREPARATION_PATHS = {
    "/api/v1/web/project/create", "/api/v1/web/project/update", "/api/v1/web/project/save",
    "/tiktok/web/project/create/v1", "/tiktok/web/project/update/v1", "/tiktok/web/project/save/v1",
}
MAX_UPLOAD_NODES = 16
MAX_STORE_INFOS = 16
MAX_APPLY_BODY = 1 << 20
_NATIVE_FACTS = ("action", "file_type", "reason", "nodes", "empty_vid", "empty_key", "invalid_store", "results")


class _NativeRejected(ValueError):
    """Bounded, value-free reason why a native upload response was rejected."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _node_counts(nodes: list) -> dict:
    # Presence counts only; never include identity, host or storage values.
    items = [node for node in nodes[:MAX_UPLOAD_NODES] if isinstance(node, dict)]
    return {
        "empty_vid": sum(1 for node in items if not node.get("Vid")),
        "empty_key": sum(1 for node in items if not node.get("SessionKey")),
        "invalid_store": sum(1 for node in items if not isinstance(node.get("StoreInfos"), list)
                             or not node["StoreInfos"] or not isinstance(node.get("UploadHost"), str)),
    }


def _upload_string(value, limit: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid upload identity or address")
    return value


def _tiktok_host(host: str | None) -> bool:
    host = (host or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def _studio_origin(url: str) -> bool:
    parts = urlparse(url)
    return (parts.scheme == "https" and parts.hostname == "www.tiktok.com" and parts.port in (None, 443)
            and parts.username is None and parts.password is None)


def _publish_path(request) -> str | None:
    parts = urlparse(request.url)
    path = parts.path.rstrip("/")
    if request.method != "POST":
        return None
    # An unrecognized project publish route must not bypass the known-route gate.
    if path in POST_PATHS or ("project" in path and ("/post" in path or "/publish" in path)):
        return path
    return None


def _validated_creation(payload, prepared: dict) -> str:
    """Accept only the one-video native payload whose settings we authorized."""
    try:
        common = payload["post_common_info"]
        features = payload["feature_common_info_list"]
        posts = payload["single_post_req_list"]
        if len(features) != 1 or len(posts) != 1:
            raise ValueError
        feature, post = features[0], posts[0]
        privacy = feature["privacy_setting_info"]
        if type(privacy["visibility_type"]) is not int or privacy["visibility_type"] != prepared["visibility"]:
            raise ValueError
        for name in ("allow_comment", "allow_duet", "allow_stitch"):
            if type(privacy[name]) is not int or privacy[name] != int(prepared[name]):
                raise ValueError
        if post["single_post_feature_info"]["text"] != prepared["caption"]:
            raise ValueError
        if not prepared.get("media_id") or str(post["video_id"]) != prepared["media_id"]:
            raise ValueError
        if str(feature["vedit_common_info"]["video_id"]) != prepared["media_id"] or feature.get("schedule_time"):
            raise ValueError
        creation = common["creation_id"]
        if not isinstance(creation, str) or not 1 <= len(creation) <= 256:
            raise ValueError
        return creation
    except (KeyError, IndexError, TypeError, ValueError):
        raise UploadError("Studio publish payload does not match the authorized video, caption or privacy settings",
                          stage="studio publish validation") from None


class _PublishGate:
    def __init__(self, prepared: dict, guard):
        self.prepared, self.guard = prepared, guard
        self.armed = False
        self.used = False
        self.before_send = None
        self.request = None
        self.error: Exception | None = None
        self.storage_paths: set[tuple[str, str]] = set()
        self.blocked_storage_requests = 0
        self.blocked_shape = ""
    @staticmethod
    def storage_for(node: dict) -> set[tuple[str, str]]:
        host = _upload_string(node["UploadHost"], 253)
        if any(c in host for c in "/@\\"):
            raise ValueError("Invalid upload host")
        address = urlparse("https://" + host)
        if (address.netloc != host or address.query or address.fragment or address.port is not None
                or not address.hostname or _tiktok_host(address.hostname)):
            raise ValueError("Invalid upload storage origin")
        infos = node["StoreInfos"]
        if not isinstance(infos, list) or not 1 <= len(infos) <= MAX_STORE_INFOS:
            raise ValueError("Invalid storage entries")
        paths = set()
        for item in infos:
            uri = _upload_string(item["StoreUri"], 2048)
            if any(c in uri for c in "?#\\") or any(part in (".", "..") for part in uri.split("/")) or not uri.lstrip("/"):
                raise ValueError("Invalid storage URI")
            paths.add((host.lower(), "/" + uri.lstrip("/")))
        return paths

    def _storage_shape(self, request) -> str:
        # Booleans and counts only: never the host, path, query or any identifier.
        parts = urlparse(request.url)
        path = parts.path[len("/upload/v1"):] if parts.path.startswith("/upload/v1/") else parts.path
        method = request.method if request.method in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "DELETE", "PATCH") else "other"
        host = parts.netloc.lower()
        return (f"method={method} query={int(bool(parts.query))} native_prefix={int(parts.path.startswith('/upload/v1/'))} "
                f"segments={min(len([s for s in path.split('/') if s]), 9)} encoded={int('%' in parts.path)} "
                f"path_known={int((host, path) in self.storage_paths)} known_paths={min(len(self.storage_paths), 999)}")

    def _allowed_preparation(self, request) -> bool:
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return True
        parts = urlparse(request.url)
        query = parse_qs(parts.query)
        if parts.scheme != "https":
            return False
        if request.method == "POST" and _studio_origin(request.url):
            if parts.path.rstrip("/") in PROJECT_PREPARATION_PATHS:
                return True
            if parts.path.rstrip("/") == "/top/v1" and query.get("Action", [None])[0] in ("ApplyUploadInner", "CommitUploadInner"):
                return True
        # Current native SDK uses a single POST with no query. The exact
        # object and host still come only from its authenticated Apply response.
        if (request.method == "POST" and not parts.query and parts.path.startswith("/upload/v1/")
                and (parts.netloc.lower(), parts.path[len("/upload/v1"):]) in self.storage_paths):
            return True
        return (request.method in ("POST", "PUT") and (parts.netloc.lower(), parts.path) in self.storage_paths
                and query.get("phase", [None])[0] in ("transfer", "finish"))

    def route(self, route):
        request = route.request
        try:
            if self.error is not None:
                raise self.error
            self.guard()
            path = _publish_path(request)
            if path is None:
                if self._allowed_preparation(request):
                    route.continue_()
                else:
                    if urlparse(request.url).netloc.lower() in {host for host, _ in self.storage_paths}:
                        self.blocked_storage_requests = min(1000, self.blocked_storage_requests + 1)
                        self.blocked_shape = self._storage_shape(request)
                    # Unknown mutations, including a changed publication API,
                    # never leave the browser. Dropped telemetry is harmless.
                    route.abort("blockedbyclient")
                return
            if self.used:
                raise PublishUncertainError("A repeated Studio publish request was blocked; inspect the first post")
            if not self.armed or path not in POST_PATHS or not _studio_origin(request.url):
                raise UploadError("An unarmed or unrecognized Studio publish request was blocked", stage="studio publish gate")
            creation = _validated_creation(request.post_data_json, self.prepared)
            self.prepared["creation_id"] = creation
            self.before_send()
            self.guard()
            # Mark before continuing: a lost continuation/response can never
            # authorize another publication request from this page.
            self.used = True
            self.request = request
            route.continue_()
        except Exception as exc:
            if self.error is None:
                self.error = exc
            try:
                route.abort("blockedbyclient")
            except Exception:
                pass


class _StudioSession:
    # Class defaults keep browserless unit fixtures on the Playwright callback path.
    cdp_apply = False
    apply_intercepted = 0

    def __init__(self, account: Account, prepared: dict, guard, timeout: float, label: str = "", wait_guard=None):
        self.stack = ExitStack()
        self.guard, self.prepared, self.timeout = guard, prepared, timeout
        self.gate = _PublishGate(prepared, self._request_guard)
        self.error = None
        self.media_id = None
        self.session_key = None
        self.video_candidates: set[tuple[str, str]] = set()
        self.aux_keys: set[str] = set()
        self.aux_vids: set[str] = set()
        self.committed = False
        self.result = None
        self.closed = False
        self.proxy = account.get_proxy()
        self.auth_attempts: set[str] = set()
        try:
            self.stack.enter_context(browser_permit(guard=wait_guard, label=label))
            pw = self.stack.enter_context(sync_playwright())
            # Only our scoped auth handler may supply credentials. Playwright
            # otherwise reuses proxy credentials for unrelated server challenges.
            browser_proxy = self.proxy
            if self.proxy and self.proxy.scheme in ("http", "https"):
                browser_proxy = replace(self.proxy, username=None, password=None)
            browser = launch_chromium(pw, headless=True, proxy=browser_proxy)
            self.stack.callback(browser.close)
            self.context = browser.new_context(user_agent=account.user_agent, locale="en-US", service_workers="block")
            self.stack.callback(self.context.close)
            self.context.add_cookies(checked_cookies(account.cookies))
            self.context.route("**/*", self.gate.route)
            self.page = self.context.new_page()
            self.page.on("response", self.on_response)
            self.page.on("requestfailed", self.on_request_failed)
            # Playwright routes only the first request in a redirect chain.
            # Intercept native responses as well so a 307/308 cannot bypass the
            # request gate or resend an already submitted publication.
            # Arm response interception per mutation and handle proxy challenges
            # in this Fetch session as well; a second Fetch client cannot rely
            # on Playwright's internal auth handler.
            self.cdp = self.context.new_cdp_session(self.page)
            self.stack.callback(self.cdp.detach)
            self.cdp.on("Fetch.requestPaused", self.on_response_paused)
            self.cdp.on("Fetch.authRequired", self.on_auth_required)
            self.cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}],
                                           "handleAuthRequests": True})
            self.cdp_apply = True
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            self.stack.close()

    def export_cookies(self) -> list[dict]:
        return session_cookies(self.context)

    def _request_guard(self):
        if self.gate.error is not None:
            raise self.gate.error
        if self.error is not None:
            raise self.error
        self.guard()

    def check(self):
        self._request_guard()
        if not _studio_origin(self.page.url):
            raise SessionRejected("Studio navigated outside the authenticated TikTok origin")
        if "/login" in urlparse(self.page.url).path or self.page.locator("img#captcha-verify-image").is_visible():
            raise SessionRejected("Finish TikTok browser verification before using Studio upload")

    def wait(self, predicate, seconds: float, message: str):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.check()
            if predicate():
                return
            self.page.wait_for_timeout(200)
        self.check()
        if self.gate.used:
            raise PublishUncertainError(message + "; the native publish request may have succeeded")
        raise UploadError(message, stage="studio wait", diagnostics={
            "native": (f"reason=wait_timeout blocked_storage={self.gate.blocked_storage_requests} "
                       f"apply_cdp={self.apply_intercepted} {self.gate.blocked_shape}").strip()})

    def on_request_failed(self, request):
        if request == self.gate.request and self.error is None:
            self.error = PublishUncertainError("The Studio publish response was lost; inspect the account")

    def on_auth_required(self, event):
        response = {"response": "CancelAuth"}
        try:
            self._request_guard()
            challenge = event["authChallenge"]
            origin = urlparse(challenge["origin"])
            proxy = self.proxy
            if (proxy and proxy.username is not None and challenge.get("source") == "Proxy"
                    and origin.scheme in ("http", "https") and origin.scheme == proxy.scheme
                    and origin.hostname is not None
                    and origin.hostname.encode("idna").lower() == proxy.host.encode("idna").lower()
                    and (origin.port if origin.port is not None else (443 if origin.scheme == "https" else 80)) == proxy.port
                    and origin.username is None and origin.password is None
                    and origin.path in ("", "/") and not origin.query and not origin.fragment
                    and event["requestId"] not in self.auth_attempts):
                # Keep IDs until close: clearing on a 407/redirect would permit
                # an unbounded authentication loop on the same request.
                self.auth_attempts.add(event["requestId"])
                response = {"response": "ProvideCredentials", "username": proxy.username, "password": proxy.password or ""}
            self.cdp.send("Fetch.continueWithAuth", {"requestId": event["requestId"], "authChallengeResponse": response})
            return
        except Exception:
            if self.error is None:
                self.error = (PublishUncertainError("Studio proxy authentication failed after publication") if self.gate.used
                              else UploadError("Studio proxy authentication failed", stage="studio proxy auth"))
        try:
            self.cdp.send("Fetch.continueWithAuth", {"requestId": event["requestId"],
                                                    "authChallengeResponse": {"response": "CancelAuth"}})
        except Exception:
            pass

    def on_response_paused(self, event):
        request = event["request"]
        mutation = request["method"] not in ("GET", "HEAD", "OPTIONS")
        response_stage = "responseStatusCode" in event or "responseErrorReason" in event
        observed = self._upload_action(request["url"])
        apply = observed[1] if observed and observed[0] == "ApplyUploadInner" else None
        try:
            if not response_stage:
                self.cdp.send("Fetch.continueRequest", {"requestId": event["requestId"],
                                                        "interceptResponse": mutation or apply is not None})
            elif apply is not None and "responseStatusCode" in event and not (300 <= event["responseStatusCode"] < 400):
                # The SDK may start native storage immediately after reading
                # Apply, before any asynchronous Playwright response callback.
                # Register the validated response in-process, then release it.
                self._intercept_apply(event, apply)
                if self.error is not None:
                    self.cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
                else:
                    self.cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
            elif mutation and 300 <= event.get("responseStatusCode", 0) < 400:
                if self.error is None:
                    self.error = (PublishUncertainError("A redirect after Studio publication was blocked; inspect the account")
                                  if self.gate.used else UploadError("A Studio mutation redirect was blocked", stage="studio redirect"))
                self.cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
            else:
                self.cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
        except Exception:
            if self.error is None:
                self.error = (PublishUncertainError("Studio response interception failed after publication") if self.gate.used
                              else UploadError("Studio response interception failed", stage="studio redirect guard"))

    @staticmethod
    def _upload_action(url: str):
        """Return (action, file_type) for a native Studio upload RPC, else None."""
        parts = urlparse(url)
        if not _studio_origin(url) or parts.path.rstrip("/") != "/top/v1":
            return None
        query = parse_qs(parts.query)
        action = query.get("Action", [None])[0]
        if action not in ("ApplyUploadInner", "CommitUploadInner"):
            return None
        file_type = query.get("FileType", [None])[0]
        return action, (file_type if file_type in ("video", "image") else "other")

    def on_response(self, response):
        try:
            if response.request == self.gate.request:
                if 300 <= response.status < 400 or response.status >= 500:
                    raise PublishUncertainError("TikTok returned a server error after the Studio publish request")
                if response.status != 200:
                    raise PublishError("TikTok rejected the native Studio publish request", status_code=response.status)
                data = response.json()
                if not isinstance(data, dict) or type(data.get("status_code")) is not int:
                    raise PublishUncertainError("TikTok returned no conclusive Studio publication result")
                if data["status_code"] != 0:
                    raise PublishError("TikTok rejected the native Studio post", status_code=data["status_code"])
                self.result = data
                return
        except Exception as exc:
            if self.error is None:
                if not isinstance(exc, (PublishError, PublishUncertainError)):
                    exc = PublishUncertainError("The native Studio publication response could not be verified")
                self.error = exc
            return
        observed = self._upload_action(response.url)
        if observed is None:
            return
        action, file_type = observed
        # CDP already validated and registered Apply before page JS saw it.
        if action == "ApplyUploadInner" and self.cdp_apply:
            return

        def read():
            try:
                data = response.json()
            except Exception:
                data = None
            return response.text(), data
        self._observe_upload(action, file_type, response.status, response.headers, read, response.request)

    def _observe_upload(self, action: str, file_type: str, status: int, headers, read, request):
        facts = {}
        try:
            text, data = read()
            diagnostic = SimpleNamespace(status_code=status, headers=CaseInsensitiveDict(headers), text=text,
                                         json=lambda: data)
            facts = upload_diagnostics(diagnostic, payload=data)
            failure = UploadError.from_response("studio " + action, diagnostic, payload=data)
            if status != 200 or failure.provider_code not in (None, 0) or failure.rate_limited:
                raise failure
            if action == "ApplyUploadInner":
                self._apply(file_type, data, facts)
            else:
                self._commit(request, data, facts)
        except Exception as exc:
            if self.error is None:
                if not isinstance(exc, (UploadError, PublishError, PublishUncertainError)):
                    reason = exc.reason if isinstance(exc, _NativeRejected) else "malformed_response"
                    facts.update(reason=reason, action=(action or "other").removesuffix("UploadInner").lower(), file_type=file_type)
                    facts["native"] = " ".join(f"{key}={facts[key]}" for key in _NATIVE_FACTS if key in facts)
                    exc = UploadError("Studio upload response did not contain the required transfer proof",
                                      stage="studio transfer", diagnostics=facts)
                self.error = exc

    def _intercept_apply(self, event, file_type: str):
        """Validate and register an Apply response before the page can read it."""
        def read():
            body = self.cdp.send("Fetch.getResponseBody", {"requestId": event["requestId"]})
            raw = body.get("body", "")
            if not isinstance(raw, str) or len(raw) > MAX_APPLY_BODY:
                raise _NativeRejected("response_too_large")
            if body.get("base64Encoded"):
                raw = base64.b64decode(raw, validate=True).decode("utf-8")
            try:
                data = json.loads(raw)
            except ValueError:
                data = None
            return raw, data
        headers = {}
        for item in event.get("responseHeaders") or ():
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                headers[item["name"]] = str(item.get("value", ""))
        self.apply_intercepted = min(1000, self.apply_intercepted + 1)
        self._observe_upload("ApplyUploadInner", file_type, event["responseStatusCode"], headers, read, None)

    def _apply(self, file_type: str, data, facts: dict):
        if file_type not in ("video", "image"):
            raise _NativeRejected("file_type_unsupported")
        nodes = data["Result"]["InnerUploadAddress"]["UploadNodes"]
        if not isinstance(nodes, list):
            raise _NativeRejected("node_list_invalid")
        facts["nodes"] = len(nodes)
        facts.update(_node_counts(nodes))
        if not 1 <= len(nodes) <= MAX_UPLOAD_NODES:
            raise _NativeRejected("node_count_invalid")
        video = file_type == "video"
        candidates, keys, vids, storage = set(), set(), set(), set()
        for node in nodes:
            if not isinstance(node, dict):
                raise _NativeRejected("node_invalid")
            try:
                storage.update(self.gate.storage_for(node))
            except (KeyError, TypeError, ValueError):
                raise _NativeRejected("storage_invalid") from None
            key, vid = node.get("SessionKey"), node.get("Vid")
            try:
                key = _upload_string(key, 8192)
            except ValueError:
                raise _NativeRejected("session_key_invalid") from None
            if video:
                try:
                    vid = _upload_string(vid, 256)
                except ValueError:
                    raise _NativeRejected("vid_invalid") from None
                candidates.add((key, vid))
                continue
            # Cover images may carry an empty Vid, but the observed response
            # still has a session key. Track it so commits cannot be
            # confused with the video; they can never select media_id.
            keys.add(key)
            if vid is not None and not isinstance(vid, str):
                raise _NativeRejected("vid_invalid")
            if vid:
                try:
                    vids.add(_upload_string(vid, 256))
                except ValueError:
                    raise _NativeRejected("vid_invalid") from None
        video_keys = {key for key, _ in self.video_candidates | candidates}
        video_vids = {vid for _, vid in self.video_candidates | candidates}
        aux_keys, aux_vids = self.aux_keys | keys, self.aux_vids | vids
        if (len(self.video_candidates | candidates) > MAX_UPLOAD_NODES or len(aux_keys) > MAX_UPLOAD_NODES
                or len(aux_vids) > MAX_UPLOAD_NODES
                or len(self.gate.storage_paths | storage) > MAX_UPLOAD_NODES * MAX_STORE_INFOS):
            raise _NativeRejected("limit_exceeded")
        if video_keys & aux_keys or video_vids & aux_vids:
            raise _NativeRejected("identity_conflict")
        # Validate the complete authenticated response before extending
        # transfer permissions. Node order never selects the video.
        self.video_candidates.update(candidates)
        self.aux_keys, self.aux_vids = aux_keys, aux_vids
        self.gate.storage_paths.update(storage)

    def _commit(self, request, data, facts: dict):
        try:
            key = request.post_data_json["SessionKey"]
        except (KeyError, TypeError, ValueError):
            key = None
        try:
            key = _upload_string(key, 8192)
        except ValueError:
            raise _NativeRejected("commit_session_invalid") from None
        result = data.get("Result") if isinstance(data, dict) else None
        results = result.get("Results") if isinstance(result, dict) else None
        facts["results"] = len(results) if isinstance(results, list) else 0
        video_vids = {vid for _, vid in self.video_candidates}
        if key in self.aux_keys:
            if isinstance(key, str) and key in {k for k, _ in self.video_candidates}:
                raise _NativeRejected("identity_conflict")
            # A known auxiliary commit is never video proof, but it must not
            # name the video either.
            if isinstance(results, list) and any(isinstance(item, dict) and item.get("Vid") in video_vids
                                                 for item in results[:MAX_UPLOAD_NODES]):
                raise _NativeRejected("commit_identity_ambiguous")
            return
        if not isinstance(results, list) or not 1 <= len(results) <= MAX_UPLOAD_NODES:
            raise _NativeRejected("commit_results_invalid")
        try:
            identities = {_upload_string(item["Vid"], 256) for item in results}
        except (KeyError, TypeError, ValueError):
            raise _NativeRejected("commit_results_invalid") from None
        if len(identities) != 1:
            raise _NativeRejected("commit_identity_ambiguous")
        media_id = identities.pop()
        if (key, media_id) not in self.video_candidates:
            raise _NativeRejected("commit_identity_unknown")
        if self.media_id is not None and self.media_id != media_id:
            raise _NativeRejected("commit_identity_conflict")
        self.media_id, self.session_key = media_id, key
        self.committed = True

class BrowserStudioClient:
    def __init__(self, account: Account, *, expected_remote_account_id: str, timeout=20, guard=None, label: str = ""):
        self.account, self.timeout, self.guard, self.label = account, timeout, guard or (lambda: None), label
        self.browser_wait_guard = guard
        self.expected_remote_account_id = expected_remote_account_id

    def prepare(self, video, caption, *, creation_id=None, visibility="private", allow_comment=True,
                allow_duet=False, allow_stitch=False, ai_label=False):
        self.guard()
        if not isinstance(caption, str) or not caption.strip() or len(caption) > MAX_CAPTION_LENGTH:
            raise ValidationError(f"caption must contain 1–{MAX_CAPTION_LENGTH} characters")
        if ai_label:
            raise ValidationError("Studio mode does not yet implement the AI label control")
        if not self.expected_remote_account_id:
            raise ValidationError("Studio mode requires the verified remote account ID")
        path = resolve_video_path(video)
        if not path.stat().st_size:
            raise ValidationError("video must be a non-empty file")
        prepared = {"path": path, "caption": caption, "visibility": _visibility(visibility),
                    "allow_comment": allow_comment, "allow_duet": allow_duet, "allow_stitch": allow_stitch}
        session = _StudioSession(self.account, prepared, self.guard, self.timeout, self.label, self.browser_wait_guard)
        prepared["session"] = session
        try:
            session.page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=self.timeout * 1000)
            session.check()
            result = session.page.evaluate("""async timeout => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeout);
                try {
                    const r = await fetch('/passport/web/account/info/?aid=1459', {
                        credentials: 'include', signal: controller.signal});
                    return {status: r.status, body: await r.json()};
                } finally { clearTimeout(timer); }
            }""", self.timeout * 1000)
            try:
                identity = (_identity(result["body"], bool(self.account.proxy), "studio")
                            if result["status"] == 200 and isinstance(result["body"], dict) else None)
            except (AttributeError, KeyError, TypeError, ValueError):
                raise SessionRejected("Studio returned no verified account identity") from None
            if identity is None or identity.remote_account_id != str(self.expected_remote_account_id):
                raise SessionRejected("Studio did not verify the authorized TikTok account")
            session.check()
            return prepared
        except BaseException:
            session.close()
            raise

    def transfer(self, prepared):
        session = prepared["session"]
        session.check()
        session.wait(lambda: session.page.locator("input[type='file']").count() > 0,
                     self.timeout, "Studio video input did not appear")
        inputs = session.page.locator("input[type='file'][accept*='video']")
        if inputs.count() == 0:
            inputs = session.page.locator("input[type='file']")
        if inputs.count() != 1:
            raise UploadError("Studio video input is missing or ambiguous", stage="studio transfer")
        inputs.set_input_files(str(prepared["path"]), timeout=self.timeout * 1000)
        session.wait(lambda: session.media_id is not None and session.committed,
                     300, "Studio did not confirm the video transfer")
        prepared["media_id"] = session.media_id
        return session.media_id

    def prepare_publish(self, prepared):
        session, page = prepared["session"], prepared["session"].page
        session.check()
        editor = page.locator("div[contenteditable='true']")
        session.wait(lambda: editor.count() > 0, self.timeout, "Studio caption editor did not appear")
        if editor.count() != 1:
            raise UploadError("Studio caption editor is missing or ambiguous", stage="studio metadata")
        editor.fill(prepared["caption"], timeout=self.timeout * 1000)
        if editor.inner_text() != prepared["caption"]:
            raise UploadError("Studio did not retain the exact caption", stage="studio metadata")
        label = "Only you" if prepared["visibility"] == 1 else "Everyone"
        dropdown = page.locator("[data-e2e='video_visibility_container'] button[role='combobox']")
        dropdown.click(timeout=self.timeout * 1000)
        page.get_by_role("option", name=label, exact=True).click(timeout=self.timeout * 1000)
        if label not in dropdown.inner_text():
            raise UploadError("Studio did not confirm the selected privacy", stage="studio privacy")
        for label, key in (("Comment", "allow_comment"), ("Duet", "allow_duet"), ("Stitch", "allow_stitch")):
            control = page.locator(f"xpath=//label[.='{label}']/following-sibling::div/input")
            if control.count() == 1 and control.is_visible():
                control.set_checked(bool(prepared[key]), timeout=self.timeout * 1000)
            # Some privacy/account types hide these controls. The native payload
            # still has to prove the exact desired values before any final send.
        button = page.locator(POST_BUTTON)
        session.wait(lambda: button.count() == 1 and button.is_enabled() and button.get_attribute("data-disabled") != "true",
                     self.timeout, "Studio Post button did not become ready")
        prepared["ready"] = True
        return prepared

    def submit(self, prepared, before_send):
        session, page = prepared["session"], prepared["session"].page
        session.check()
        if not prepared.get("ready") or session.gate.armed or session.gate.used:
            raise UploadError("Studio publication was not prepared or was already attempted", stage="studio submit")
        session.gate.before_send = before_send
        session.gate.armed = True
        confirmed = False
        try:
            # There is deliberately no click retry or JavaScript click fallback.
            page.locator(POST_BUTTON).click(timeout=self.timeout * 1000)
            def finished():
                nonlocal confirmed
                if session.result is not None:
                    return True
                if not session.gate.used and not confirmed:
                    confirm = page.get_by_role("button", name="Post now", exact=True)
                    if confirm.count() == 1 and confirm.is_visible():
                        confirmed = True
                        confirm.click(timeout=self.timeout * 1000)
                return False
            session.wait(finished, self.timeout, "Studio returned no native publish response")
            return session.result
        except Exception as exc:
            if session.gate.error is not None:
                raise session.gate.error
            if isinstance(exc, (PublishError, PublishUncertainError)):
                raise
            if session.gate.used:
                raise PublishUncertainError("Studio publication was sent, but its outcome is not verified") from exc
            raise
