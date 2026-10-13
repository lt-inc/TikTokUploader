from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
import uuid
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pika
from autotok import Account, Client
from autotok.errors import AutotokError, NotLoggedInError, PublishError, PublishUncertainError
from autotok.uploader import _creation_id

from .browser import browser_permit
from .config import Config
from .connections import Connections, iso_now
from .identity import SessionRejected, verify_identity
from .main_client import Fenced, Lease, MainClient
from .media import download_media
from .metrics import ACTIVE, BROKER, STAGES, TRANSFER, UPLOADS
from .reconcile import inspect_posts, response_post_ids
from .store import EncryptedStore
from .ownership import ProcessOwnership

log = logging.getLogger("tiktok.worker")
QUEUE = "tiktok.workflow.v1.publish"


class Worker:
    def __init__(self, config: Config, store: EncryptedStore, connections: Connections, owner: ProcessOwnership | None = None, browser_help=None):
        self.config, self.store, self.connections = config, store, connections
        self.main = MainClient(config)
        self.worker_id = f"tiktok-{uuid.uuid4()}"
        self.stop = threading.Event()
        self.broker_connected = False
        self.thread = threading.Thread(target=self.run, daemon=True, name="tiktok-publisher")
        self.owner = owner
        self.browser_help = browser_help

    def start(self):
        if not self.owner or not self.owner.held:
            raise RuntimeError("The publisher must own the session volume before starting")
        self.store.cleanup_abandoned()
        self.config.scratch_dir.mkdir(parents=True, exist_ok=True)
        scratch = self.config.scratch_dir.resolve()
        state = self.config.state_dir.resolve()
        if scratch == state or state.is_relative_to(scratch):
            raise ValueError("Persistent encrypted state cannot be inside scratch")
        for orphan in scratch.glob("publish-*"):
            target = orphan.resolve()
            if orphan.is_dir() and not orphan.is_symlink() and target.is_relative_to(scratch):
                shutil.rmtree(target)
        self.drain_previous_owner()
        self.thread.start()

    def drain_previous_owner(self):
        if not self.owner or not self.owner.held:
            raise RuntimeError("Boot drain requires exclusive process ownership")
        for journal in self.store.all("publication"):
            if journal.get("worker_id") != self.worker_id and journal.get("lease_token") and not journal.get("operation_released"):
                # We hold the PV's OS lock: the prior local publisher process
                # cannot still be executing. Preserve submitting/accepted as
                # unknown; acknowledge only the ended physical operation.
                journal["operation_ended"] = True
                journal["ended_reason"] = "previous_process_terminated"
                self.store.put("publication", journal["publication_id"], journal)
                try:
                    self.release_ended(journal)
                except Exception as exc:
                    log.info("boot operation release pending publication_id=%s error_type=%s", journal["publication_id"], type(exc).__name__)

    def _checkpoint(self, lease: Lease, journal: dict, state: str, *, submitting=False):
        checkpoint = {k: journal.get(k) for k in ("creation_id", "media_id", "phase", "submitted_at", "post_ids", "caption") if journal.get(k) is not None}
        lease.checkpoint(state, checkpoint, final_request_started=submitting)
        log.info("publishing stage", extra={"publication_id": journal["publication_id"], "attempt_id": journal["attempt_id"], "phase": state})

    def _failure(self, lease: Lease, state: str, code: str, message: str, retryable=False, reconnect_required=False):
        response = self.main.call("fail", {**lease.fields, "state": state, "code": code, "error_code": code,
            "error": message, "message": message, "retryable": retryable, "reconnect_required": reconnect_required})
        if response.get("accepted") is False:
            raise Fenced("Terex rejected the failure report")

    def release_ended(self, journal: dict):
        if not journal.get("operation_ended") or journal.get("operation_released"):
            return
        fields = {key: journal[key] for key in ("attempt_id", "lease_token", "expected_version")}
        result = self.main.call("release", fields)
        if result.get("accepted") is not True:
            raise Fenced("Terex did not acknowledge the ended operation")
        journal["operation_released"] = True
        self.store.put("publication", journal["publication_id"], journal)

    def reconcile(self, journal: dict) -> bool:
        row = self.connections.for_verification(journal["account_id"], journal["remote_account_id"])
        if not journal.get("proof"):
            proof = inspect_posts(row, journal, self.config.proxy_url)
            if not proof:
                return False
            journal["proof"] = proof
            self.store.put("publication", journal["publication_id"], journal)
        proof = journal["proof"]
        result = self.main.call("reconcile", {"publication_id": journal["publication_id"], "expected_version": journal["expected_version"],
            "session_version": journal["session_version"], "remote_account_id": journal["remote_account_id"],
            "remote_id": proof["remote_id"], "remote_url": proof["remote_url"], "verified_at": proof["verified_at"]})
        if result.get("accepted") is True:
            journal["phase"] = "published"
            self.store.put("publication", journal["publication_id"], journal)
            UPLOADS.labels("reconciled").inc()
            return True
        return False

    def execute(self, command: dict):
        raw_id = command.get("publication_id")
        if isinstance(raw_id, bool):
            raise ValueError("Invalid publication ID")
        publication_id = int(raw_id)
        if publication_id <= 0:
            raise ValueError("Invalid publication ID")
        previous = self.store.get("publication", publication_id)
        if previous:
            self.release_ended(previous)
        unknown = previous and previous.get("phase") in ("submitting", "accepted", "published")
        if unknown and (previous.get("phase") == "published" or int(command.get("expected_version", 0)) <= int(previous["expected_version"])):
            # Broker redelivery, restart and lost Complete never re-upload.
            if previous["phase"] != "published":
                self.reconcile(previous)
            return
        claim = self.main.call("claim", {"kind": "publish", "publication_id": publication_id,
            "attempt_id": command["attempt_id"], "expected_version": command["expected_version"], "worker_id": self.worker_id})
        if not claim.get("granted"):
            return
        publication = claim["publication"]
        if publication.get("provider") != "tiktok":
            raise ValueError("The worker can only publish to TikTok")
        lease = Lease(self.main, claim)
        if unknown:
            if not publication.get("retry_authorized") or int(claim["expected_version"]) <= int(previous["expected_version"]):
                try:
                    self._failure(lease, "needs_attention", "upload_unknown", "Previous publication is uncertain. Explicit operator confirmation of absence is required before retrying.")
                finally:
                    self.main.call("release", lease.fields)
                return
            # Preserve the uncertain attempt while starting the explicitly
            # authorized newer publication version. Old redeliveries stay fenced.
            self.store.put("attempt", previous["attempt_id"], previous)
        journal = {"publication_id": publication_id, "attempt_id": claim["attempt_id"], "lease_token": claim["lease_token"], "worker_id": self.worker_id,
            "expected_version": claim["expected_version"],
            "account_id": int(publication["account_id"]), "remote_account_id": str(publication["remote_account_id"]),
            "session_version": publication["session_version"], "caption": publication.get("metadata", {}).get("caption", ""),
            "creation_id": _creation_id(), "phase": "preparing"}
        self.store.put("publication", publication_id, journal)
        lease.start()
        ACTIVE.inc()
        prepared = None
        started = time.monotonic()
        deadline = started + 900
        external_started = False
        def guard():
            lease.check()
            if self.stop.is_set() or time.monotonic() >= deadline:
                raise Fenced("Publication stopped or exceeded the operation deadline")
            self.connections.require(journal["account_id"], journal["session_version"], journal["remote_account_id"])
        try:
            # Durable local claim + acknowledged registration precede all
            # physical work. Lost Claim responses leave no orphan operation.
            self._checkpoint(lease, journal, "uploading")
            row = self.connections.require(journal["account_id"], journal["session_version"], journal["remote_account_id"])
            identity = verify_identity(row["cookies"], row["user_agent"], self.config.proxy_url)
            if identity.remote_account_id != journal["remote_account_id"]:
                raise SessionRejected("TikTok returned a different account identity")
            with tempfile.TemporaryDirectory(prefix="publish-", dir=self.config.scratch_dir) as tmp:
                path = Path(tmp) / "video.mp4"
                with STAGES.labels("download").time():
                    size = download_media(publication["media_url"], publication["media_sha256"], path, self.config, guard)
                account = Account(name=f"account-{journal['account_id']}", cookies=row["cookies"], user_agent=row["user_agent"], proxy=self.config.proxy_url)
                uploader = Client(account, timeout=20, guard=guard)
                with STAGES.labels("prepare").time():
                    prepared = uploader.prepare(path, journal["caption"], creation_id=journal["creation_id"], visibility=publication.get("visibility", "private"))
                    journal["phase"] = "prepared"
                    self.store.put("publication", publication_id, journal)
                    self._checkpoint(lease, journal, "uploading")
                with STAGES.labels("transfer").time():
                    journal["media_id"] = uploader.transfer(prepared)
                    journal["phase"] = "transferred"
                    self.store.put("publication", publication_id, journal)
                    self._checkpoint(lease, journal, "uploading")
                    TRANSFER.labels("upload").inc(size)
                if self.browser_help:
                    # Keep the transferred project while the operator solves
                    # any device/CAPTCHA check in this very browser.
                    row = self.browser_help.verify_for_publish(row, guard, lambda: self._checkpoint(lease, journal, "processing"))
                    for cookie in row["cookies"]:
                        prepared["session"].cookies.set(cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/"))
                with STAGES.labels("prepare_publish").time(), browser_permit():
                    uploader.prepare_publish(prepared)
                def barrier():
                    nonlocal external_started
                    guard()
                    journal.update(phase="submitting", submitted_at=time.time())
                    # Local journal protects response loss while the main-be
                    # durable acknowledgement fences the actual send.
                    self.store.put("publication", publication_id, journal)
                    self._checkpoint(lease, journal, "publishing", submitting=True)
                    external_started = True
                with STAGES.labels("submit").time():
                    response = uploader.submit(prepared, barrier)
                journal.update(phase="accepted", post_ids=sorted(response_post_ids(response)))
                self.store.put("publication", publication_id, journal)
                self._checkpoint(lease, journal, "verifying", submitting=True)
                with STAGES.labels("verify").time():
                    proof = inspect_posts(row, journal, self.config.proxy_url, guard)
                if not proof:
                    self._failure(lease, "needs_attention", "upload_unknown", "TikTok accepted the request, but the actual post is not yet verified. No automatic re-upload.")
                    UPLOADS.labels("unverified").inc()
                    return
                journal["proof"] = proof
                self.store.put("publication", publication_id, journal)
                result = self.main.call("complete", {**lease.fields, "state": "published", "final_request_started": True,
                    "remote_id": proof["remote_id"], "remote_url": proof["remote_url"],
                    "verified_at": proof["verified_at"], "provider_checkpoint": {"creation_id": journal["creation_id"], "media_id": journal["media_id"], "proof": proof["proof"]}})
                if result.get("accepted") is not True:
                    raise Fenced("Terex did not accept the confirmed publication")
                journal["phase"] = "published"
                self.store.put("publication", publication_id, journal)
                UPLOADS.labels("published").inc()
                log.info("post confirmed", extra={"publication_id": publication_id, "attempt_id": claim["attempt_id"], "size_bytes": size, "result": "published"})
        except PublishError:
            journal["phase"] = "rejected"
            self.store.put("publication", publication_id, journal)
            self._failure(lease, "needs_attention", "tiktok_rejected", "TikTok rejected publication; review the account and video before retrying.")
            UPLOADS.labels("rejected").inc()
        except (NotLoggedInError, SessionRejected):
            with self.store.lock:
                current = self.store.get("account", journal["account_id"])
                if current and current.get("session_version") == journal["session_version"]:
                    current.update(status="needs_reauth", error="Reconnect this TikTok account or finish browser verification.")
                    self.store.put("account", journal["account_id"], current)
            self._failure(lease, "blocked", "auth_required", "TikTok account needs reconnection or browser verification.", reconnect_required=True)
            UPLOADS.labels("needs_reauth").inc()
        except Exception as exc:
            if external_started or journal.get("phase") in ("submitting", "accepted"):
                self._failure(lease, "needs_attention", "upload_unknown", "The final publication result is uncertain. Check the post; automatic upload retry is blocked.")
                UPLOADS.labels("uncertain").inc()
            else:
                self._failure(lease, "failed", "upload_preflight_failed", "Preparation/transfer failed before publication; safe to retry.", retryable=True)
                UPLOADS.labels("preflight_failed").inc()
            log.warning("upload failed publication_id=%s attempt_id=%s phase=%s error_type=%s", publication_id, claim["attempt_id"], journal["phase"], type(exc).__name__)
        finally:
            if prepared:
                prepared["session"].close()
            lease.close()
            ACTIVE.dec()
            log.info("publishing operation ended", extra={"publication_id": publication_id, "attempt_id": claim["attempt_id"], "phase": journal["phase"], "duration_ms": round((time.monotonic() - started) * 1000)})
            # Reset maintenance waits for physical operations to end. Lease
            # expiry alone must never be treated as an acknowledgement.
            journal["operation_ended"] = True
            self.store.put("publication", publication_id, journal)
            self.release_ended(journal)

    def recover(self):
        for journal in self.store.all("publication"):
            try:
                self.release_ended(journal)
            except Exception as exc:
                log.info("operation release pending publication_id=%s error_type=%s", journal["publication_id"], type(exc).__name__)
            if journal.get("phase") in ("submitting", "accepted"):
                try:
                    self.reconcile(journal)
                except Exception as exc:
                    log.info("reconciliation pending publication_id=%s error_type=%s", journal["publication_id"], type(exc).__name__)

    def run(self):
        while not self.stop.is_set():
            connection = None
            try:
                params = pika.URLParameters(self.config.rabbit_url)
                params.heartbeat = 600
                params.blocked_connection_timeout = 30
                params.connection_attempts = 1
                params.socket_timeout = 10
                connection = pika.BlockingConnection(params)
                channel = connection.channel()
                channel.queue_declare(queue=QUEUE, durable=True)
                channel.basic_qos(prefetch_count=1)
                self.broker_connected = True
                BROKER.set(1)
                # Broker I/O stays on this thread even during long transfers.
                # One executor preserves one upload; prefetch=1 preserves one
                # unacknowledged command while heartbeats continue normally.
                next_recovery, pending, delivery_tag = 0, None, None
                with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tiktok-execution") as executor:
                    for method, properties, body in channel.consume(QUEUE, inactivity_timeout=1, auto_ack=False):
                        if self.stop.is_set():
                            break
                        if pending is not None and pending.done():
                            try:
                                pending.result()
                                if delivery_tag is not None:
                                    channel.basic_ack(delivery_tag)
                            except (KeyError, ValueError, json.JSONDecodeError):
                                log.error("invalid publishing command rejected")
                                if delivery_tag is not None:
                                    channel.basic_reject(delivery_tag, requeue=False)
                            except Exception as exc:
                                log.warning("publishing command deferred error_type=%s", type(exc).__name__)
                                if delivery_tag is not None:
                                    channel.basic_nack(delivery_tag, requeue=True)
                            pending, delivery_tag = None, None
                        if method is not None:
                            try:
                                command = json.loads(body)
                                pending = executor.submit(self.execute, command)
                                delivery_tag = method.delivery_tag
                            except (ValueError, json.JSONDecodeError):
                                channel.basic_reject(method.delivery_tag, requeue=False)
                        elif pending is None and time.monotonic() >= next_recovery:
                            pending = executor.submit(self.recover)
                            next_recovery = time.monotonic() + 60
                channel.cancel()
            except Exception as exc:
                log.warning("broker unavailable error_type=%s", type(exc).__name__)
                self.stop.wait(5)
            finally:
                self.broker_connected = False
                BROKER.set(0)
                if connection and connection.is_open:
                    connection.close()
