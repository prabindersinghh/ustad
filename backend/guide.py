"""Guide mode: the perception -> decision -> action loop.

For each step the worker claims to have finished:

  1. SAM 3.1 grounds the step's target_phrase in the live frame (perception)
  2. Muse Spark sees the frame + the grounding + the success criteria, and
     must call exactly one tool (decision)
  3. The server executes that tool -- advance, correct, or ask for a better
     view -- and speaks the result back to the worker (action)

The visual evidence changes what the system does next. That is the whole
point: this is not a chatbot describing a photo.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from . import config, db, meta_client, sam_parse

log = logging.getLogger("ustad.guide")

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "verify_step",
        "description": (
            "Record that the current step was completed correctly. Call this only "
            "when the frame and the grounding together show the success criteria met."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "confidence": {
                    "type": "number",
                    "description": "0-1 confidence that the step is genuinely done.",
                },
                "reason": {
                    "type": "string",
                    "description": "One sentence on what in the image proves it.",
                },
                "say": {
                    "type": "string",
                    "description": "Short confirmation to speak to the worker.",
                },
            },
            "required": ["confidence", "reason", "say"],
        },
    },
    {
        "type": "function",
        "name": "flag_error",
        "description": (
            "The step was done wrong, or a hazard is visible. Stops the worker "
            "and tells them how to fix it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["minor", "major", "danger"]},
                "what_is_wrong": {"type": "string"},
                "how_to_fix": {"type": "string"},
                "say": {
                    "type": "string",
                    "description": "Short spoken correction for the worker.",
                },
            },
            "required": ["severity", "what_is_wrong", "how_to_fix", "say"],
        },
    },
    {
        "type": "function",
        "name": "request_better_view",
        "description": (
            "The frame does not show enough to judge the step. Ask the worker to "
            "reposition. Use this instead of guessing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "what_to_show": {"type": "string"},
                "say": {"type": "string"},
            },
            "required": ["what_to_show", "say"],
        },
    },
]

SYSTEM = """\
You are Ustad, verifying one step of a factory procedure from a worker's
point-of-view camera.

You will be given the step, its visual success criteria, and a grounding
report from a segmentation model that searched the frame for the step's
target object.

You must call exactly one tool. Never reply with prose.

Judge strictly:
  * If the grounding found no match and the frame does not clearly show the
    success criteria anyway, call request_better_view -- do not guess.
  * If you can see the step was done, but done wrong, call flag_error.
  * Only call verify_step when the image genuinely shows the criteria met.
  * Anything unsafe in the frame is flag_error with severity "danger",
    even if the step itself was done correctly.
Keep every "say" field under 20 words. It is spoken aloud to someone whose
hands are busy.
"""


def judge_step(
    step: dict[str, Any],
    frame: bytes,
    *,
    worker_said: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Perception + decision for one step, with NO persistence.

    This is the pure core of the verification loop: SAM grounds the target
    phrase, Muse Spark decides with exactly one tool call, and we map that to a
    verdict. Returns the action plus verdict, latency, grounding, detections and
    backend. `verify_step` wraps this with evidence saving and DB logging; the
    eval harness calls it directly so its numbers come from the same code path
    the demo runs.
    """
    started = time.perf_counter()

    # --- perception -------------------------------------------------------
    phrase = step.get("target_phrase") or step.get("title") or ""
    detections: list[sam_parse.Detection] = []
    if phrase:
        try:
            detections = meta_client.segment(phrase, frame)
        except Exception as exc:  # noqa: BLE001 - never let grounding kill the loop
            log.warning("Grounding failed for %r: %s", phrase, exc)
    grounding = sam_parse.summarise(phrase, detections) if phrase else "Grounding: skipped."

    # --- decision ---------------------------------------------------------
    prompt = [
        {
            "type": "input_text",
            "text": (
                f"Step {step.get('index', 0) + 1}: {step.get('title', '')}\n"
                f"Instruction given to the worker: {step.get('instruction', '')}\n"
                f"Success criteria: {step.get('success_criteria') or '(not specified)'}\n"
                f"Known common mistake: {step.get('common_mistake') or '(none recorded)'}\n"
                f"Attempt number: {attempt}\n"
                + (f"The worker said: \"{worker_said}\"\n" if worker_said else "")
                + f"\n{grounding}\n\nThe image is the worker's current view."
            ),
        },
        meta_client.as_image_part(frame),
    ]

    backend = meta_client.active_backend()
    resp = meta_client.reason(SYSTEM, prompt, tools=TOOLS)
    backend = getattr(resp, "backend", backend)  # may have fallen back
    calls = meta_client.tool_calls(resp)
    latency_ms = int((time.perf_counter() - started) * 1000)

    if not calls:
        # Model answered in prose despite instructions - treat as "need a better view".
        fallback = (getattr(resp, "output_text", "") or "").strip()
        action = {
            "tool": "request_better_view",
            "say": "I could not see that clearly. Hold the view steady on the part.",
            "detail": {"what_to_show": fallback[:200] or "the target part"},
        }
    else:
        call = calls[0]
        action = {
            "tool": call["name"],
            "say": call["args"].get("say", ""),
            "detail": call["args"],
        }

    verdict = {
        "verify_step": "pass",
        "flag_error": "fail",
        "request_better_view": "retry",
    }.get(action["tool"], "retry")

    action.update(
        verdict=verdict,
        latency_ms=latency_ms,
        grounding=grounding,
        detections=[d.to_dict() for d in detections],
        backend=backend,
    )
    return action


def verify_step(
    *,
    session_id: str,
    step: dict[str, Any],
    frame: bytes,
    worker_said: str | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    """Run one perception -> decision -> action cycle. Returns the action taken."""
    action = judge_step(step, frame, worker_said=worker_said, attempt=attempt)
    verdict = action["verdict"]
    latency_ms = action["latency_ms"]
    grounding = action["grounding"]
    backend = action["backend"]

    # --- action -----------------------------------------------------------
    evidence_path = None
    if verdict in ("pass", "fail"):
        evidence_path = _save_evidence(session_id, step.get("index", 0), attempt, frame)

    db.log_step_event(
        session_id,
        step.get("index", 0),
        verdict,
        confidence=action["detail"].get("confidence"),
        reason=(
            action["detail"].get("reason")
            or action["detail"].get("what_is_wrong")
            or action["detail"].get("what_to_show")
        ),
        grounding=grounding,
        evidence_path=evidence_path,
        attempt=attempt,
        latency_ms=latency_ms,
        backend=backend,
    )

    # judge_step already set verdict, latency_ms, grounding, detections, backend.
    action.update(
        advance=(verdict == "pass"),
        evidence_path=evidence_path,
    )
    log.info("step %s attempt %s -> %s in %dms on %s backend",
             step.get("index"), attempt, verdict, latency_ms, backend)
    return action


def _save_evidence(session_id: str, step_index: int, attempt: int, frame: bytes) -> str:
    """Every judged step leaves a photo behind. This is the audit trail."""
    name = f"{session_id}_s{step_index:02d}_a{attempt}.jpg"
    path = config.MEDIA_DIR / name
    path.write_bytes(frame)
    return f"media/{name}"
