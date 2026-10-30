"""Experimental local TikTok Studio uploader.

The live site constructs upload and publish requests. The final request is
validated and fenced before the browser is permitted to send it; UI messages
and redirects are never evidence that a post exists.
"""
from __future__ import annotations

import time
from contextlib import ExitStack
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from requests.structures import CaseInsensitiveDict

from autotok.accounts import Account
from autotok.browsers import launch_chromium, sync_playwright
from autotok.errors import PublishError, PublishUncertainError, UploadError, ValidationError
from autotok.uploader import MAX_CAPTION_LENGTH, _visibility, resolve_video_path

from .browser import browser_permit
from .identity import SessionRejected, _identity, checked_cookies

STUDIO_URL = "https://www.tiktok.com/tiktokstudio/upload"
POST_PATHS = {"/tiktok/web/project/post/v1", "/api/v1/web/project/post"}
POST_BUTTON = "button[data-e2e='post_video_button']"
PROJECT_PREPARATION_PATHS = {
    "/api/v1/web/project/create", "/api/v1/web/project/update", "/api/v1/web/project/save",
    "/tiktok/web/project/create/v1", "/tiktok/web/project/update/v1", "/tiktok/web/project/save/v1",
}


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

    def allow_storage(self, node: dict):
        host = node.get("UploadHost")
        if not isinstance(host, str) or not host or "/" in host or "@" in host:
            raise ValueError("Invalid upload host")
        address = urlparse("https://" + host)
        if (address.netloc != host or address.query or address.fragment or address.port is not None
                or _tiktok_host(address.hostname)):
            raise ValueError("Invalid upload storage origin")
        for item in node["StoreInfos"]:
            uri = item["StoreUri"]
            if not isinstance(uri, str) or "?" in uri or "#" in uri:
                raise ValueError("Invalid storage URI")
            self.storage_paths.add((host.lower(), "/" + uri.lstrip("/")))

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
    def __init__(self, account: Account, prepared: dict, guard, timeout: float, label: str = ""):
        self.stack = ExitStack()
        self.guard, self.prepared, self.timeout = guard, prepared, timeout
        self.gate = _PublishGate(prepared, self._request_guard)
        self.error = None
        self.media_id = None
        self.session_key = None
        self.committed = False
        self.result = None
        self.closed = False
        try:
            self.stack.enter_context(browser_permit(guard=guard, label=label))
            pw = self.stack.enter_context(sync_playwright())
            browser = launch_chromium(pw, headless=True, proxy=account.get_proxy())
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
            self.cdp = self.context.new_cdp_session(self.page)
            self.stack.callback(self.cdp.detach)
            self.cdp.on("Fetch.requestPaused", self.on_response_paused)
            self.cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
        except BaseException:
            self.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            self.stack.close()

    def export_cookies(self) -> list[dict]:
        return checked_cookies([c for c in self.context.cookies() if _tiktok_host(c.get("domain", "").lstrip("."))])

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
        raise UploadError(message, stage="studio wait")

    def on_request_failed(self, request):
        if request == self.gate.request and self.error is None:
            self.error = PublishUncertainError("The Studio publish response was lost; inspect the account")

    def on_response_paused(self, event):
        request = event["request"]
        redirect = (request["method"] not in ("GET", "HEAD", "OPTIONS")
                    and 300 <= event.get("responseStatusCode", 0) < 400)
        try:
            if redirect:
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
            parts = urlparse(response.url)
            if not _studio_origin(response.url) or parts.path.rstrip("/") != "/top/v1":
                return
            query = parse_qs(parts.query)
            action = query.get("Action", [None])[0]
            if action not in ("ApplyUploadInner", "CommitUploadInner"):
                return
            try:
                data = response.json()
            except Exception:
                data = None
            diagnostic = SimpleNamespace(status_code=response.status, headers=CaseInsensitiveDict(response.headers),
                                         text=response.text(), json=lambda: data)
            failure = UploadError.from_response("studio " + action, diagnostic, payload=data)
            if response.status != 200 or failure.provider_code not in (None, 0) or failure.rate_limited:
                raise failure
            if action == "ApplyUploadInner":
                nodes = data["Result"]["InnerUploadAddress"]["UploadNodes"]
                for node in nodes:
                    self.gate.allow_storage(node)
                if query.get("FileType", [None])[0] != "video":
                    return
                if len(nodes) != 1 or not nodes[0]["Vid"] or not nodes[0]["SessionKey"]:
                    raise ValueError
                media_id = str(nodes[0]["Vid"])
                if self.media_id is not None and self.media_id != media_id:
                    raise UploadError("Studio returned more than one video upload", stage="studio apply")
                self.media_id, self.session_key = media_id, nodes[0]["SessionKey"]
            else:
                if not self.media_id or response.request.post_data_json.get("SessionKey") != self.session_key:
                    return
                results = data["Result"]["Results"]
                if not any(str(item.get("Vid")) == self.media_id for item in results if isinstance(item, dict)):
                    raise ValueError
                self.committed = True
        except Exception as exc:
            if self.error is None:
                if response.request == self.gate.request and not isinstance(exc, (PublishError, PublishUncertainError)):
                    exc = PublishUncertainError("The native Studio publication response could not be verified")
                elif not isinstance(exc, (UploadError, PublishError, PublishUncertainError)):
                    exc = UploadError("Studio upload response did not contain the required transfer proof", stage="studio transfer")
                self.error = exc


class BrowserStudioClient:
    def __init__(self, account: Account, *, expected_remote_account_id: str, timeout=20, guard=lambda: None, label: str = ""):
        self.account, self.timeout, self.guard, self.label = account, timeout, guard, label
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
        session = _StudioSession(self.account, prepared, self.guard, self.timeout, self.label)
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
