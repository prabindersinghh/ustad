"""A second opinion on a finished job, from Muse Glimmer, Meta's open-weight model.

Each step's evidence photo goes back through the same judge, on Glimmer instead
of Muse Spark, and the pack shows where the two agree. It runs after the job, on
a supervisor's request, in the background: a check took about 63 s on NVIDIA's
free API (Muse Spark: 4.6 s), so it can't guide live and must never slow a job.

It reports only what Glimmer said. A failed call is recorded as an error, never
answered by another model, because the record names the model that judged.

A supervisor can also send only the doubtful steps (the edge-case review): one
photo per doubtful step instead of one per judged step, so a slow model's time
goes where a second look is worth most.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from . import config, db, guide, sam_parse

log = logging.getLogger("ustad.second_opinion")

# A verdict below this confidence counts as doubtful. A starting value, not a tuned one.
DOUBT_CONFIDENCE = 0.8
_JUDGED = ("pass", "fail", "retry")     # skipped and operator-confirmed steps had no model verdict
_BOX_FIELDS = ("frame", "ordinal", "x1", "y1", "x2", "y2", "img_w", "img_h", "confidence")
_state_lock = threading.Lock()
_running: str | None = None            # one at a time: the free API is rate-limited
_thread: threading.Thread | None = None


def steps_as_ran(session: dict[str, Any], proc: dict[str, Any]) -> list[dict[str, Any]]:
    """The procedure's steps as they were when this job ran."""
    ran = session.get("procedure_version")
    if ran and proc.get("version") and int(ran) != int(proc["version"]):
        hist = db.procedure_history_steps(proc["id"], int(ran))
        if hist:
            return hist[0]
    return proc.get("steps", [])


def doubtful_steps(events: list[dict[str, Any]]) -> set[int]:
    """Steps worth a second look: a fail or retry at any point, more than one
    attempt, or a verdict the model itself wasn't sure of."""
    out: set[int] = set()
    for e in events:
        if e.get("verdict") not in _JUDGED:
            continue
        conf = e.get("confidence")
        if (e["verdict"] != "pass" or (e.get("attempt") or 1) > 1
                or (isinstance(conf, (int, float)) and conf < DOUBT_CONFIDENCE)):
            out.add(e["step_index"])
    return out


def _to_check(events: list[dict[str, Any]], *, doubtful_only: bool = False) -> list[dict[str, Any]]:
    """The last judged photo of each step: what the job's record rests on."""
    latest: dict[int, dict[str, Any]] = {}
    for e in events:
        if e.get("evidence_path") and e.get("verdict") in ("pass", "fail"):
            latest[e["step_index"]] = e
    keep = doubtful_steps(events) if doubtful_only else set(latest)
    return [latest[i] for i in sorted(latest) if i in keep]


def _detections(event: dict[str, Any]) -> list[sam_parse.Detection]:
    out = []
    for d in event.get("detections") or []:
        try:
            out.append(sam_parse.Detection(**{k: d[k] for k in _BOX_FIELDS}))
        except (KeyError, TypeError):
            continue
    return out


def check_one(step: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """One photo through the judge on Glimmer. Never raises."""
    row: dict[str, Any] = {
        "event_id": event["id"], "step_index": event["step_index"], "model": config.OPEN_MODEL,
        "first_verdict": event["verdict"], "verdict": None, "agrees": None, "confidence": None,
        "reason": None, "latency_ms": None, "error": None,
    }
    try:
        frame = (config.MEDIA_DIR / Path(str(event["evidence_path"])).name).read_bytes()
        action = guide.judge_step(step, frame, detections=_detections(event), open_weights=True)
        detail = action.get("detail") or {}
        row.update(
            verdict=action["verdict"],
            # "Not done yet" is one answer however it's put: an error and a
            # request for a better view both say the step isn't proven.
            agrees=(action["verdict"] == "pass") == (event["verdict"] == "pass"),
            confidence=detail.get("confidence"),
            reason=detail.get("reason") or detail.get("what_is_wrong") or detail.get("what_to_show"),
            latency_ms=action.get("latency_ms"),
        )
    except Exception as exc:  # noqa: BLE001 - one failed call must not lose the others
        log.warning("second opinion failed on step %s: %s", event.get("step_index"), type(exc).__name__)
        row["error"] = type(exc).__name__
    return row


def run(session_id: str, *, doubtful_only: bool = False) -> None:
    """Check a job's judged steps, or only the doubtful ones, saving each answer as it arrives."""
    global _running
    try:
        record = db.session_log(session_id)
        proc = db.get_procedure(record["session"]["procedure_id"]) if record["session"] else None
        if not proc:
            return
        steps = steps_as_ran(record["session"], proc)
        for event in _to_check(record["events"], doubtful_only=doubtful_only):
            if event["step_index"] < len(steps):
                row = check_one(steps[event["step_index"]], event)
                db.save_second_opinion(session_id, row)
                log.info("second opinion: session %s step %d on %s -> %s in %s ms", session_id,
                         event["step_index"] + 1, row["model"], row["verdict"] or "error", row["latency_ms"])
    finally:
        with _state_lock:
            _running = None


def start(session_id: str, *, doubtful_only: bool = False) -> int:
    """Start in the background. Returns how many photos will be checked."""
    global _running, _thread
    record = db.session_log(session_id)
    if not record["session"]:
        raise KeyError(session_id)
    count = len(_to_check(record["events"], doubtful_only=doubtful_only))
    if not count:
        raise ValueError("This job has no doubtful steps with a photo to check." if doubtful_only
                         else "This job has no judged photos to check.")
    with _state_lock:
        if _running:
            raise RuntimeError("Another second opinion is running. Try again in a few minutes.")
        _running = session_id
    _thread = threading.Thread(target=run, args=(session_id,), kwargs={"doubtful_only": doubtful_only},
                               daemon=True, name="second-opinion")
    _thread.start()
    return count


def status(session_id: str) -> dict[str, Any]:
    events = db.session_log(session_id)["events"]
    checkable = {e["step_index"] for e in _to_check(events, doubtful_only=True)}
    return {
        "doubtful": len(checkable),
        # A step that only ever got "can't tell" saved no photo, so it can't be re-checked.
        "doubtful_no_photo": len(doubtful_steps(events) - checkable),
        "doubt_confidence": DOUBT_CONFIDENCE,
        "available": config.open_backend_ready(),
        "running": _running == session_id,
        "model": config.OPEN_MODEL if config.open_backend_ready() else None,
        "rows": db.second_opinions(session_id),
    }
