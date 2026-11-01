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
      "check": "<how the step is verified: 'visual' (default: the camera can
                see it done), 'reading' (a gauge, display or scale must show
                a value), 'timed' (something must run or wait for a time), or
                'confirm' (the result cannot be seen in a photo: a click, a
                sound, a smell)>",
      "reading": {"what": "<e.g. discharge pressure>", "unit": "<e.g. bar>",
                  "min": <number or null>, "max": <number or null>} or null,
      "duration_s": <seconds to run or wait, for a 'timed' step, else null>,
      "watch_for": [ {"phrase": "<short noun phrase of another VISIBLE object
                     that matters during this step>", "role": "hazard" | "tool"} ]
                   (0 to 2 items. 'hazard' = a part the hands must keep clear of
                   during this step: a blade, a rotating shaft, a belt, a hot
                   surface, a live terminal. 'tool' = the tool the step needs:
                   a torque wrench, a spanner, a scraper. Only objects you can
                   see in the keyframes.),
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
  * Industrial machines: when the expert names an acceptable value or range
    for a gauge, display, scale or meter ("pressure should sit between 6 and
    8 bar", "weigh out 25 kilos"), make it a 'reading' step with that range;
    use the gauge or display itself as the target_phrase. When they say to
    run, mix, heat or wait for a time, make it 'timed' with duration_s. Use
    'confirm' only when a camera truly cannot verify the result.
  * Keep lockout, isolation and other safety steps as their own steps, with
    the hazard filled in; never merge them into a neighbouring step.
  * If the narration contains "[step marker]", the expert marked the start of
    a new step there: begin a new step at that point.
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
    frame_times: list[float] | None = None,
) -> dict[str, Any]:
    """Expert recording -> procedure dict. Does not persist.

    When `validate` is true (default from config.VALIDATE_PROCEDURE) each step's
    target_phrase is grounded against its keyframe with SAM, and any phrase that
    cannot be found is regenerated once. A `_validation` block is attached to the
    returned dict so the UI and eval harness can see what the self-check did.

    `frame_times` (seconds from the start, one per keyframe) lets the writer line
    timestamped narration up with what the camera showed at that moment.
    """
    if validate is None:
        validate = config.VALIDATE_PROCEDURE

    keep = select_keyframes(len(keyframes), config.MAX_KEYFRAMES_FOR_PROCEDURE)
    frames = [keyframes[i] for i in keep]
    times = [frame_times[i] for i in keep] if frame_times and len(frame_times) == len(keyframes) else None
    log.info("Building procedure from %d of %d keyframes, %d chars of narration",
             len(frames), len(keyframes), len(transcript))

    proc = _generate(transcript, frames, hint=hint, times=times)

    if validate and frames:
        try:
            proc = _validate_and_repair(proc, frames, transcript, hint=hint, times=times)
        except Exception:  # noqa: BLE001 - validation is best-effort, never fatal
            log.exception("Procedure self-check failed; returning unvalidated procedure")
    return proc


def select_keyframes(n: int, limit: int) -> list[int]:
    """Evenly spaced indices across the whole recording, always first and last.

    Taking the first `limit` frames silently dropped the end of every long job,
    which is usually the part that matters most.
    """
    if n <= limit:
        return list(range(n))
    if limit <= 1:
        return [n - 1]
    return sorted({round(i * (n - 1) / (limit - 1)) for i in range(limit)})


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _generate(
    transcript: str,
    frames: list[bytes],
    *,
    hint: str | None,
    extra_system: str = "",
    times: list[float] | None = None,
) -> dict[str, Any]:
    when = (
        "\nKeyframe capture times: "
        + ", ".join(f"{i} at {_mmss(t)}" for i, t in enumerate(times)) + "."
        if times else ""
    )
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                (f"Context from the supervisor: {hint}\n\n" if hint else "")
                + f"Expert narration (transcribed):\n{transcript or '(no speech captured)'}"
                + f"\n\nThe {len(frames)} images that follow are keyframes in "
                + "chronological order, indexed from 0." + when
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
    times: list[float] | None = None,
) -> dict[str, Any]:
    """Ground every target_phrase; regenerate the failures once.

    `segment_fn` and `generate_fn` are injectable so this is unit-testable
    without touching the network.
    """
    segment_fn = segment_fn or meta_client.segment
    generate_fn = generate_fn or (
        lambda: _generate(transcript, frames, hint=hint, times=times,
                          extra_system=REPAIR_SUFFIX.format(failures=failures_block))
    )

    failures = _check_grounding(proc["steps"], frames, segment_fn)
    # Hazards and tools are checked the same way: a hazard phrase SAM can't
    # find would otherwise be silently unwatched during guidance.
    watch_missing = _check_watch(proc["steps"], frames, segment_fn)
    report: dict[str, Any] = {
        "ran": True,
        "steps_checked": len(proc["steps"]),
        "failed_before": [f["target_phrase"] for f in failures],
        "watch_missing_before": [w["phrase"] for _, w in watch_missing],
        "repaired": False,
    }

    if failures or watch_missing:
        failures_block = "\n".join(
            [f"  - step {f['index'] + 1} ({f['title']!r}): "
             f"target_phrase {f['target_phrase']!r} not found" for f in failures]
            + [f"  - step {s['index'] + 1} ({s['title']!r}): watch_for {w['role']} {w['phrase']!r} "
               f"not found; name it the way it looks in the frames, or drop it if it isn't visible"
               for s, w in watch_missing]
        )
        log.info("%d target and %d watch phrase(s) failed grounding; regenerating",
                 len(failures), len(watch_missing))
        try:
            repaired = generate_fn()
            still = _check_grounding(repaired["steps"], frames, segment_fn)
            still_watch = _check_watch(repaired["steps"], frames, segment_fn)
            report["repaired"] = True
            report["failed_after"] = [f["target_phrase"] for f in still]
            # Only accept the repair if it did not make things worse.
            if len(still) + len(still_watch) <= len(failures) + len(watch_missing):
                proc = repaired
            else:
                report["failed_after"] = report["failed_before"]
        except Exception:  # noqa: BLE001
            log.exception("Repair regeneration failed; keeping original procedure")
            report["failed_after"] = report["failed_before"]
    else:
        report["failed_after"] = []

    # Every watch item now carries `trackable` from its own check; anything
    # SAM still can't find is shown as not tracked rather than pretended.
    report["watch_untracked"] = [w["phrase"] for s in proc["steps"] for w in (s.get("watch_for") or [])
                                 if w.get("trackable") is False]
    proc["_validation"] = report
    return proc


def _check_watch(
    steps: list[dict[str, Any]],
    frames: list[bytes],
    segment_fn: Callable[[str, bytes], list[Any]],
) -> list[tuple[dict[str, Any], dict[str, str]]]:
    """Ground each step's watch_for phrases on its keyframe, in parallel.

    Sets item["trackable"] in place and returns the (step, item) pairs SAM
    could not find. A SAM error is not evidence of a bad phrase: those stay
    trackable.
    """
    from concurrent.futures import ThreadPoolExecutor

    jobs = []
    for step in steps:
        ki = step.get("keyframe_index")
        if not isinstance(ki, int) or not (0 <= ki < len(frames)):
            ki = min(step.get("index", 0), len(frames) - 1)
        for item in step.get("watch_for") or []:
            jobs.append((step, item, frames[ki]))
    if not jobs:
        return []

    def found(job) -> bool:
        try:
            return bool(segment_fn(job[1]["phrase"], job[2]))
        except Exception as exc:  # noqa: BLE001
            log.warning("Watch check errored for %r: %s", job[1]["phrase"], exc)
            return True

    with ThreadPoolExecutor(max_workers=min(6, len(jobs))) as pool:
        ok = list(pool.map(found, jobs))
    missing = []
    for (step, item, _), hit in zip(jobs, ok):
        item["trackable"] = hit
        if not hit:
            missing.append((step, item))
    return missing


def _check_grounding(
    steps: list[dict[str, Any]],
    frames: list[bytes],
    segment_fn: Callable[[str, bytes], list[Any]],
) -> list[dict[str, Any]]:
    """Return the steps whose target_phrase SAM cannot find in its keyframe.

    The checks are independent, so they run in parallel: one SAM round-trip of
    wall time instead of one per step.
    """
    from concurrent.futures import ThreadPoolExecutor

    def one(step: dict[str, Any]) -> bool:
        """True when the step fails grounding."""
        if step.get("check") == "confirm":
            return False                      # not visually checkable by design
        phrase = (step.get("target_phrase") or "").strip()
        if not phrase:
            return True
        ki = step.get("keyframe_index")
        if not isinstance(ki, int) or not (0 <= ki < len(frames)):
            ki = min(step.get("index", 0), len(frames) - 1)
        try:
            return not segment_fn(phrase, frames[ki])
        except Exception as exc:  # noqa: BLE001 - a SAM error is not a bad phrase
            log.warning("Grounding check errored for %r: %s", phrase, exc)
            return False

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(steps)))) as pool:
        failed = list(pool.map(one, steps))
    return [s for s, bad in zip(steps, failed) if bad]


# --------------------------------------------------------------------------

def _clean_watch(items: Any, target: Any) -> list[dict[str, str]]:
    """Up to two extra objects to track during a step: hazards to keep clear
    of and the tool to use. Each one costs a SAM call per changed frame."""
    out: list[dict[str, str]] = []
    seen = {str(target or "").strip().lower()}
    for it in items if isinstance(items, list) else []:
        if not isinstance(it, dict):
            continue
        phrase = " ".join(str(it.get("phrase") or "").split())[:60]
        role = str(it.get("role") or "").lower()
        if phrase and role in ("hazard", "tool") and phrase.lower() not in seen:
            seen.add(phrase.lower())
            out.append({"phrase": phrase, "role": role})
        if len(out) == 2:
            break
    return out


def _clean_reading(r: Any) -> dict[str, Any] | None:
    """{'what','unit','min','max'} with numeric bounds, or None if unusable."""
    if not isinstance(r, dict):
        return None
    def num(v):
        try:
            return float(v) if v is not None and str(v).strip() != "" else None
        except (TypeError, ValueError):
            return None
    lo, hi = num(r.get("min")), num(r.get("max"))
    if lo is None and hi is None:
        return None
    if lo is not None and hi is not None and lo > hi:
        lo, hi = hi, lo
    return {"what": str(r.get("what") or "reading").strip(), "unit": str(r.get("unit") or "").strip(),
            "min": lo, "max": hi}


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
        check = str(raw.get("check") or "visual").lower()
        if check not in ("visual", "reading", "timed", "confirm"):
            check = "visual"
        reading = _clean_reading(raw.get("reading"))
        if check == "reading" and not reading:
            check = "visual"                      # a reading step needs a range to check against
        duration = raw.get("duration_s")
        duration = int(duration) if isinstance(duration, (int, float)) and 0 < duration <= 4 * 3600 else None
        if check == "timed" and not duration:
            check = "visual"
        steps.append(
            {
                "index": len(steps),
                "title": (raw.get("title") or f"Step {i + 1}").strip(),
                "instruction": instruction,
                "target_phrase": (raw.get("target_phrase") or "").strip(),
                "success_criteria": (raw.get("success_criteria") or "").strip(),
                "keyframe_index": ki,
                "check": check,
                "reading": reading if check == "reading" else None,
                "duration_s": duration if check == "timed" else None,
                "watch_for": _clean_watch(raw.get("watch_for"), raw.get("target_phrase")),
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
