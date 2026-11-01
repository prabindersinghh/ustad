"""Capture mode: turn one expert recording into a reusable, checkable procedure.

This is the leverage in Ustad. An expert does the job once, narrating as they
go. We sample keyframes from the video, transcribe the narration, and ask
Muse Spark to produce a step list where every step carries:

  * a spoken instruction (what the worker hears)
  * a target_phrase (a short noun phrase SAM can ground in the frame)
  * success_criteria (what "done correctly" looks like, in visual terms)

The target_phrase is what makes the procedure *checkable* rather than just
readable -- it is the hook the guide loop uses to verify work with real pixels.

After generation we run a grounding self-check: SAM tries to find each step's
target_phrase in the keyframe the model said best shows it. A phrase SAM can't
ground is a phrase the guide loop can never verify, so we feed those failures
back to the model and regenerate just those steps once. This closes the loop at
authoring time instead of discovering a useless target_phrase live on the floor.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from . import config, meta_client

log = logging.getLogger("ustad.capture")

SYSTEM = """\
You convert a recording of an expert technician into a precise, checkable work
procedure for a new worker who has never done this task.

You are given:
  * the expert's narration, transcribed
  * keyframes sampled from the expert's point of view, in order (0-indexed)

Produce a JSON object:
{
  "title": "<short task name>",
  "machine": "<equipment name, or null>",
  "steps": [
    {
      "title": "<3-6 word step name>",
      "instruction": "<one sentence, spoken aloud to the worker, imperative>",
      "target_phrase": "<short noun phrase naming the ONE object the worker
                        must act on, e.g. 'red valve handle'. No verbs, no
                        articles, no sentences. This is fed to a segmentation
                        model, so it must name something plainly visible.>",
      "success_criteria": "<what the CAMERA SHOULD SEE when this step is done
                            correctly. Describe visible state only -- position,
                            colour, alignment, what is attached or removed. Never
                            abstract ('properly tightened') -- always observable
                            ('cap seated flush, no gap visible').>",
      "keyframe_index": <integer: the keyframe that best shows this step's
                        target object, used to verify the target_phrase>,
      "common_mistake": "<the most likely error, or null>",
      "hazard": "<safety warning to speak before the step, or null>"
    }
  ]
}

Rules:
  * Between 4 and 12 steps. Merge trivial actions; never invent steps the
    narration and frames do not support.
  * instruction must be speakable in under 8 seconds.
  * target_phrase must name a CONCRETE object visible in the frames -- a part,
    tool, fastener, gauge or control. Not an action, not a region, not a state.
    Prefer a distinguishing adjective ('left pressure gauge', not 'gauge').
  * success_criteria must be something a camera could confirm by looking.
  * keyframe_index must be a valid index into the keyframes you were given.
  * If the expert mentions a torque, pressure, count or setting, put the exact
    value in the instruction.
  * Write instruction, success_criteria and hazard in the same language as the
    narration.
"""

# A regeneration pass is appended to SYSTEM when some target phrases failed to
# ground, naming the offenders so the model can make them concrete.
REPAIR_SUFFIX = """\

REVISION REQUIRED. A segmentation model could NOT locate these target_phrase
values in the keyframes you chose, which means the guide loop can never verify
them:

{failures}

Regenerate the FULL procedure with the SAME steps and meaning, but rewrite each
failed target_phrase to name a more concrete, visually distinct object that is
actually present in the frames (and fix its keyframe_index if it was wrong).
Keep the phrases that were not listed as problems.
"""


def build_procedure(
    transcript: str,
    keyframes: list[bytes],
    *,
    hint: str | None = None,
    validate: bool | None = None,
) -> dict[str, Any]:
    """Expert recording -> procedure dict. Does not persist.

    When `validate` is true (default from config.VALIDATE_PROCEDURE) each step's
    target_phrase is grounded against its keyframe with SAM, and any phrase that
    cannot be found is regenerated once. A `_validation` block is attached to the
    returned dict so the UI and eval harness can see what the self-check did.
    """
    if validate is None:
        validate = config.VALIDATE_PROCEDURE

    frames = keyframes[: config.MAX_KEYFRAMES_FOR_PROCEDURE]
    log.info("Building procedure from %d keyframes, %d chars of narration",
             len(frames), len(transcript))

    proc = _generate(transcript, frames, hint=hint)

    if validate and frames:
        try:
            proc = _validate_and_repair(proc, frames, transcript, hint=hint)
        except Exception:  # noqa: BLE001 - validation is best-effort, never fatal
            log.exception("Procedure self-check failed; returning unvalidated procedure")
    return proc


def _generate(
    transcript: str,
    frames: list[bytes],
    *,
    hint: str | None,
    extra_system: str = "",
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                (f"Context from the supervisor: {hint}\n\n" if hint else "")
                + f"Expert narration (transcribed):\n{transcript or '(no speech captured)'}"
                + f"\n\nThe {len(frames)} images that follow are keyframes in "
                + "chronological order, indexed from 0."
            ),
        }
    ]
    content.extend(meta_client.as_image_part(f) for f in frames)

    data = meta_client.reason_json(SYSTEM + extra_system, content)
    return _normalise(data, n_frames=len(frames))


# --------------------------------------------------------------------------
# Grounding self-check
# --------------------------------------------------------------------------

def _validate_and_repair(
    proc: dict[str, Any],
    frames: list[bytes],
    transcript: str,
    *,
    hint: str | None,
    segment_fn: Callable[[str, bytes], list[Any]] | None = None,
    generate_fn: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ground every target_phrase; regenerate the failures once.

    `segment_fn` and `generate_fn` are injectable so this is unit-testable
    without touching the network.
    """
    segment_fn = segment_fn or meta_client.segment
    generate_fn = generate_fn or (
        lambda: _generate(transcript, frames, hint=hint,
                          extra_system=REPAIR_SUFFIX.format(failures=failures_block))
    )

    failures = _check_grounding(proc["steps"], frames, segment_fn)
    report: dict[str, Any] = {
        "ran": True,
        "steps_checked": len(proc["steps"]),
        "failed_before": [f["target_phrase"] for f in failures],
        "repaired": False,
    }

    if failures:
        failures_block = "\n".join(
            f"  - step {f['index'] + 1} ({f['title']!r}): "
            f"target_phrase {f['target_phrase']!r} not found"
            for f in failures
        )
        log.info("%d target phrase(s) failed grounding; regenerating", len(failures))
        try:
            repaired = generate_fn()
            still = _check_grounding(repaired["steps"], frames, segment_fn)
            report["repaired"] = True
            report["failed_after"] = [f["target_phrase"] for f in still]
            # Only accept the repair if it did not make things worse.
            if len(still) <= len(failures):
                proc = repaired
        except Exception:  # noqa: BLE001
            log.exception("Repair regeneration failed; keeping original procedure")
            report["failed_after"] = report["failed_before"]
    else:
        report["failed_after"] = []

    proc["_validation"] = report
    return proc


def _check_grounding(
    steps: list[dict[str, Any]],
    frames: list[bytes],
    segment_fn: Callable[[str, bytes], list[Any]],
) -> list[dict[str, Any]]:
    """Return the steps whose target_phrase SAM cannot find in its keyframe."""
    failures: list[dict[str, Any]] = []
    for step in steps:
        phrase = (step.get("target_phrase") or "").strip()
        if not phrase:
            failures.append(step)
            continue
        ki = step.get("keyframe_index")
        if not isinstance(ki, int) or not (0 <= ki < len(frames)):
            ki = min(step.get("index", 0), len(frames) - 1)
        try:
            dets = segment_fn(phrase, frames[ki])
        except Exception as exc:  # noqa: BLE001 - a SAM error is not a bad phrase
            log.warning("Grounding check errored for %r: %s", phrase, exc)
            continue
        if not dets:
            failures.append(step)
    return failures


# --------------------------------------------------------------------------

def _normalise(data: Any, *, n_frames: int = 0) -> dict[str, Any]:
    """Defend against shape drift from the model."""
    if isinstance(data, list):
        data = {"title": "Untitled procedure", "steps": data}
    if not isinstance(data, dict):
        raise ValueError("Procedure generator returned an unexpected shape")

    steps_in = data.get("steps") or []
    steps: list[dict[str, Any]] = []
    for i, raw in enumerate(steps_in):
        if not isinstance(raw, dict):
            continue
        instruction = (raw.get("instruction") or raw.get("text") or "").strip()
        if not instruction:
            continue
        ki = raw.get("keyframe_index")
        if not isinstance(ki, int) or (n_frames and not (0 <= ki < n_frames)):
            ki = min(i, n_frames - 1) if n_frames else 0
        steps.append(
            {
                "index": i,
                "title": (raw.get("title") or f"Step {i + 1}").strip(),
                "instruction": instruction,
                "target_phrase": (raw.get("target_phrase") or "").strip(),
                "success_criteria": (raw.get("success_criteria") or "").strip(),
                "keyframe_index": ki,
                "common_mistake": raw.get("common_mistake") or None,
                "hazard": raw.get("hazard") or None,
            }
        )

    if not steps:
        raise ValueError("Procedure generator produced no usable steps")

    return {
        "title": (data.get("title") or "Untitled procedure").strip(),
        "machine": data.get("machine") or None,
        "steps": steps,
    }
