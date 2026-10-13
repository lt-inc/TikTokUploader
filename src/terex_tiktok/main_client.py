from __future__ import annotations

import threading
import time
from datetime import datetime

import httpx

from .config import Config


class Fenced(Exception):
    pass


class MainClient:
    def __init__(self, config: Config):
        self.client = httpx.Client(base_url=config.main_url, headers={"Authorization": f"Bearer {config.worker_token}"},
                                   timeout=httpx.Timeout(20, connect=5), trust_env=False)

    def call(self, operation: str, payload: dict) -> dict:
        response = self.client.post(f"/api/tiktok/worker/{operation}", json=payload)
        response.raise_for_status()
        return response.json()


class Lease:
    def __init__(self, client: MainClient, claim: dict):
        self.client = client
        self.fields = {k: claim[k] for k in ("attempt_id", "lease_token", "expected_version")}
        self.expiry = self._expiry(claim["expires_at"])
        self.cancel = threading.Event()
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._renew, daemon=True)

    @staticmethod
    def _expiry(value: str) -> float:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()

    def check(self) -> None:
        if self.cancel.is_set() or time.time() >= self.expiry - 5:
            raise Fenced("Publishing lease is no longer valid")

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=25)

    def _renew(self) -> None:
        while not self.stopped.wait(20):
            for delay in (0, 1, 2, 5):
                if self.stopped.wait(delay):
                    return
                try:
                    self.check()
                    result = self.client.call("renew", self.fields)
                    if not result.get("allowed") or not result.get("can_publish", True):
                        self.cancel.set()
                        return
                    self.expiry = self._expiry(result["expires_at"])
                    break
                except Fenced:
                    self.cancel.set()
                    return
                except (httpx.HTTPError, KeyError, ValueError):
                    if time.time() >= self.expiry - 5:
                        self.cancel.set()
                        return

    def checkpoint(self, state: str, provider_checkpoint: dict, final_request_started: bool = False,
                   remote_id: str = "", remote_url: str = "", verified_at: str | None = None) -> None:
        self.check()
        result = self.client.call("checkpoint", {**self.fields, "state": state, "provider_checkpoint": provider_checkpoint,
            "final_request_started": final_request_started, "remote_id": remote_id, "remote_url": remote_url, "verified_at": verified_at})
        if result.get("accepted") is not True:
            self.cancel.set()
            raise Fenced("Terex rejected the publishing checkpoint")
        self.check()
