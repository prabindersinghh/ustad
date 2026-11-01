"""Ustad API.

Transport is a WebSocket per session so the phone (and later the glasses)
can push frames and audio and receive spoken instructions with no polling.

The client is deliberately dumb: it captures media and speaks text. All
perception, decision and state lives here, which is what lets us swap the
phone for Ray-Ban Meta glasses later without touching this file.
"""
from __future__ import annotations

import asyncio
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

from . import config, db, guide, meta_client, procedures

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


# ---------------------------------------------------------------- capture

@app.websocket("/ws/capture")
async def ws_capture(ws: WebSocket) -> None:
    """Expert records once; we return a reusable procedure.

    Protocol (JSON text frames):
      -> {"type":"start","hint":"optional context"}
      -> {"type":"frame","data":"<data url>"}            (repeatedly)
      -> {"type":"audio","data":"<base64>","mime":"audio/mp4"}
      -> {"type":"stop"}
      <- {"type":"status","message":"..."}
      <- {"type":"procedure","procedure_id":"...","procedure":{...}}
    """
    await ws.accept()
    keyframes: list[bytes] = []
    audio_blobs: list[bytes] = []
    audio_mime = "audio/mp4"
    hint: str | None = None

    async def status(message: str) -> None:
        await ws.send_json({"type": "status", "message": message})

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")

            if kind == "start":
                hint = msg.get("hint") or None
                keyframes.clear()
                audio_blobs.clear()
                await status("Recording. Narrate each step as you do it.")

            elif kind == "frame":
                keyframes.append(_decode_frame(msg["data"]))
                await ws.send_json({"type": "frame_ack", "count": len(keyframes)})

            elif kind == "audio":
                audio_blobs.append(base64.b64decode(msg["data"]))
                audio_mime = msg.get("mime", audio_mime)

            elif kind == "stop":
                audio_size = sum(len(b) for b in audio_blobs)
                log.info("capture stop: %d keyframes, %d bytes of %s audio",
                         len(keyframes), audio_size, audio_mime)
                if not keyframes:
                    await ws.send_json({
                        "type": "error",
                        "message": "No camera frames reached the server. Keep recording "
                                   "for a few seconds with the camera on the work, then stop.",
                    })
                    continue

                await status(f"Captured {len(keyframes)} keyframes. Transcribing…")
                transcript = ""
                if audio_blobs:
                    ext = "m4a" if "mp4" in audio_mime or "m4a" in audio_mime else "webm"
                    try:
                        # Model calls block for seconds; run them off the event
                        # loop so the socket keeps answering pings and stays up.
                        transcript = await asyncio.to_thread(
                            meta_client.transcribe, b"".join(audio_blobs), filename=f"capture.{ext}"
                        )
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Transcription failed")
                        await status("Couldn't transcribe the narration; writing from the video alone.")

                await status("Writing the procedure…")
                try:
                    proc = await asyncio.to_thread(
                        procedures.build_procedure, transcript, keyframes, hint=hint
                    )
                except Exception as exc:  # noqa: BLE001
                    log.exception("Procedure generation failed")
                    await ws.send_json({"type": "error", "message": str(exc)})
                    continue

                pid = db.save_procedure(
                    proc["title"],
                    proc["steps"],
                    machine=proc.get("machine"),
                    transcript=transcript,
                )
                await ws.send_json(
                    {"type": "procedure", "procedure_id": pid, "procedure": proc}
                )

            elif kind == "close":
                break

    except WebSocketDisconnect:
        log.info("capture socket closed")
    except Exception:  # noqa: BLE001
        log.exception("capture socket error")


# ------------------------------------------------------------------ guide

@app.websocket("/ws/guide")
async def ws_guide(ws: WebSocket) -> None:
    """Worker follows a procedure; every step is checked before advancing.

    Protocol (JSON text frames):
      -> {"type":"start","procedure_id":"...","worker":"name"}
      -> {"type":"check","data":"<data url>","said":"done"}
      -> {"type":"repeat"}
      -> {"type":"skip"}            (supervisor override, logged)
      <- {"type":"speak","text":"...","step":N,"of":M}
      <- {"type":"verdict", ...}
      <- {"type":"complete","session_id":"..."}
    """
    await ws.accept()
    proc: dict[str, Any] | None = None
    session_id: str | None = None
    idx = 0
    attempt = 1

    async def speak(text: str, **extra: Any) -> None:
        await ws.send_json({"type": "speak", "text": text, **extra})

    async def announce_step() -> None:
        assert proc is not None
        step = proc["steps"][idx]
        prefix = f"Warning. {step['hazard']} " if step.get("hazard") else ""
        await speak(
            prefix + step["instruction"],
            step=idx + 1,
            of=len(proc["steps"]),
            title=step.get("title", ""),
            target=step.get("target_phrase", ""),
        )

    try:
        while True:
            msg = json.loads(await ws.receive_text())
            kind = msg.get("type")

            if kind == "start":
                proc = db.get_procedure(msg["procedure_id"])
                if not proc:
                    await ws.send_json({"type": "error", "message": "Unknown procedure"})
                    continue
                session_id = db.start_session(proc["id"], msg.get("worker"), "guide")
                idx, attempt = 0, 1
                await ws.send_json(
                    {
                        "type": "session",
                        "session_id": session_id,
                        "title": proc["title"],
                        "steps": len(proc["steps"]),
                    }
                )
                await announce_step()

            elif kind == "resume":
                # Reconnect after a dropped socket. The server, not the client,
                # decides where the worker is: resume just past the last step
                # that passed, so a lost connection never restarts the job.
                sid = msg.get("session_id")
                record = db.session_log(sid) if sid else {"session": None}
                if not record["session"]:
                    await ws.send_json({"type": "error", "message": "Unknown session"})
                    continue
                proc = db.get_procedure(record["session"]["procedure_id"])
                if not proc:
                    await ws.send_json({"type": "error", "message": "Unknown procedure"})
                    continue
                session_id = sid
                passed = [e["step_index"] for e in record["events"] if e["verdict"] == "pass"]
                idx = (max(passed) + 1) if passed else 0
                attempt = 1
                if idx >= len(proc["steps"]):
                    await ws.send_json({"type": "complete", "session_id": session_id})
                    continue
                await ws.send_json(
                    {
                        "type": "session",
                        "session_id": session_id,
                        "title": proc["title"],
                        "steps": len(proc["steps"]),
                        "resumed_at": idx + 1,
                    }
                )
                await announce_step()

            elif kind == "check":
                if proc is None or session_id is None:
                    await ws.send_json({"type": "error", "message": "Session not started"})
                    continue

                frame = _decode_frame(msg["data"])
                try:
                    action = await asyncio.to_thread(
                        guide.verify_step,
                        session_id=session_id,
                        step=proc["steps"][idx],
                        frame=frame,
                        worker_said=msg.get("said"),
                        attempt=attempt,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.exception("verification failed")
                    await ws.send_json({"type": "error", "message": str(exc)})
                    continue

                await ws.send_json({"type": "verdict", **action, "step": idx + 1})
                if action["say"]:
                    await speak(action["say"], step=idx + 1, of=len(proc["steps"]))

                if action["advance"]:
                    idx += 1
                    attempt = 1
                    if idx >= len(proc["steps"]):
                        db.finish_session(session_id, "completed")
                        await ws.send_json(
                            {"type": "complete", "session_id": session_id}
                        )
                        await speak("Procedure complete. Well done.")
                    else:
                        await announce_step()
                else:
                    attempt += 1

            elif kind == "repeat":
                if proc is not None:
                    await announce_step()

            elif kind == "skip":
                if proc is not None and session_id is not None:
                    db.log_step_event(
                        session_id, idx, "skipped",
                        reason="Manually skipped by operator", attempt=attempt,
                    )
                    idx += 1
                    attempt = 1
                    if idx >= len(proc["steps"]):
                        db.finish_session(session_id, "completed_with_skips")
                        await ws.send_json({"type": "complete", "session_id": session_id})
                    else:
                        await announce_step()

            elif kind == "abort":
                if session_id:
                    db.finish_session(session_id, "aborted")
                break

    except WebSocketDisconnect:
        log.info("guide socket closed")
    except Exception:  # noqa: BLE001
        log.exception("guide socket error")


# --------------------------------------------------------------- static

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:  # pragma: no cover
    @app.get("/")
    def _no_web() -> JSONResponse:
        return JSONResponse({"error": "web/ directory missing"}, status_code=500)
