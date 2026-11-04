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
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import access, config, db, improve, live, meta_client, second_opinion

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
    live.clear_take_files()        # frames of unfinished takes from a previous run
    log.info("Ustad up. Spark=%s SAM=%s ASR=%s",
             config.SPARK_MODEL, config.SAM_MODEL, config.ASR_MODEL)
    if not config.MODEL_API_KEY:
        log.warning("MODEL_API_KEY is empty - API calls will fail. Create .env.")
    config.supervisor_token()      # create it now, so the supervisor can find it
    log.info("Supervisor token: SUPERVISOR_TOKEN in .env, else data/supervisor_token.txt")
    yield


app = FastAPI(title="Ustad", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# Open to anyone: the landing page and what it needs. Everything else is signed in.
_OPEN_API = {"/api/health", "/api/login", "/api/me", "/api/clientlog"}
_SIGNED_IN_PAGES = {"/dashboard", "/evidence", "/dashboard.html", "/evidence.html"}


@app.middleware("http")
async def _access_gate(request: Request, call_next):
    # The path the router will use. request.url is rebuilt from the Host header,
    # which the client controls, so it must never decide access.
    path = request.scope["path"]
    if access.enabled() and not access.signed_in(request.cookies.get(access.COOKIE)):
        if (path.startswith("/api/") and path not in _OPEN_API) or path.startswith("/media/"):
            return JSONResponse({"detail": "sign_in"}, status_code=401)
        if path in _SIGNED_IN_PAGES:
            query = request.scope.get("query_string", b"").decode("latin-1")
            target = path + ("?" + query if query else "")
            return RedirectResponse("/?signin=" + quote(target, safe=""), status_code=303)
    response = await call_next(request)
    if path == "/" or path in _SIGNED_IN_PAGES or path.endswith(".html"):
        response.headers["Cache-Control"] = "no-store"
    elif path.startswith(("/models/", "/assets/")):
        response.headers["Cache-Control"] = "public, max-age=604800"   # big and rarely changed
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


_login_gates: dict[str, live._RateGate] = {}
_login_failures = live._RateGate(300, 600.0)   # all clients together, against distributed guessing


@app.get("/api/me")
def api_me(request: Request) -> dict[str, bool]:
    return {"required": access.enabled(), "signed_in": access.signed_in(request.cookies.get(access.COOKIE))}


@app.post("/api/login")
def api_login(request: Request, payload: dict[str, Any]) -> JSONResponse:
    """The demo sign-in. Attempts are limited, so the code can't be guessed."""
    if not access.enabled():
        return JSONResponse({"ok": True})
    # Only wrong guesses count, per client: nobody can lock anyone else out.
    gate = live.gate_for(_login_gates, live.client_address(request), 8, 600.0)
    if gate.full() or _login_failures.full():
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    if not access.code_ok(payload.get("code")):
        gate.allow()
        _login_failures.allow()
        if _login_failures.full():
            log.warning("sign-in: many wrong codes from many clients; pausing sign-in for a few minutes")
        raise HTTPException(401, "That code isn't right.")
    response = JSONResponse({"ok": True})
    response.set_cookie(access.COOKIE, access.issue(), max_age=access.TTL_S, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https")
    return response


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


_improve_gate = live._RateGate(6, 600.0)      # each request is a paid Spark call


def _require_supervisor(token: str | None) -> None:
    """Changing a procedure needs the supervisor token, not just the link."""
    if not token or not hmac.compare_digest(token.encode(), config.supervisor_token().encode()):
        raise HTTPException(401, "Supervisor token required")


@app.post("/api/procedures/{pid}/improve")
def api_improve(pid: str, x_supervisor_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Self-improving procedures: Spark reads the runs and proposes edits."""
    _require_supervisor(x_supervisor_token)
    if not _improve_gate.allow():
        raise HTTPException(429, "Too many requests. Try again in a few minutes.")
    try:
        return improve.propose(pid)
    except KeyError:
        raise HTTPException(404, "No such procedure") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@app.post("/api/procedures/{pid}/proposals/{prid}/apply")
def api_apply(pid: str, prid: str, payload: dict[str, Any],
              x_supervisor_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Supervisor approval: apply the accepted suggestions as a new version."""
    _require_supervisor(x_supervisor_token)
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
    steps = second_opinion.steps_as_ran(record["session"], proc)
    ran = record["session"].get("procedure_version")
    return {
        **record,
        "procedure": {"id": proc.get("id"), "title": proc["title"], "machine": proc.get("machine"),
                      "version": ran or proc.get("version") or 1, "steps": steps},
        "models": {"reasoning": config.SPARK_MODEL, "perception": config.SAM_MODEL},
    }


_second_opinion_gate = live._RateGate(3, 600.0)   # each run uses a rate-limited free API


@app.get("/api/sessions/{sid}/second-opinion")
def api_second_opinion(sid: str) -> dict[str, Any]:
    """What Muse Glimmer said about this job, if asked."""
    return second_opinion.status(sid)


@app.post("/api/sessions/{sid}/second-opinion")
def api_second_opinion_start(sid: str, scope: str = "all",
                             x_supervisor_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Re-check a finished job's photos on Muse Glimmer, in the background:
    every judged step, or with `scope=doubtful` only the doubtful ones.
    Needs the token because it sends the job's photos to a third party."""
    _require_supervisor(x_supervisor_token)
    if scope not in ("all", "doubtful"):
        raise HTTPException(400, "scope must be all or doubtful")
    if not config.open_backend_ready():
        raise HTTPException(409, "No open-weights model is configured.")
    if not _second_opinion_gate.allow():
        raise HTTPException(429, "Too many requests. Try again in a few minutes.")
    try:
        return {"started": True, "scope": scope, "model": config.OPEN_MODEL,
                "steps": second_opinion.start(sid, doubtful_only=scope == "doubtful")}
    except KeyError:
        raise HTTPException(404, "No such session") from None
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(409, str(exc)) from None


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

_live_sockets = 0
_socket_gates: dict[str, live._RateGate] = {}


def _socket_refusal(ws: WebSocket) -> str | None:
    """Why this socket may not start paid work, or None."""
    if not access.signed_in(ws.cookies.get(access.COOKIE)):
        return "Please sign in to the demo first."
    stop = access.spend_stop()
    if stop:
        return stop
    if config.MAX_LIVE_SOCKETS and _live_sockets >= config.MAX_LIVE_SOCKETS:
        return "The demo is full right now. Please try again in a few minutes."
    if not config.SOCKETS_PER_CLIENT_PER_HOUR:
        return None
    gate = live.gate_for(_socket_gates, live.client_address(ws), config.SOCKETS_PER_CLIENT_PER_HOUR, 3600.0)
    if not gate.allow():
        return "Too many sessions from this device. Please try again later."
    return None


async def _serve(ws: WebSocket, make_session) -> None:
    """Accept, apply the demo's limits, run the session for at most MAX_SOCKET_S."""
    global _live_sockets
    await ws.accept()
    refusal = _socket_refusal(ws)
    if refusal:
        await ws.send_json({"type": "error", "message": refusal})
        await ws.close(code=4403)
        return
    _live_sockets += 1
    try:
        pump = _pump(ws, make_session(ws))
        if config.MAX_SOCKET_S:
            try:
                await asyncio.wait_for(pump, timeout=config.MAX_SOCKET_S)
            except asyncio.TimeoutError:
                await ws.close(code=4408)
        else:
            await pump
    finally:
        _live_sockets -= 1


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
    await _serve(ws, live.CaptureSocket)
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
    await _serve(ws, live.GuideSession)
    log.info("guide socket closed")


# --------------------------------------------------------------- static

if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
else:  # pragma: no cover
    @app.get("/")
    def _no_web() -> JSONResponse:
        return JSONResponse({"error": "web/ directory missing"}, status_code=500)
