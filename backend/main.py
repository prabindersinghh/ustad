"""Ustad API.

Transport is a WebSocket per session so the phone (and later the glasses)
can push frames and audio and receive spoken instructions with no polling.

The client is deliberately dumb: it captures media and speaks text. All
perception, decision and state lives here, which is what lets us swap the
phone for Ray-Ban Meta glasses later without touching this file.
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, live, meta_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ustad")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init()
    log.info("Ustad up. Spark=%s SAM=%s ASR=%s",
             config.SPARK_MODEL, config.SAM_MODEL, config.ASR_MODEL)
    if not config.MODEL_API_KEY:
        log.warning("MODEL_API_KEY is empty - API calls will fail. Create .env.")
    yield


app = FastAPI(title="Ustad", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _decode_frame(payload: str) -> bytes:
    """Accept a data URL or bare base64 from the browser."""
    if payload.startswith("data:"):
        payload = payload.split(",", 1)[1]
    return base64.b64decode(payload)


# ---------------------------------------------------------------- REST

@app.get("/api/health")
def health() -> dict[str, Any]:
    backend = meta_client.active_backend()
    return {
        "ok": True,
        "key_configured": bool(config.MODEL_API_KEY),
        "reasoning_backend": backend,
        "models": {
            "reasoning": config.OPEN_MODEL if backend == "open" else config.SPARK_MODEL,
            "spark": config.SPARK_MODEL,
            "sam": config.SAM_MODEL,
            "asr": config.ASR_MODEL,
            "open": config.OPEN_MODEL if config.open_backend_ready() else None,
        },
    }


@app.get("/api/procedures")
def api_procedures() -> list[dict[str, Any]]:
    return db.list_procedures()


@app.get("/api/procedures/{pid}")
def api_procedure(pid: str) -> dict[str, Any]:
    proc = db.get_procedure(pid)
    if not proc:
        raise HTTPException(404, "No such procedure")
    return proc


@app.get("/api/sessions")
def api_sessions() -> list[dict[str, Any]]:
    return db.list_sessions()


@app.get("/api/sessions/{sid}")
def api_session(sid: str) -> dict[str, Any]:
    return db.session_log(sid)


@app.get("/api/metrics")
def api_metrics() -> dict[str, Any]:
    return db.metrics()


@app.get("/api/usage")
def api_usage() -> dict[str, Any]:
    """Metered spend at list price, by model, against BUDGET_USD."""
    return db.usage_summary()


@app.get("/dashboard")
def dashboard() -> FileResponse:
    """Supervisor view: procedures, session log with evidence photos, metrics."""
    path = WEB_DIR / "dashboard.html"
    if not path.is_file():
        raise HTTPException(404, "dashboard.html missing")
    return FileResponse(path)


@app.get("/media/{name}")
def media(name: str) -> FileResponse:
    path = (config.MEDIA_DIR / name).resolve()
    if not path.is_file() or config.MEDIA_DIR.resolve() not in path.parents:
        raise HTTPException(404, "No such file")
    return FileResponse(path)


# ------------------------------------------------------------- live sockets
# Both sockets carry JSON text frames plus binary frames of 16 kHz mono PCM
# (the phone's microphone, relayed to Muse Voice). The protocol lives in
# backend/live.py.

async def _pump(ws: WebSocket, session) -> None:
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes"):
                await session.on_audio(msg["bytes"])
            elif msg.get("text"):
                if not await session.on_text(json.loads(msg["text"])):
                    break
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("socket error")
    finally:
        await session.close()


@app.websocket("/ws/capture")
async def ws_capture(ws: WebSocket) -> None:
    """Expert records once; narration streams live; we return a procedure.

      -> {"type":"start","hint":"..."}  {"type":"frame","data":"<jpeg data url>"}
      -> binary PCM (narration)          {"type":"stop"}
      <- {"type":"heard","text":"...","final":bool}   live captions
      <- {"type":"draft","n":N,"text":"...","at":"0:07"}  a step forming
      <- {"type":"draft_title","n":N,"title":"..."}
      <- {"type":"status"|"frame_ack"|"error"|"procedure", ...}
    """
    await ws.accept()
    await _pump(ws, live.CaptureSession(ws))
    log.info("capture socket closed")


@app.websocket("/ws/guide")
async def ws_guide(ws: WebSocket) -> None:
    """Worker follows a procedure live; every step is checked before advancing.

      -> {"type":"start","procedure_id":"..."} | {"type":"resume","session_id":"..."}
      -> {"type":"watch","data":"<jpeg>"}  the live view, about once a second
      -> binary PCM (voice commands)       {"type":"check"} {"type":"repeat"|"skip"|"abort"}
      <- {"type":"track","detections":[...]}   live grounding for the AR box
      <- {"type":"heard"|"checking"|"auto"|"hazard"|"pulse", ...}
      <- {"type":"speak"|"verdict"|"session"|"complete"|"error", ...}
    """
    await ws.accept()
    await _pump(ws, live.GuideSession(ws))
    log.info("guide socket closed")


# --------------------------------------------------------------- static

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:  # pragma: no cover
    @app.get("/")
    def _no_web() -> JSONResponse:
        return JSONResponse({"error": "web/ directory missing"}, status_code=500)
