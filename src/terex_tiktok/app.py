from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import websockets
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .config import Config
from .connections import Connections
from .help import BrowserHelp
from .metrics import CONNECTED
from .reconcile import inspect_posts
from .store import EncryptedStore
from .worker import Worker
from .ownership import ProcessOwnership

log = logging.getLogger("tiktok.api")


class Pairing(BaseModel):
    account_id: int = Field(gt=0)
    pairing_token: str
    expires_at: str
    expected_remote_account_id: str | None = None
    webhook_url: str | None = None


class Export(BaseModel):
    pairing_token: str = Field(max_length=256)
    cookies: list[dict] = Field(max_length=200)
    user_agent: str = Field(max_length=1024)
    origin: str


class VerifyPost(BaseModel):
    publication_id: int = Field(gt=0)
    account_id: int
    remote_account_id: str
    remote_id: str | None = None
    operator_confirmed: bool = False


def create_app(config: Config | None = None, *, start_worker=True) -> FastAPI:
    config = config or Config.from_env()
    store = EncryptedStore(config.state_dir, config.encryption_key)
    connections = Connections(store, config)
    help_manager = BrowserHelp(connections)
    owner = ProcessOwnership(config.state_dir)
    worker = Worker(config, store, connections, owner, help_manager)

    @asynccontextmanager
    async def lifespan(app):
        owner.acquire()
        try:
            if start_worker:
                worker.start()
            yield
        finally:
            worker.stop.set()
            for session in help_manager.sessions.values():
                session["stop"].set()
            if start_worker:
                # Keep ownership while a blocked network call is ending. A
                # subsequent process must not drain an operation still running.
                await asyncio.to_thread(worker.thread.join)
            owner.release()

    app = FastAPI(title="Terex TikTok Uploader", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.connections, app.state.store, app.state.worker = connections, store, worker

    @app.middleware("http")
    async def request_limit(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > 1024 * 1024):
            return Response(status_code=413)
        return await call_next(request)

    def service(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization, f"Bearer {config.api_token}"):
            raise HTTPException(401, "Service authentication required")

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unavailable(request, exc):
        from fastapi.responses import JSONResponse
        log.warning("request unavailable method=%s error_type=%s", request.method, type(exc).__name__)
        return JSONResponse(status_code=503, content={"detail": "TikTok service temporarily unavailable"})

    @app.get("/healthz")
    def health():
        return {"ok": True}

    @app.get("/source")
    def source():
        revision = os.environ.get("TIKTOK_SOURCE_REVISION", "codex/terex-tiktok-uploader")
        return {"license": "AGPL-3.0-only", "revision": revision,
                "source_url": f"https://github.com/lt-inc/TikTokUploader/tree/{revision}"}

    @app.get("/readyz")
    def ready():
        # API/session helpers remain ready during broker outage; publishing
        # availability is visible as a distinct metric instead of disconnecting UI.
        return {"ok": config.state_dir.is_dir(), "worker_connected": worker.broker_connected}

    @app.get("/metrics")
    def metrics():
        counts = {}
        for row in store.all("account"):
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        for state in ("connected", "pairing", "needs_reauth", "challenge", "disconnected"):
            CONNECTED.labels(state).set(counts.get(state, 0))
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/v1/connections", dependencies=[Depends(service)])
    def start(payload: Pairing):
        return connections.start(payload.account_id, payload.pairing_token, payload.expires_at, payload.expected_remote_account_id)

    @app.get("/v1/connections/{account_id}", dependencies=[Depends(service)])
    def status(account_id: int):
        return connections.public(store.get("account", account_id) or {"account_id": account_id, "status": "disconnected", "session_version": 0})

    @app.post("/v1/connect/payload")
    def export(payload: Export):
        # Deliberately pairing-token-only. The public Terex route forwards this
        # exact payload; operator credentials are never sent to the extension.
        return connections.receive(payload.pairing_token, payload.cookies, payload.user_agent, payload.origin)

    @app.post("/v1/connections/{account_id}/check", dependencies=[Depends(service)])
    def check(account_id: int):
        return connections.check(account_id)

    @app.delete("/v1/connections/{account_id}", dependencies=[Depends(service)])
    def disconnect(account_id: int):
        help_manager.cancel_account(account_id)
        return connections.disconnect(account_id)

    @app.post("/v1/connections/{account_id}/browser-help", dependencies=[Depends(service)])
    def browser_help(account_id: int):
        return help_manager.start(account_id)

    @app.post("/v1/publications/verify", dependencies=[Depends(service)])
    def verify(payload: VerifyPost):
        journal = store.get("publication", payload.publication_id)
        if not journal or journal["account_id"] != payload.account_id or journal["remote_account_id"] != payload.remote_account_id:
            return {"verified": False}
        row = connections.for_verification(payload.account_id, payload.remote_account_id)
        proof = journal.get("proof")
        if proof and payload.remote_id and proof["remote_id"] != payload.remote_id:
            return {"verified": False}
        if not proof:
            candidate = dict(journal)
            if payload.remote_id:
                known = candidate.get("post_ids", [])
                if known and payload.remote_id not in known:
                    if not payload.operator_confirmed:
                        return {"verified": False}
                if payload.operator_confirmed:
                    # Only main-be's operator-authorized URL reconciliation
                    # emits this flag. Automatic worker reconciliation never does.
                    candidate["post_ids"] = [payload.remote_id]
                    candidate["operator_confirmed"] = True
            proof = inspect_posts(row, candidate, config.proxy_url)
            if proof and payload.remote_id and proof["remote_id"] != payload.remote_id:
                return {"verified": False}
        if not proof:
            return {"verified": False}
        journal["proof"] = proof
        if payload.operator_confirmed:
            journal["operator_confirmation"] = {"remote_id": payload.remote_id, "verified_at": proof["verified_at"]}
        store.put("publication", payload.publication_id, journal)
        return {"verified": True, **proof, "remote_account_id": payload.remote_account_id}

    @app.get("/api/tiktok/browser-help/{token}/status")
    def help_status(token: str):
        session = help_manager.require(token)
        return {"status": session["status"], "error": session["error"]}

    @app.post("/api/tiktok/browser-help/{token}/done")
    def help_done(token: str, request: Request):
        if request.headers.get("origin") != config.public_url:
            raise HTTPException(403, "Origin rejected")
        help_manager.require(token)["finish"].set()
        return {"accepted": True}

    @app.delete("/api/tiktok/browser-help/{token}/")
    def help_cancel(token: str, request: Request):
        if request.headers.get("origin") != config.public_url:
            raise HTTPException(403, "Origin rejected")
        help_manager.require(token)["stop"].set()
        return {"accepted": True}

    @app.websocket("/api/tiktok/browser-help/{token}/websockify")
    async def help_socket(socket: WebSocket, token: str):
        try:
            help_manager.require(token)
            if socket.headers.get("origin") != config.public_url:
                raise ValueError("Origin rejected")
        except ValueError:
            await socket.close(code=1008)
            return
        await socket.accept(subprotocol="binary" if "binary" in socket.headers.get("sec-websocket-protocol", "") else None)
        try:
            async with websockets.connect("ws://127.0.0.1:6080/websockify", subprotocols=["binary"]) as upstream:
                async def incoming():
                    while True:
                        help_manager.require(token)
                        try:
                            packet = await asyncio.wait_for(socket.receive_bytes(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        await upstream.send(packet)
                async def outgoing():
                    while True:
                        help_manager.require(token)
                        try:
                            packet = await asyncio.wait_for(upstream.recv(), timeout=5)
                        except asyncio.TimeoutError:
                            continue
                        if isinstance(packet, bytes):
                            await socket.send_bytes(packet)
                tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except Exception:
            with contextlib.suppress(Exception):
                await socket.close()

    @app.get("/api/tiktok/browser-help/{token}/")
    def help_page(token: str):
        help_manager.require(token)
        return HTMLResponse(f'''<!doctype html><meta charset="utf-8"><title>TikTok verification</title>
<style>body{{margin:0;background:#101827;color:white;font:16px sans-serif}}header{{padding:12px}}button{{padding:10px}}#screen{{height:85vh}}</style>
<header>Complete TikTok verification in this browser. This session expires in ten minutes.
<button id="done">I have finished</button><button id="close">Close</button><span id="status"></span></header><div id="screen"></div>
<script type="module">import RFB from './assets/core/rfb.js';
const path=location.pathname;const rfb=new RFB(document.querySelector('#screen'),`${{location.protocol==='https:'?'wss':'ws'}}://${{location.host}}${{path}}websockify`);rfb.scaleViewport=true;
document.querySelector('#done').onclick=()=>fetch(path+'done',{{method:'POST'}});
document.querySelector('#close').onclick=()=>fetch(path,{{method:'DELETE'}}).then(()=>window.close());
const timer=setInterval(async()=>{{const r=await fetch(path+'status');if(!r.ok){{clearInterval(timer);rfb.disconnect();return}}const s=await r.json();document.querySelector('#status').textContent=s.error||s.status;if(['completed','failed','expired'].includes(s.status)){{clearInterval(timer);rfb.disconnect();}}}},2000);</script>''', headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

    @app.get("/api/tiktok/browser-help/{token}/assets/{asset:path}")
    def help_asset(token: str, asset: str):
        help_manager.require(token)
        root = Path("/usr/share/novnc").resolve()
        path = (root / asset).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path)

    return app


def run():
    import uvicorn
    from .logging_setup import configure_logging
    configure_logging()
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("TIKTOK_HTTP_PORT", 8080)), access_log=False)


if __name__ == "__main__":
    run()
