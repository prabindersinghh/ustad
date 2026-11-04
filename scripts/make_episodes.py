"""Synthetic verifiable episodes from the factory clip, for fine-tuning Glimmer.

Each episode is one call to the production judge: the same system prompt, the
same tools and the same user message, because the prompt is captured from
`guide.judge_step` itself rather than rebuilt here. Only the answer is ours.

Subcommands, in order:

    python -m scripts.make_episodes timeline          # free: frames, title card, cuts, labels
    python -m scripts.make_episodes ground --limit 8  # paid: SAM on base frames, priced sample
    python -m scripts.make_episodes ground            # paid: SAM on every base frame
    python -m scripts.make_episodes check             # free: my labels against SAM's hands
    python -m scripts.make_episodes build             # paid: SAM on derived images, writes JSONL

Every SAM result is cached by image hash + phrase in data/factory/rl/sam_cache.json,
so a restart resumes and nothing is paid for twice. Output lives in
data/factory/rl/, which git excludes: it holds factory frames.

The labels in PHASES come from an agent viewing contact sheets of the frames.
No expert reviewed them. The clip is one 42 s recording of one machine, so
anything measured on these episodes describes this clip, not other machines.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter

from backend import config, guide, meta_client, sam_parse

CLIP = config.ROOT / "data" / "factory" / "factory.mp4"
PROCEDURE = config.ROOT / "data" / "factory" / "procedure.json"
OUT = config.ROOT / "data" / "factory" / "rl"
FRAMES = OUT / "frames"
DERIVED = OUT / "derived"
CACHE_PATH = OUT / "sam_cache.json"
TIMELINE_PATH = OUT / "step_timeline.json"

FPS = 60
FRAME_STEP = 11              # every 11th frame: about 5.5 per second
TRAIN_END_S = 31.5           # train strictly before this
EVAL_START_S = 32.0          # held-out eval from here on
CUT_MARGIN_S = 0.10          # around a hard cut
TITLE_MARGIN_S = 0.30        # around the title card
HAND_PHRASE = "human hand"
SAM_THREADS = 4              # the running server shares this key
SPEND_LIMIT_USD = 8.0        # this task's Meta allowance
BLUR_SIGMAS = (8, 10, 12)    # the brief's floor is 8
GONE_BELOW = 0.2             # a target this little in view is out of frame
KEPT_ABOVE = 0.9             # a target this much in view is still judgeable
MIN_CROP_SIDE = 0.38         # a crop that removes the target keeps this share of each side

# SAM found no match for these targets on sampled frames, so a crop can't be
# planned from its boxes. Where they sit in this clip, from viewing frames
# across all the camera positions (x1, y1, x2, y2 on 480x752), generous enough
# to include the print head's forward travel. Step 4's criteria covers the print
# head and the fixture, so its region starts where the print head does.
VIEWED_REGION: dict[int, tuple[float, float, float, float]] = {
    2: (130, 330, 290, 520),   # red ink pad
    3: (95, 340, 290, 480),    # teal silicone pad
    4: (90, 300, 480, 752),    # print head and rotary fixture
}

# What each frame shows, from viewing it. (first t, last t, phase), times as
# printed on the contact sheets. Frames in no range are left out as unclear.
#
#   H   a hand is on a blue part, print head back
#   E   a hand is in view by the fixture, not clearly on a part
#   F   print head forward over the fixture with a hand right beside it
#   Ef  print head forward, a hand at the frame's edge: not beside the head, but
#       the head is out, so only "hands clear" is safe to call
#   N   no hand in view
#   Nc  no hand in view, but in a stretch where fingertips come and go around a
#       cream block, so "hands clear" is not safe to assert
#
# Contact sheets were not enough: a first pass called 18 fingertip frames "no
# hand". SAM's hand detections caught 3 of them, and enlarged crops of the right
# edge of every no-hand frame found the other 15, which SAM misses. Frames left
# out below (6.60, 6.97, 15.22, 15.58, 40.88, 41.07) are ones those crops could
# not settle.
# The cream block at 8.62-12.28 sits on a bracket on the fixture: not a hand.
PHASES: list[tuple[float, float, str]] = [
    (0.00, 0.00, "E"), (0.18, 2.57, "H"), (2.75, 3.48, "F"), (3.67, 5.32, "N"),
    (5.50, 5.50, "E"), (5.68, 6.05, "H"), (6.23, 6.23, "H"), (6.42, 6.42, "E"),
    (6.78, 6.78, "E"), (7.15, 8.07, "E"), (8.62, 12.28, "N"), (12.47, 14.48, "E"),
    (14.67, 15.03, "F"), (15.40, 15.40, "E"), (15.77, 15.77, "E"),
    (19.25, 20.17, "H"), (20.35, 20.53, "E"), (20.72, 21.45, "Ef"), (21.63, 22.00, "F"),
    (22.18, 22.18, "E"), (22.37, 24.57, "H"), (24.93, 26.03, "F"), (26.22, 28.23, "H"),
    (28.42, 29.15, "F"), (29.33, 30.07, "Ef"), (30.25, 30.43, "E"), (30.62, 31.90, "H"),
    (32.08, 33.18, "E"), (33.37, 33.55, "Nc"), (33.73, 33.92, "Ef"), (34.10, 34.28, "Nc"),
    (34.47, 36.85, "H"), (37.03, 37.77, "F"), (37.95, 39.78, "N"), (39.97, 40.52, "H"),
    (40.70, 40.70, "E"), (41.25, 42.40, "E"),
]

# For each phase, each step's truth: "hold" (criteria met), "fail" (really not
# met, target in view), "weak" (not met as written, but see below), or absent
# (unclear, never used). Step 0 and step 3 never fail: the button and the teal
# pad are in view and unblocked in every frame.
#
# "weak": step 1 asks for a hand on a part, and a frame with no hand fails that
# as written. But parts sit on the fixture, so the worker may have seated one
# and let go, and the system prompt lets a later state prove an earlier step.
# Kept and tagged so the set can be reported or trained without them. Without
# them, nothing teaches the model that step 1 can fail at all.
#
# "F" gives no verify labels: the system prompt makes anything unsafe a
# flag_error whatever the step, and a hand beside the forward print head
# could count. Steps 2 and 4 are plainly unmet there, so those stay as fails.
TRUTH: dict[str, dict[int, str]] = {
    "H":  {0: "hold", 1: "hold", 2: "hold", 3: "hold", 4: "fail"},
    "E":  {0: "hold", 2: "hold", 3: "hold", 4: "fail"},
    "F":  {2: "fail", 4: "fail"},
    "Ef": {4: "fail"},
    "N":  {0: "hold", 1: "weak", 2: "hold", 3: "hold", 4: "hold"},
    "Nc": {0: "hold", 1: "weak", 2: "hold", 3: "hold"},
}
HAND_DEPENDENT = {1, 4}      # the truth of these rests on where the hands are
HAND_PHASES = ("H", "E", "F", "Ef")

STEP_SEEN = {
    0: "the button is in view and nothing blocks it",
    1: "a hand is on the blue part on the fixture",
    2: "the pad is in view with no hand on or under it",
    3: "the pad is in place at the print station",
    4: "no hand is near the print head or the fixture",
}
SAY_OK = {
    0: "Emergency stop located.",
    1: "Blue part is seated.",
    2: "Ink pad looks clear.",
    3: "Silicone pad is in place.",
    4: "Hands are clear.",
}


# ---------------------------------------------------------------------------
# Timeline: frames, title card, cuts, labels
# ---------------------------------------------------------------------------

def phase_at(t: float, phases: list[tuple[float, float, str]] = PHASES) -> str | None:
    for first, last, phase in phases:
        if first - 0.006 <= t <= last + 0.006:
            return phase
    return None


def is_title_card(im: Image.Image) -> bool:
    """The clip's title card is flat blue with white text. Measured, not assumed."""
    small = im.convert("RGB").resize((32, 32))
    px = [small.getpixel((x, y)) for x in range(32) for y in range(32)]
    blue = sum(1 for r, g, b in px if b > 180 and r < 70 and 40 < g < 110)
    return blue / len(px) > 0.5


def extract_frames() -> list[int]:
    """Every FRAME_STEP-th frame as n<index>.jpg. Skips frames already on disk."""
    FRAMES.mkdir(parents=True, exist_ok=True)
    have = sorted(int(p.stem[1:]) for p in FRAMES.glob("n*.jpg"))
    if have:
        return have
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(CLIP),
         "-vf", f"select='not(mod(n\\,{FRAME_STEP}))'", "-fps_mode", "passthrough",
         "-q:v", "2", str(FRAMES / "s%05d.jpg")], check=True)
    for p in sorted(FRAMES.glob("s*.jpg")):
        p.rename(FRAMES / f"n{(int(p.stem[1:]) - 1) * FRAME_STEP:05d}.jpg")
    return sorted(int(p.stem[1:]) for p in FRAMES.glob("n*.jpg"))


def scan_cuts() -> list[float]:
    """Hard cuts in the clip, from ffmpeg's scene score over every frame."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(CLIP), "-vf", "select='gt(scene\\,0.25)',showinfo",
         "-f", "null", "-"], capture_output=True, text=True, check=True)
    return [float(m) for m in re.findall(r"pts_time:([0-9.]+)", proc.stderr)]


def exclusion(t: float, title: tuple[float, float] | None, cuts: list[float]) -> str | None:
    if title and title[0] - TITLE_MARGIN_S <= t <= title[1] + TITLE_MARGIN_S:
        return "title card"
    if any(abs(t - c) <= CUT_MARGIN_S for c in cuts):
        return "beside a cut"
    return None


def split_of(t: float) -> str | None:
    if t < TRAIN_END_S:
        return "train"
    if t >= EVAL_START_S:
        return "eval"
    return None


def step_truth(phase: str | None) -> dict[int, str]:
    return dict(TRUTH.get(phase or "", {}))


def cmd_timeline(_: argparse.Namespace) -> None:
    indices = extract_frames()
    cuts = scan_cuts()
    title_ts = [i / FPS for i in indices if is_title_card(Image.open(FRAMES / f"n{i:05d}.jpg"))]
    title = (min(title_ts), max(title_ts)) if title_ts else None
    frames = []
    for i in indices:
        t = i / FPS
        why = exclusion(t, title, cuts)
        split = split_of(t)
        if split is None and not why:
            why = "in the gap between train and eval"
        phase = phase_at(t)
        if not why and phase is None:
            why = "unclear when viewed"
        frames.append({
            "frame": i, "t": round(t, 3), "split": None if why else split, "excluded": why,
            "phase": None if why else phase, "steps": {} if why else
            {str(k): v for k, v in step_truth(phase).items()},
        })
    doc = {
        "provenance": "Labels are an AI agent's, from viewing contact sheets of the frames. "
                      "No expert reviewed them. The procedure itself is a draft.",
        "clip": str(CLIP.name), "fps": FPS, "frame_step": FRAME_STEP,
        "cuts_detected_s": cuts, "title_card_detected_s": title,
        "train_before_s": TRAIN_END_S, "eval_from_s": EVAL_START_S, "frames": frames,
    }
    TIMELINE_PATH.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    counts: dict[str, int] = {}
    for f in frames:
        key = f"{f['split']}/{f['phase']}" if f["split"] else f"excluded: {f['excluded']}"
        counts[key] = counts.get(key, 0) + 1
    print(json.dumps({"cuts": cuts, "title_card": title, "frames": counts}, indent=1, sort_keys=True))


def load_timeline() -> list[dict[str, Any]]:
    return json.loads(TIMELINE_PATH.read_text(encoding="utf-8"))["frames"]


# ---------------------------------------------------------------------------
# SAM, cached
# ---------------------------------------------------------------------------

class SamCache:
    """Detections by sha256(image) + phrase. Failures are never cached, so a
    network error can't pass for "not found"."""

    def __init__(self, path: Path = CACHE_PATH) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.paid = 0
        self.errors: list[str] = []
        self._since_save = 0

    @staticmethod
    def key(image: bytes, phrase: str) -> str:
        return f"{hashlib.sha256(image).hexdigest()}|{phrase}"

    def get(self, image: bytes, phrase: str) -> list[dict[str, Any]] | None:
        return self.data.get(self.key(image, phrase))

    def put(self, image: bytes, phrase: str, dets: list[sam_parse.Detection]) -> None:
        rows = [{"frame": d.frame, "ordinal": d.ordinal, "x1": d.x1, "y1": d.y1, "x2": d.x2, "y2": d.y2,
                 "img_w": d.img_w, "img_h": d.img_h, "confidence": d.confidence} for d in dets]
        with self.lock:
            self.data[self.key(image, phrase)] = rows
            self.paid += 1
            self._since_save += 1
            if self._since_save >= 20:
                self._save_locked()

    def _save_locked(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        os.replace(tmp, self.path)
        self._since_save = 0

    def save(self) -> None:
        with self.lock:
            self._save_locked()


def to_dets(rows: list[dict[str, Any]]) -> list[sam_parse.Detection]:
    return [sam_parse.Detection(**r) for r in rows]


def segment_all(cache: SamCache, jobs: list[tuple[bytes, str]], *, budget_calls: int) -> None:
    """Run SAM on every (image, phrase) not cached yet, up to a call limit."""
    todo, seen = [], set()
    for image, phrase in jobs:
        k = cache.key(image, phrase)
        if k not in cache.data and k not in seen:
            seen.add(k)
            todo.append((image, phrase))
    if len(todo) > budget_calls:
        cache.save()
        sys.exit(f"Refusing: {len(todo)} SAM calls needed, the limit is {budget_calls} "
                 f"(${budget_calls * config.PRICE_SAM_IMAGE:.2f}). Nothing was sent.")

    def run(job: tuple[bytes, str]) -> None:
        image, phrase = job
        for attempt in range(4):
            try:
                cache.put(image, phrase, meta_client.segment(phrase, image))
                return
            except Exception as exc:  # noqa: BLE001 - retried, then reported, never cached
                last = f"{type(exc).__name__}: {str(exc)[:160]}"
                time.sleep(2 ** attempt)
        cache.errors.append(f"{phrase}: {last}")

    with ThreadPoolExecutor(max_workers=SAM_THREADS) as pool:
        for done, fut in enumerate(as_completed([pool.submit(run, j) for j in todo]), 1):
            fut.result()
            if done % 100 == 0:
                print(f"  SAM {done}/{len(todo)}", flush=True)
    cache.save()
    if cache.errors:
        print(f"  {len(cache.errors)} SAM calls failed and were not cached. First: {cache.errors[0]}")


def budget_calls() -> int:
    """SAM calls this task may still make. `sam_cache.json` holds one entry per
    paid call, so the count survives restarts."""
    done = len(json.loads(CACHE_PATH.read_text(encoding="utf-8"))) if CACHE_PATH.exists() else 0
    return int(SPEND_LIMIT_USD / config.PRICE_SAM_IMAGE) - done


def load_steps() -> list[dict[str, Any]]:
    return json.loads(PROCEDURE.read_text(encoding="utf-8"))["steps"]


def frame_bytes(frame: int) -> bytes:
    return (FRAMES / f"n{frame:05d}.jpg").read_bytes()


def cmd_ground(args: argparse.Namespace) -> None:
    steps = load_steps()
    frames = [f for f in load_timeline() if f["split"]]
    if args.limit:   # a spread sample across the clip, for pricing
        stride = max(1, len(frames) // args.limit)
        frames = frames[::stride][:args.limit]
    phrases = [s["target_phrase"] for s in steps] + [HAND_PHRASE]
    jobs = [(frame_bytes(f["frame"]), p) for f in frames for p in phrases]
    cache = SamCache()
    started = time.perf_counter()
    segment_all(cache, jobs, budget_calls=budget_calls())
    took = time.perf_counter() - started
    print(json.dumps({
        "frames": len(frames), "phrases": len(phrases), "paid_calls_this_run": cache.paid,
        "cost_this_run_usd": round(cache.paid * config.PRICE_SAM_IMAGE, 4), "seconds": round(took, 1),
        "seconds_per_paid_call": round(took / cache.paid, 3) if cache.paid else None,
        "grounding_rate": grounding_rates(cache, frames, phrases),
    }, indent=1))


def grounding_rates(cache: SamCache, frames: list[dict[str, Any]], phrases: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in phrases:
        hits = total = 0
        confs: list[float] = []
        for f in frames:
            rows = cache.get(frame_bytes(f["frame"]), p)
            if rows is None:
                continue
            total += 1
            if rows:
                hits += 1
                confs.append(max(r["confidence"] or 0 for r in rows))
        out[p] = {"found": hits, "of": total,
                  "median_top_confidence": round(sorted(confs)[len(confs) // 2], 3) if confs else None}
    return out


def cmd_check(_: argparse.Namespace) -> None:
    """Where SAM's hand detections disagree with my viewing. Those frames get a second look."""
    cache = SamCache()
    rows = []
    for f in load_timeline():
        if not f["split"]:
            continue
        hands = cache.get(frame_bytes(f["frame"]), HAND_PHRASE)
        if hands is None:
            continue
        sees = any((d["confidence"] or 0) >= 0.5 for d in hands)
        says = f["phase"] in HAND_PHASES
        if sees != says:
            rows.append({"t": f["t"], "phase": f["phase"],
                         "sam_top": max([d["confidence"] or 0 for d in hands], default=None)})
    print(json.dumps({"disagreements": rows, "count": len(rows)}, indent=0))


# ---------------------------------------------------------------------------
# Prompt: captured from the production judge
# ---------------------------------------------------------------------------

class _Stop:
    output: list[Any] = []
    output_text = ""


def capture_prompt(step: dict[str, Any], image: bytes, dets: list[sam_parse.Detection],
                   steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Run `guide.judge_step` with the model call replaced, and keep what it
    would have sent. `upcoming` matches `live._upcoming`."""
    captured: dict[str, Any] = {}

    def fake(system: str, content: list[dict[str, Any]], *, tools: Any = None, **_: Any) -> _Stop:
        captured.update(system=system, content=content, tools=tools)
        return _Stop()

    real = meta_client.reason
    meta_client.reason = fake  # type: ignore[assignment]
    try:
        action = guide.judge_step(step, image, detections=dets,
                                  upcoming=steps[step["index"] + 1: step["index"] + 4])
    finally:
        meta_client.reason = real  # type: ignore[assignment]
    captured["grounding"] = action["grounding"]
    return captured


def best_where(dets: list[sam_parse.Detection]) -> str | None:
    if not dets:
        return None
    top = max(dets, key=lambda d: (d.confidence or 0, d.area_fraction))
    return top.describe()


def grounded(phrase: str, dets: list[sam_parse.Detection]) -> str:
    where = best_where(dets)
    return f'Grounding found "{phrase}" at {where}' if where else f'Grounding found no match for "{phrase}"'


def answer(label: str, step: dict[str, Any], phase: str | None, dets: list[sam_parse.Detection],
           degraded: str | None = None) -> dict[str, Any]:
    """The correct tool call. Text is templated from the step and the real
    grounding, and `confidence` is a fixed value, not a measurement."""
    idx, phrase = step["index"], step["target_phrase"]
    g = grounded(phrase, dets)
    if label == "verify_step":
        return {"name": label, "arguments": {
            "confidence": 0.9 if dets else 0.75,
            "reason": f"{g}, and {STEP_SEEN[idx]}.",
            "say": SAY_OK[idx],
        }}
    if label == "request_better_view":
        problem = {"blur": "The frame is too blurred to judge",
                   "crop": f"The {phrase} is out of frame"}.get(degraded or "", "The view is not enough to judge")
        return {"name": label, "arguments": {
            "what_to_show": f"{problem}. {g}. Show the {phrase} clearly, in focus.",
            "say": f"Please show me the {phrase} clearly.",
        }}
    if phase == "Ef":
        return {"name": label, "arguments": {
            "severity": "major",
            "what_is_wrong": f"{g}, and a hand is still in view while the print head is forward.",
            "how_to_fix": "Keep your hands right away from the fixture and print head.",
            "say": "Hand still in view. Keep clear of the print head.",
        }}
    if phase == "F":
        return {"name": label, "arguments": {
            "severity": "danger",
            "what_is_wrong": f"{g}, and a hand is right beside the print head while it is forward.",
            "how_to_fix": "Take your hand away from the print head and the fixture.",
            "say": "Stop. Move your hand away from the print head.",
        }}
    if idx == 4:
        return {"name": label, "arguments": {
            "severity": "major",
            "what_is_wrong": f"{g}, and a hand is still at the fixture, so hands are not clear.",
            "how_to_fix": "Take your hands away before the fixture moves.",
            "say": "Hands are still at the fixture. Move them clear.",
        }}
    return {"name": label, "arguments": {
        "severity": "minor",
        "what_is_wrong": f"{g}, but no hand is on a blue part, so it is not being seated.",
        "how_to_fix": "Place a blue part on the fixture and seat it with your hand.",
        "say": "Seat the blue part with your hand.",
    }}


def to_row(cap: dict[str, Any], call: dict[str, Any], image: bytes) -> dict[str, Any]:
    """Fireworks chat JSONL. Tool-call rows, because production asks the open
    backend with tools and tool_choice=required: a JSON string in `content`
    would teach prose, and prose counts as no tool call in `judge_step`."""
    text = cap["content"][0]["text"]
    return {
        "messages": [
            {"role": "system", "content": cap["system"]},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(image).decode()}},
            ]},
            {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_0", "type": "function",
                "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)},
            }]},
        ],
        "tools": meta_client._to_chat_tools(cap["tools"]),
    }


# ---------------------------------------------------------------------------
# Image changes and what they do to the label
# ---------------------------------------------------------------------------

def jpeg(im: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    im.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def boxes_px(dets: list[sam_parse.Detection], w: int, h: int) -> list[tuple[float, float, float, float]]:
    """Detection boxes in this image's pixels (SAM reports its own image size)."""
    out = []
    for d in dets:
        sx, sy = (w / d.img_w if d.img_w else 1), (h / d.img_h if d.img_h else 1)
        out.append((d.x1 * sx, d.y1 * sy, d.x2 * sx, d.y2 * sy))
    return out


def visible_share(box: tuple[float, float, float, float], crop: tuple[float, float, float, float]) -> float:
    x1, y1, x2, y2 = box
    ix = max(0.0, min(x2, crop[2]) - max(x1, crop[0]))
    iy = max(0.0, min(y2, crop[3]) - max(y1, crop[1]))
    area = max(1e-6, (x2 - x1) * (y2 - y1))
    return ix * iy / area


def rotate_box(box: tuple[float, float, float, float], angle: float, w: int, h: int) -> tuple[float, float, float, float]:
    """Where a box lands after PIL's `rotate(angle)` about the image centre."""
    th = math.radians(angle)
    c, s = math.cos(th), math.sin(th)
    cx, cy = w / 2, h / 2
    xs, ys = [], []
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[0], box[3]), (box[2], box[3])):
        dx, dy = x - cx, y - cy
        xs.append(cx + dx * c + dy * s)
        ys.append(cy - dx * s + dy * c)
    return (min(xs), min(ys), max(xs), max(ys))


def after_geometry(label: str, step_idx: int, target_share: list[float], hand_share: list[float]) -> tuple[str | None, str]:
    """What a crop or rotation does to the right answer. Returns (label, note);
    a None label drops the episode.

      target gone from frame     -> request_better_view, whatever it was
      target half in frame       -> dropped, the truth is unclear
      a hand cut, step needs it  -> dropped: step 4 could read as "hands clear"
                                    when a hand is just out of frame
    """
    if not target_share:
        return None, "no_target_box"
    best = max(target_share)
    if best < GONE_BELOW:
        return "request_better_view", "target_cropped_out"
    if best < KEPT_ABOVE:
        return None, "target_partly_cut"
    if step_idx in HAND_DEPENDENT and any(s < KEPT_ABOVE for s in hand_share):
        return None, "hand_cut"
    return label, "kept"


def target_boxes(step_idx: int, dets: list[sam_parse.Detection], w: int, h: int) -> list[tuple[float, float, float, float]]:
    """SAM's boxes for the target, or where I saw it when SAM found nothing."""
    boxes = boxes_px(dets, w, h)
    if boxes:
        return boxes
    region = VIEWED_REGION.get(step_idx)
    return [(region[0] * w / 480, region[1] * h / 752, region[2] * w / 480, region[3] * h / 752)] if region else []


def removal_crop(boxes: list[tuple[float, float, float, float]], w: int, h: int) -> tuple[int, int, int, int] | None:
    """The largest rectangle holding no target box, if it keeps enough of the frame."""
    if not boxes:
        return None
    x1 = min(b[0] for b in boxes)
    y1 = min(b[1] for b in boxes)
    x2 = max(b[2] for b in boxes)
    y2 = max(b[3] for b in boxes)
    pad = 6
    options = [(0, 0, int(x1 - pad), h), (int(x2 + pad), 0, w, h),
               (0, 0, w, int(y1 - pad)), (0, int(y2 + pad), w, h)]
    legal = [o for o in options
             if (o[2] - o[0]) >= MIN_CROP_SIDE * w and (o[3] - o[1]) >= MIN_CROP_SIDE * h]
    return max(legal, key=lambda o: (o[2] - o[0]) * (o[3] - o[1])) if legal else None


def plan_augment(rng: random.Random, kind: str, w: int, h: int) -> dict[str, Any]:
    if kind == "brightness":
        return {"kind": kind, "factor": rng.choice((0.8, 1.2))}
    if kind == "rotate":
        return {"kind": kind, "angle": rng.choice((-1, 1)) * rng.uniform(2.0, 4.0)}
    keep_w, keep_h = rng.uniform(0.8, 0.95), rng.uniform(0.8, 0.95)
    x = rng.randint(0, int(w * (1 - keep_w)))
    y = rng.randint(0, int(h * (1 - keep_h)))
    return {"kind": "crop", "box": (x, y, x + int(w * keep_w), y + int(h * keep_h))}


def apply_augment(im: Image.Image, plan: dict[str, Any]) -> Image.Image:
    if plan["kind"] == "brightness":
        return ImageEnhance.Brightness(im).enhance(plan["factor"])
    if plan["kind"] == "rotate":
        w, h = im.size
        s = abs(math.sin(math.radians(plan["angle"])))
        mx, my = math.ceil(h / 2 * s), math.ceil(w / 2 * s)   # trims the black corners
        return im.rotate(plan["angle"], Image.Resampling.BICUBIC).crop((mx, my, w - mx, h - my))
    return im.crop(plan["box"])


def shares_after(plan: dict[str, Any], boxes: list[tuple[float, float, float, float]], w: int, h: int) -> list[float]:
    if plan["kind"] == "brightness":
        return [1.0 for _ in boxes]
    if plan["kind"] == "crop":
        return [visible_share(b, plan["box"]) for b in boxes]
    s = abs(math.sin(math.radians(plan["angle"])))
    mx, my = math.ceil(h / 2 * s), math.ceil(w / 2 * s)
    frame = (mx, my, w - mx, h - my)
    shares = []
    for b in boxes:
        turned = rotate_box(b, plan["angle"], w, h)
        area = max(1e-6, (b[2] - b[0]) * (b[3] - b[1]))
        turned_area = max(1e-6, (turned[2] - turned[0]) * (turned[3] - turned[1]))
        # Measured on the turned box's bounds: a rotated box only grows, so this
        # is the cautious side for a target near the edge.
        shares.append(visible_share(turned, frame) * min(1.0, turned_area / area))
    return shares


def pick_least_used(options: list[int], used: dict[int, int], rng: random.Random) -> int:
    rng.shuffle(options)
    return min(options, key=lambda s: used.get(s, 0))


def mismatch_options(truth: dict[int, str]) -> list[int]:
    """Steps that are really unmet here. Step 0 can't be one: the button is in
    every frame, so 'wrong step' would teach a false alarm."""
    return [s for s, v in truth.items() if v in ("fail", "weak") and s != 0]


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def store(image: bytes, name: str) -> str:
    DERIVED.mkdir(parents=True, exist_ok=True)
    path = DERIVED / f"{name}.jpg"
    if not path.exists():
        path.write_bytes(image)
    return str(path.relative_to(OUT)).replace("\\", "/")


def degrade(rng: random.Random, im: Image.Image, f: dict[str, Any], step: dict[str, Any], mode: str,
            target_dets: list[sam_parse.Detection]) -> tuple[bytes, dict[str, Any]] | None:
    w, h = im.size
    if mode == "crop":
        box = removal_crop(target_boxes(step["index"], target_dets, w, h), w, h)
        if box is None:
            return None
        return jpeg(im.crop(box)), {"degraded": "crop", "crop": box}
    sigma = rng.choice(BLUR_SIGMAS)
    return jpeg(im.filter(ImageFilter.GaussianBlur(radius=sigma))), {"degraded": "blur", "sigma": sigma}


def cmd_build(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    steps = load_steps()
    timeline = [f for f in load_timeline() if f["split"]]
    cache = SamCache()
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    def base_dets(f: dict[str, Any], phrase: str) -> list[sam_parse.Detection]:
        rows = cache.get(frame_bytes(f["frame"]), phrase)
        if rows is None:
            sys.exit(f"Base frame {f['frame']} has no SAM result for {phrase!r}. Run `ground` first.")
        return to_dets(rows)

    # 1. Plan every episode first, so the derived images' SAM calls run as one
    #    priced batch and the plan stays the same when a run resumes.
    plans: list[dict[str, Any]] = []
    used_match: dict[int, int] = {}
    used_mismatch: dict[int, int] = {}
    used_degrade: dict[int, int] = {}
    pair_truth: dict[str, int] = {}
    for f in timeline:
        truth = {int(k): v for k, v in f["steps"].items()}
        name = f"n{f['frame']:05d}"
        image = frame_bytes(f["frame"])
        im = Image.open(io.BytesIO(image)).convert("RGB")
        w, h = im.size
        holds = [s for s, v in truth.items() if v == "hold"]
        unmet = mismatch_options(truth)
        hands = base_dets(f, HAND_PHRASE)

        # Eval takes every valid pair on the untouched frame; train takes one of each.
        if f["split"] == "eval":
            pairs = [(s, "verify_step") for s in holds] + [(s, "flag_error") for s in unmet]
        else:
            pairs = []
            if holds:
                s = pick_least_used(list(holds), used_match, rng)
                used_match[s] = used_match.get(s, 0) + 1
                pairs.append((s, "verify_step"))
            else:
                drop("no_step_holds_in_this_frame")
            if unmet:
                s = pick_least_used(list(unmet), used_mismatch, rng)
                used_mismatch[s] = used_mismatch.get(s, 0) + 1
                pairs.append((s, "flag_error"))
            else:
                drop("no_real_mismatch_in_this_frame")
        # Every frame+step pair by its truth. A blind "other step = flag_error"
        # rule would have mislabelled the "hold" pairs and guessed the "unclear" ones.
        for s in range(len(steps)):
            key = f"{f['split']}/{truth.get(s, 'unclear')}"
            pair_truth[key] = pair_truth.get(key, 0) + 1

        for s, label in pairs:
            plans.append({"split": f["split"], "f": f, "step": s, "label": label, "image": image,
                          "aug": None, "weak": truth.get(s) == "weak", "id": f"{name}_s{s}_{label}"})

        # Degraded: blur or crop out the target. Both splits; eval's go in their own file.
        modes = ["blur", "crop"] if f["split"] == "eval" else [rng.choice(("blur", "crop"))]
        for mode in modes:
            s = pick_least_used(list(range(len(steps))), used_degrade, rng)
            used_degrade[s] = used_degrade.get(s, 0) + 1
            out = degrade(rng, im, f, steps[s], mode, base_dets(f, steps[s]["target_phrase"]))
            if out is None:
                drop(f"degrade_{mode}_no_room_to_crop_out_target")
                out = degrade(rng, im, f, steps[s], "blur", [])
                if out is None:
                    continue
            img, info = out
            suffix = info["degraded"]
            pid = f"{name}_s{s}_{suffix}"
            store(img, pid)
            plans.append({"split": "eval_degraded" if f["split"] == "eval" else "train", "f": f, "step": s,
                          "label": "request_better_view", "image": img, "aug": info, "weak": False, "id": pid})

        # Augmented copies of a train frame stay in train. No augmentation on eval.
        if f["split"] != "train":
            continue
        for kind in ("brightness", "rotate", "crop"):
            candidates = pairs
            if not candidates:
                continue
            s, label = candidates[rng.randrange(len(candidates))]
            plan = plan_augment(rng, kind, w, h)
            boxes = target_boxes(s, base_dets(f, steps[s]["target_phrase"]), w, h)
            final, note = after_geometry(label, s, shares_after(plan, boxes, w, h),
                                         shares_after(plan, boxes_px(hands, w, h), w, h))
            if kind == "brightness":
                final, note = label, "kept"      # brightness moves nothing
            elif s in HAND_DEPENDENT and f["phase"] in HAND_PHASES and not hands:
                # A hand I can see but SAM can't locate: no way to tell if this cuts it off.
                final, note = None, "hand_box_missing"
            if final is None:
                drop(f"augment_{kind}_{note}")
                continue
            img = jpeg(apply_augment(im, plan))
            pid = f"{name}_s{s}_aug_{kind}"
            store(img, pid)
            plans.append({"split": "train", "f": f, "step": s, "label": final, "image": img,
                          "aug": {k: v for k, v in plan.items()} | {"note": note},
                          "weak": truth.get(s) == "weak" and final == label, "id": pid})

    # 2. SAM on every derived image: production grounds the frame it is given.
    jobs = [(p["image"], steps[p["step"]]["target_phrase"]) for p in plans if p["aug"]]
    new = sum(1 for k in {cache.key(*j) for j in jobs} if k not in cache.data)
    print(f"{len(plans)} episodes planned; {new} new SAM calls (${new * config.PRICE_SAM_IMAGE:.2f}).")
    if args.plan_only:
        print(json.dumps({"dropped": dropped, "frame_step_pairs_by_truth": pair_truth}, indent=1))
        return
    segment_all(cache, jobs, budget_calls=budget_calls())
    if cache.errors:
        sys.exit("Stopping: some SAM calls failed, rerun to resume. Nothing was written.")

    # 3. Write. A removal that SAM still sees isn't a removal: drop it.
    OUT.mkdir(parents=True, exist_ok=True)
    files = {k: (open(OUT / f"{k}.jsonl", "w", encoding="utf-8"), open(OUT / f"{k}.meta.jsonl", "w", encoding="utf-8"))
             for k in ("train", "eval", "eval_degraded")}
    counts: dict[str, dict[str, int]] = {k: {} for k in files}
    for p in plans:
        step = steps[p["step"]]
        rows = cache.get(p["image"], step["target_phrase"])
        dets = to_dets(rows or [])
        label = p["label"]
        aug = p["aug"] or {}
        if aug.get("degraded") == "crop" or aug.get("note") == "target_cropped_out":
            if any((d.confidence or 0) >= 0.5 for d in dets):
                drop("removal_sam_still_finds_target")
                continue
        cap = capture_prompt(step, p["image"], dets, steps)
        degraded = aug.get("degraded") or ("crop" if aug.get("note") == "target_cropped_out" else None)
        call = answer(label, step, p["f"]["phase"], dets, degraded)
        row = to_row(cap, call, p["image"])
        data, meta = files[p["split"]]
        data.write(json.dumps(row, ensure_ascii=False) + "\n")
        meta.write(json.dumps({
            "id": p["id"], "frame": p["f"]["frame"], "t": p["f"]["t"], "phase": p["f"]["phase"], "step": p["step"],
            "label": label, "weak_label": p["weak"], "aug": aug, "sam_found": bool(dets),
            "grounding": cap["grounding"],
        }, ensure_ascii=False) + "\n")
        c = counts[p["split"]]
        key = label + ("/weak" if p["weak"] else "")
        c[key] = c.get(key, 0) + 1
    for data, meta in files.values():
        data.close()
        meta.close()
    cache.save()
    report = {
        "episodes": counts, "dropped": dropped, "frame_step_pairs_by_truth": pair_truth,
        "paid_sam_calls_this_run": cache.paid,
        "cost_this_run_usd": round(cache.paid * config.PRICE_SAM_IMAGE, 4),
        "sam_cache_entries": len(cache.data),
        "sam_cache_cost_usd": round(len(cache.data) * config.PRICE_SAM_IMAGE, 4),
        "grounding_on_base_frames": grounding_rates(
            cache, timeline, [s["target_phrase"] for s in steps] + [HAND_PHRASE]),
    }
    (OUT / "episodes_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report, indent=1))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("timeline")
    g = sub.add_parser("ground")
    g.add_argument("--limit", type=int, default=0, help="Only this many frames, spread across the clip.")
    sub.add_parser("check")
    b = sub.add_parser("build")
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--plan-only", action="store_true", help="Count episodes and SAM calls; send nothing.")
    args = p.parse_args()
    if shutil.which("ffmpeg") is None:
        sys.exit("ffmpeg is not on PATH.")
    {"timeline": cmd_timeline, "ground": cmd_ground, "check": cmd_check, "build": cmd_build}[args.cmd](args)


if __name__ == "__main__":
    main()
