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

from . import config, db, improve, live, meta_client

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


@app.get("/api/procedures/{pid}/insights")
def api_insights(pid: str) -> dict[str, Any]:
    """Per-step struggle data for a procedure: first-try rate, catches, time."""
    if not db.get_procedure(pid):
        raise HTTPException(404, "No such procedure")
    return db.step_insights(pid)


@app.get("/api/procedures/{pid}/improve")
def api_improve_latest(pid: str) -> dict[str, Any]:
    """The latest suggestions for this procedure and its version history."""
    if not db.get_procedure(pid):
        raise HTTPException(404, "No such procedure")
    return {"proposal": db.latest_proposal(pid), "history": db.procedure_history(pid)}


@app.post("/api/procedures/{pid}/improve")
def api_improve(pid: str) -> dict[str, Any]:
    """Self-improving procedures: Spark reads the runs and proposes edits."""
    try:
        return improve.propose(pid)
    except KeyError:
        raise HTTPException(404, "No such procedure") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/procedures/{pid}/proposals/{prid}/apply")
def api_apply(pid: str, prid: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Supervisor approval: apply the accepted suggestions as a new version."""
    accept = payload.get("accept") or []
    if not isinstance(accept, list) or not all(isinstance(i, int) for i in accept):
        raise HTTPException(422, "accept must be a list of suggestion numbers")
    try:
        return improve.apply(pid, prid, accept)
    except KeyError:
        raise HTTPException(404, "No such suggestions") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.get("/api/sessions/{sid}/pack")
def api_pack(sid: str) -> dict[str, Any]:
    """Everything the evidence pack shows for one job: the session, the
    procedure as it was when the job ran, and every event with its photo."""
    record = db.session_log(sid)
    if not record["session"]:
        raise HTTPException(404, "No such session")
    proc = db.get_procedure(record["session"]["procedure_id"]) or {"title": "(deleted procedure)", "steps": []}
    steps = proc.get("steps", [])
    ran = record["session"].get("procedure_version")
    if ran and proc.get("version") and int(ran) != int(proc["version"]):
        hist = db.procedure_history_steps(proc["id"], int(ran))
        if hist:
            steps = hist[0]
    return {
        **record,
        "procedure": {"id": proc.get("id"), "title": proc["title"], "machine": proc.get("machine"),
                      "version": ran or proc.get("version") or 1, "steps": steps},
        "models": {"reasoning": config.SPARK_MODEL, "perception": config.SAM_MODEL},
    }


_clientlog_gate = live._RateGate(30, 60.0)   # unauthenticated: bound it


@app.post("/api/clientlog")
async def api_clientlog(payload: dict[str, Any]) -> dict[str, bool]:
    """Script errors reported by phones (Diag.report in web/app.js).

    Anyone can post here, so every field is flattened to one escaped line and
    the endpoint is rate-limited: it must not be a way to forge or flood logs.
    """
    if not _clientlog_gate.allow():
        return {"ok": False}
    log.warning("client %s: %s | %s", live.clean_for_log(payload.get("kind"), 20),
                live.clean_for_log(payload.get("message"), 400), live.clean_for_log(payload.get("ua"), 140))
    return {"ok": True}


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


@app.get("/evidence")
def evidence() -> FileResponse:
    """Printable evidence pack for one job: /evidence?s=<session id>."""
    path = WEB_DIR / "evidence.html"
    if not path.is_file():
        raise HTTPException(404, "evidence.html missing")
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

      -> {"type":"start","hint":"..."}  {"type":"frame","data":"<jpeg data url>","at"?:s}
      -> binary PCM (narration)          {"type":"stop"}
      -> {"type":"rejoin","capture_id":"..."} after a dropped connection, then
         buffered {"type":"frame",...,"at":s} and {"type":"audio_gap","at":s,"data":"<b64 pcm>"}
      <- {"type":"capture","capture_id":"..."}  {"type":"rejoined"|"rejoin_failed", ...}
      <- {"type":"heard","text":"...","final":bool}   live captions
      <- {"type":"draft","n":N,"text":"...","at":"0:07"}  a step forming
      <- {"type":"draft_title","n":N,"title":"..."}
      <- {"type":"status"|"frame_ack"|"error"|"procedure"|"pong", ...}
    """
    await ws.accept()
    await _pump(ws, live.CaptureSocket(ws))
    log.info("capture socket closed")


@app.websocket("/ws/guide")
async def ws_guide(ws: WebSocket) -> None:
    """Worker follows a procedure live; every step is checked before advancing.

      -> {"type":"start","procedure_id":"..."} | {"type":"resume","session_id":"..."}
      -> {"type":"watch","data":"<jpeg>"}  the live view, about once a second
      -> binary PCM (voice commands)       {"type":"check"} {"type":"repeat"|"skip"|"abort"}
      -> {"type":"listen"} (Ask button)    {"type":"ask","text":"..."}  {"type":"show"}  {"type":"ping"}
      <- {"type":"track","detections":[...]}   live grounding for the AR box
      <- {"type":"heard"|"checking"|"auto"|"hazard"|"pulse", ...}
      <- {"type":"thinking"|"answer"|"point"|"reference", ...}   Ask Ustad
      <- {"type":"speak"|"verdict"|"session"|"complete"|"error"|"pong", ...}
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
