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
      "hazard": "<safety warning to speak before the step, or null>",
      "expert_said": "<the expert's own words about this step, quoted verbatim
                      from the narration (one to three sentences, keep their
                      tips and reasons), or null if they said nothing about it>"
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
  * Write title, instruction, success_criteria, hazard and expert_said in the
    same language as the narration (Hindi stays in Devanagari).
  * target_phrase and every watch_for phrase are ALWAYS plain English noun
    phrases, whatever language the expert speaks: a segmentation model that
    only reads English searches for them.
"""

# Phrases SAM couldn't find are repaired with ONE small call that proposes
# alternatives for just those phrases, each tested by SAM in parallel. It
# replaced a full regeneration with every image again, which took 60 s and
# doubled the tokens on a real 11 s take.
REPAIR_SYSTEM = """\
A segmentation model could not find these part names in the image each was
meant for. For each one, propose 3 alternative names for the SAME object, as
it plainly looks in its image: colour, shape, material, position, any words
printed on it. Each is a noun phrase of 1 to 4 words: no verbs, no articles,
no "in hand", no "on the left of".

Return JSON: {"fixes": [{"id": <id>, "candidates": ["...", "...", "..."]}]}
"""

# Long recordings are written part by part while the expert is still
# recording (see live.CaptureSession), then merged. A part sees only its own
# few minutes of narration and frames, so cost grows with the length of the
# job instead of exploding in one giant call, and at stop only the last part
# and a text-only merge remain.
SEGMENT_NOTE = """

THIS IS PART {k} OF A LONGER RECORDING ({start} to {end}). Write only the
steps that happen in this part, in order: 1 to 8 steps (this replaces the
step count rule above). Steps already written for earlier parts:
{previous}
Do not repeat those. If a step started in the previous part and finishes
here, write it once, here, in full. "title" is the whole job as you
understand it so far.
"""

MERGE_SYSTEM = """\
You merge the draft steps of one long expert recording, written part by part,
into one clean procedure for a new worker. The drafts are in time order.

  * Merge duplicates and any step split across a part boundary into one.
  * Keep every distinct action, in order. Never invent a step.
  * Keep lockout, isolation and other safety steps as their own steps.
  * Keep each step's fields. For a merged step keep the clearest wording and
    the "frame" of the draft that best shows its target.
  * "frame" must be one of the frame numbers given in the drafts.

Return JSON: {"title": "...", "machine": "... or null", "steps": [ {the same
fields as the drafts, including "frame"} ]}
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
            proc = _validate_and_repair(proc, frames)
        except Exception:  # noqa: BLE001 - validation is best-effort, never fatal
            log.exception("Procedure self-check failed; returning unvalidated procedure")
    # keyframe_index points into the frames the writer saw; the caller keeps
    # those frames as each step's reference photo.
    proc["_frames"] = frames
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
    effort: str | None = None,
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

    data = meta_client.reason_json(SYSTEM + extra_system, content, effort=effort or config.WRITER_EFFORT)
    return _normalise(data, n_frames=len(frames))


def draft_segment(
    transcript: str,
    frames: list[bytes],
    times: list[float],
    *,
    k: int,
    previous: list[str],
    hint: str | None = None,
) -> dict[str, Any]:
    """Write the steps of one part of a long recording. keyframe_index in the
    result is local to `frames` (the caller maps it back)."""
    note = SEGMENT_NOTE.format(
        k=k, start=_mmss(times[0]) if times else "?", end=_mmss(times[-1]) if times else "?",
        previous="\n".join(f"  - {t}" for t in previous[-20:]) or "  (none: this is the first part)")
    return _generate(transcript, frames, hint=hint, times=times, extra_system=note)


def merge_drafts(drafts: list[dict[str, Any]], n_frames: int, *, hint: str | None = None) -> dict[str, Any]:
    """One text-only call: the parts' draft steps become one procedure.
    Each draft step carries "frame", an index into the whole take."""
    import json

    keep = ("title", "instruction", "target_phrase", "success_criteria", "check", "reading", "duration_s",
            "watch_for", "common_mistake", "hazard", "expert_said")
    parts = [{"part": i + 1, "steps": [{**{f: s.get(f) for f in keep}, "frame": s["frame"]}
                                       for s in d.get("steps", [])]} for i, d in enumerate(drafts)]
    text = ((f"Context from the supervisor: {hint}\n\n" if hint else "")
            + "Draft titles of the job: " + "; ".join(d.get("title", "") for d in drafts)
            + "\n\nDrafts, in time order:\n" + json.dumps(parts, ensure_ascii=False))
    data = meta_client.reason_json(MERGE_SYSTEM, [{"type": "input_text", "text": text}],
                                   effort=config.WRITER_EFFORT)
    if isinstance(data, dict):
        for s in data.get("steps") or []:
            if isinstance(s, dict):
                s["keyframe_index"] = s.get("frame")
    return _normalise(data, n_frames=n_frames)


# --------------------------------------------------------------------------
# Grounding self-check
# --------------------------------------------------------------------------

def _frame_for(step: dict[str, Any], frames: Any) -> bytes:
    ki = step.get("keyframe_index")
    if not isinstance(ki, int) or not (0 <= ki < len(frames)):
        ki = min(step.get("index", 0), len(frames) - 1)
    return frames[ki]


def _suggest_phrases(items: list[dict[str, Any]], frames: Any) -> dict[int, list[str]]:
    """One small Spark call: 3 alternative names for each phrase SAM missed,
    looking at each one's own keyframe. {item id: [candidates]}."""
    content: list[dict[str, Any]] = []
    lines = []
    for it in items:
        step = it["step"]
        what = "target part" if it["kind"] == "target" else f"{it['role']} to watch"
        lines.append(f"id {it['id']}: step {step.get('index', 0) + 1} \"{step.get('title', '')}\" "
                     f"({step.get('instruction', '')}). {what}: \"{it['old']}\". Its image is image {it['id']}.")
    content.append({"type": "input_text", "text": "\n".join(lines)})
    for it in items:
        content.append({"type": "input_text", "text": f"Image {it['id']}:"})
        content.append(meta_client.as_image_part(_frame_for(it["step"], frames)))
    data = meta_client.reason_json(REPAIR_SYSTEM, content, effort=config.REPAIR_EFFORT)
    out: dict[int, list[str]] = {}
    for fix in (data or {}).get("fixes", []) if isinstance(data, dict) else []:
        try:
            cands = [" ".join(str(c).split())[:60] for c in fix.get("candidates", []) if str(c).strip()]
            out[int(fix["id"])] = cands[:3]
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _validate_and_repair(
    proc: dict[str, Any],
    frames: Any,
    *,
    segment_fn: Callable[[str, bytes], list[Any]] | None = None,
    repair_fn: Callable[[list[dict[str, Any]], Any], dict[int, list[str]]] | None = None,
) -> dict[str, Any]:
    """Ground every target, hazard and tool phrase on its own keyframe; for
    the ones SAM can't find, ask once for alternatives and keep the first
    alternative SAM finds. `frames` is any indexable sequence of JPEG bytes.

    `segment_fn` and `repair_fn` are injectable so this is unit-testable
    without touching the network.
    """
    from concurrent.futures import ThreadPoolExecutor

    segment_fn = segment_fn or meta_client.segment
    repair_fn = repair_fn or _suggest_phrases

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
    items = ([{"id": i, "kind": "target", "step": s, "old": s.get("target_phrase") or ""}
              for i, s in enumerate(failures)]
             + [{"id": len(failures) + j, "kind": "watch", "step": s, "item": w, "old": w["phrase"],
                 "role": w["role"]} for j, (s, w) in enumerate(watch_missing)])
    if items:
        log.info("%d target and %d watch phrase(s) failed grounding; asking for alternatives",
                 len(failures), len(watch_missing))
        try:
            cands = repair_fn(items, frames)
        except Exception:  # noqa: BLE001
            log.exception("Phrase repair failed; keeping the original phrases")
            cands = {}
        jobs = [(it, c) for it in items for c in cands.get(it["id"], [])]

        def test(job):
            try:
                return segment_fn(job[1], _frame_for(job[0]["step"], frames))
            except Exception as exc:  # noqa: BLE001
                log.warning("SAM check of %r failed: %s", job[1], exc)
                return []
        with ThreadPoolExecutor(max_workers=max(1, min(8, len(jobs)))) as pool:
            hits = list(pool.map(test, jobs))
        fixed: set[int] = set()
        for (it, cand), dets in zip(jobs, hits):
            if it["id"] in fixed or not dets:
                continue                         # candidates are tried in the order given
            fixed.add(it["id"])
            if it["kind"] == "target":
                it["step"]["target_phrase"] = cand
                best = max(dets, key=lambda d: getattr(d, "confidence", 0) or 0)
                if hasattr(best, "to_dict"):
                    it["step"]["ref_box"] = best.to_dict()
            else:
                it["item"]["phrase"], it["item"]["trackable"] = cand, True
        report["repaired"] = bool(fixed)
        report["failed_after"] = [it["step"].get("target_phrase") for it in items
                                  if it["kind"] == "target" and it["id"] not in fixed]
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
        """True when the step fails grounding. A hit keeps its best box as
        `ref_box`: where the part sits in the expert's photo, for "show me"."""
        if step.get("check") == "confirm":
            return False                      # not visually checkable by design
        phrase = (step.get("target_phrase") or "").strip()
        if not phrase:
            return True
        ki = step.get("keyframe_index")
        if not isinstance(ki, int) or not (0 <= ki < len(frames)):
            ki = min(step.get("index", 0), len(frames) - 1)
        try:
            dets = segment_fn(phrase, frames[ki])
        except Exception as exc:  # noqa: BLE001 - a SAM error is not a bad phrase
            log.warning("Grounding check errored for %r: %s", phrase, exc)
            return False
        best = max(dets, key=lambda d: getattr(d, "confidence", 0) or 0, default=None)
        if best is not None and hasattr(best, "to_dict"):
            step["ref_box"] = best.to_dict()
        return not dets

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
                "expert_said": (str(raw.get("expert_said") or "").strip()[:400]) or None,
            }
        )

    if not steps:
        raise ValueError("Procedure generator produced no usable steps")

    return {
        "title": (data.get("title") or "Untitled procedure").strip(),
        "machine": data.get("machine") or None,
        "steps": steps,
    }
