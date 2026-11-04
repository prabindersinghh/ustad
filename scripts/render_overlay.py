#!/usr/bin/env python3
"""Draw a replay session's real results over its video. Nothing is invented: every
box, verdict, reason and backend label is read from the database.

    python -m scripts.render_overlay --video factory.mp4 --session <id> --out overlay_guide.mp4

Which box: only the highest-confidence detection of the step's phrase, the rule
the app's own live view uses (LiveBox.loop in web/app.js). A step with no detection
gets no box and says so. When: the camera moves, so a box shows only within
--box-window seconds of its checkpoint and never across a cut in the video.
The verdict badge holds for --hold seconds. Nothing is drawn inside --no-draw ranges
(a full-frame title card); factory.mp4 has one by default. Keeps the source size,
frame rate and sound. A bare --out name goes to data/factory/replay/, out of git.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend import db
from scripts.replay import ReplayError, best_detection, probe_size, resolve_video, to_source_box

REPLAY_DIR = Path("data") / "factory" / "replay"
KNOWN_NO_DRAW = {"factory.mp4": [(16.1, 19.13)]}      # the title card in the user's clip

# Colours from web/app.js: the target box, and the verdict colours of LiveBox.flash.
TARGET = (59, 130, 255)
VERDICTS = {"pass": ("VERIFIED", (18, 217, 138)), "fail": ("ERROR CAUGHT", (255, 90, 95)),
            "retry": ("NEED BETTER VIEW", (255, 176, 32))}
FONT, FONT_BOLD = "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/seguisb.ttf"


@dataclass
class Overlay:
    """What belongs on screen at one moment."""
    step: dict[str, Any] | None = None
    box: tuple[float, float, float, float] | None = None
    box_label: str | None = None
    no_box_note: str | None = None
    verdict: str | None = None
    reason: str | None = None
    backend: str | None = None


def clean_text(text: Any) -> str:
    """One line, with no em or en dashes in anything drawn into the video."""
    return re.sub(r"\s*[\u2014\u2013]\s*", ", ", " ".join(str(text or "").split()))


def shot_of(t: float, cuts: list[float]) -> int:
    return sum(1 for c in cuts if c <= t)


def overlay_at(t: float, points: list[dict[str, Any]], steps: list[dict[str, Any]], *, cuts: list[float],
               no_draw: list[tuple[float, float]], box_window: float = 0.5, hold: float = 2.0) -> Overlay:
    """Pure: what to draw at `t` seconds, given the rows from the database."""
    if any(a <= t < b for a, b in no_draw) or not points:
        return Overlay()
    # A step stays current until its own verdict has finished showing.
    current = next((p for p in points if t < p["checkpoint_s"] + hold), None)
    if current is None:
        return Overlay(backend=points[-1].get("backend"))
    out = Overlay(step=steps[current["step_index"]] if current["step_index"] < len(steps) else None,
                  backend=current.get("backend"))
    at = current["checkpoint_s"]
    if at <= t < at + hold:
        out.verdict, out.reason = current.get("verdict"), clean_text(current.get("reason"))
    near = next((p for p in points if abs(t - p["checkpoint_s"]) <= box_window
                 and shot_of(t, cuts) == shot_of(p["checkpoint_s"], cuts)), None)
    if near is not None:
        best = best_detection(near.get("detections") or [])
        if best is not None:
            out.box = to_source_box(best, near["video_w"], near["video_h"])
            score = f" {best['confidence']:.2f}" if best.get("confidence") is not None else ""
            out.box_label = f"{near.get('phrase')}{score}"
        elif near.get("sam_error"):
            out.no_box_note = "SAM call failed: no box"
        else:
            out.no_box_note = f'SAM did not find "{near.get("phrase")}": no box'
    return out


def wrap(draw: Any, text: str, font: Any, width: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        probe = f"{line} {word}".strip()
        if draw.textlength(probe, font=font) <= width:
            line = probe
        else:
            lines.append(line)
            line = word
    return lines + [line] if line else lines


def draw_overlay(image: Any, o: Overlay) -> None:
    from PIL import Image, ImageDraw, ImageFont

    w, h = image.size
    unit = w / 480
    small, body, head = (ImageFont.truetype(FONT, round(s * unit)) for s in (15, 17, 15))
    bold = ImageFont.truetype(FONT_BOLD, round(19 * unit))
    badge_font = ImageFont.truetype(FONT_BOLD, round(23 * unit))
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pad = round(12 * unit)

    if o.box:
        d.rounded_rectangle(o.box, radius=round(8 * unit), outline=TARGET + (255,), width=max(2, round(3 * unit)),
                            fill=TARGET + (34,))
        tw = d.textlength(o.box_label or "", font=small)
        lx = min(max(4, o.box[0]), w - tw - 20 * unit)
        ly = o.box[1] - 26 * unit if o.box[1] > 30 * unit else o.box[3] + 4 * unit
        d.rounded_rectangle((lx, ly, lx + tw + 16 * unit, ly + 23 * unit), radius=round(11 * unit), fill=TARGET + (255,))
        d.text((lx + 8 * unit, ly + 2 * unit), o.box_label or "", font=small, fill=(255, 255, 255, 255))

    y = pad
    if o.verdict in VERDICTS:
        label, colour = VERDICTS[o.verdict]
        bw = d.textlength(label, font=badge_font) + 28 * unit
        d.rounded_rectangle((pad, y, pad + bw, y + 40 * unit), radius=round(20 * unit), fill=colour + (255,))
        d.text((pad + 14 * unit, y + 4 * unit), label, font=badge_font, fill=(10, 16, 32, 255))
        y += round(46 * unit)
        lines = wrap(d, o.reason or "", small, w - 2 * pad - 16 * unit)[:3]
        if lines:
            d.rounded_rectangle((pad, y, w - pad, y + (len(lines) * 20 + 10) * unit), radius=round(10 * unit), fill=(10, 16, 32, 215))
            for i, line in enumerate(lines):
                d.text((pad + 8 * unit, y + (4 + i * 20) * unit), line, font=small, fill=(255, 255, 255, 255))
            y += round((len(lines) * 20 + 16) * unit)
    if o.no_box_note:
        tw = d.textlength(o.no_box_note, font=small)
        d.rounded_rectangle((pad, y, pad + tw + 16 * unit, y + 24 * unit), radius=round(10 * unit), fill=(10, 16, 32, 215))
        d.text((pad + 8 * unit, y + 2 * unit), o.no_box_note, font=small, fill=(255, 197, 61, 255))

    if o.step:
        title = clean_text(f"Step {o.step.get('index', 0) + 1}: {o.step.get('title', '')}")
        lines = wrap(d, clean_text(o.step.get("instruction")), body, w - 2 * pad)[:3]
        block = (28 + len(lines) * 23 + 14) * unit
        d.rectangle((0, h - block, w, h), fill=(10, 16, 32, 205))
        d.text((pad, h - block + 6 * unit), title, font=bold, fill=(255, 255, 255, 255))
        for i, line in enumerate(lines):
            d.text((pad, h - block + (32 + i * 23) * unit), line, font=body, fill=(226, 232, 245, 255))

    if o.backend:
        label = f"judged on: {clean_text(o.backend)}"
        tw = d.textlength(label, font=head)
        d.rounded_rectangle((w - tw - 22 * unit, 8 * unit, w - 6 * unit, 32 * unit), radius=round(10 * unit), fill=(10, 16, 32, 190))
        d.text((w - tw - 14 * unit, 10 * unit), label, font=head, fill=(255, 255, 255, 255))
    image.paste(layer, (0, 0), layer)


def cuts_from_diffs(diffs: list[float], fps: float) -> list[float]:
    """Times of frames far more different from the last one than ordinary motion.
    diffs[i] compares frame i + 1 with frame i. Measured on factory.mp4: a cut scores
    about 31, ordinary motion about 2."""
    if not diffs:
        return []
    usual = sorted(diffs)[len(diffs) // 2]
    return [(i + 1) / fps for i, v in enumerate(diffs) if v >= max(12.0, 6 * usual)]


def detect_cuts(video: Path, fps: float) -> list[float]:
    """Hard cuts in the source, so a box is never carried across one. ffmpeg's scene
    score misses a cut between two near-identical views, which this clip has."""
    from PIL import Image, ImageChops, ImageStat

    w, h = 120, 188
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf", f"scale={w}:{h}",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"], capture_output=True, timeout=300).stdout
    size = w * h
    frames = [Image.frombytes("L", (w, h), raw[i:i + size]) for i in range(0, len(raw) - size + 1, size)]
    return cuts_from_diffs([ImageStat.Stat(ImageChops.difference(frames[i], frames[i + 1])).mean[0]
                            for i in range(len(frames) - 1)], fps)


def video_rate(video: Path) -> str:
    return subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=r_frame_rate",
                           "-of", "csv=p=0", str(video)], capture_output=True, text=True, timeout=30).stdout.strip()


def resolve_out(name: str) -> Path:
    path = Path(name)
    if path.parent == Path("."):
        path = REPLAY_DIR / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def render(video: Path, session: str, out: Path, *, no_draw: list[tuple[float, float]], box_window: float, hold: float) -> dict[str, Any]:
    from PIL import Image

    log = db.session_log(session)
    points = db.replay_checkpoints(session)
    if not log["session"] or not points:
        raise ReplayError(f"Session {session} has no replay checkpoints to draw")
    proc = db.get_procedure(log["session"]["procedure_id"])
    steps = proc["steps"] if proc else []
    w, h = probe_size(video)
    rate = video_rate(video)
    num, _, den = rate.partition("/")
    fps = float(num) / float(den or 1)
    cuts = detect_cuts(video, fps)

    reader = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                              stdout=subprocess.PIPE)
    writer = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                               "-r", rate, "-i", "-", "-i", str(video), "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-preset", "veryfast",
                               "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", str(out)], stdin=subprocess.PIPE)
    size, n, drawn = w * h * 3, 0, 0
    while True:
        raw = reader.stdout.read(size)
        if len(raw) < size:
            break
        o = overlay_at(n / fps, points, steps, cuts=cuts, no_draw=no_draw, box_window=box_window, hold=hold)
        if o.step or o.box or o.verdict or o.backend:
            frame = Image.frombytes("RGB", (w, h), raw)
            draw_overlay(frame, o)
            raw = frame.tobytes()
            drawn += 1
        writer.stdin.write(raw)
        n += 1
    writer.stdin.close()
    if reader.wait() != 0 or writer.wait() != 0:
        raise ReplayError("ffmpeg failed while rendering")
    return {"out": out, "frames": n, "drawn": drawn, "cuts": cuts, "points": len(points)}


def parse_ranges(text: str) -> list[tuple[float, float]]:
    out = []
    for part in filter(None, text.split(",")):
        a, _, b = part.partition("-")
        out.append((float(a), float(b)))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Draw a replay session's real results over its video.")
    ap.add_argument("--video", required=True)
    ap.add_argument("--session", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-draw", help="seconds with nothing drawn, like 16.1-19.1,30-31; default per known video")
    ap.add_argument("--box-window", type=float, default=0.5)
    ap.add_argument("--hold", type=float, default=2.0)
    args = ap.parse_args(argv)
    if not shutil.which("ffmpeg"):
        print("render_overlay: ffmpeg not found", file=sys.stderr)
        return 2
    try:
        video = resolve_video(args.video)
        no_draw = parse_ranges(args.no_draw) if args.no_draw is not None else KNOWN_NO_DRAW.get(video.name, [])
        done = render(video, args.session, resolve_out(args.out), no_draw=no_draw, box_window=args.box_window, hold=args.hold)
    except ReplayError as exc:
        print(f"render_overlay: {exc}", file=sys.stderr)
        return 2
    print(f"{done['out']}: {done['frames']} frames, {done['drawn']} with an overlay, "
          f"{done['points']} checkpoints, cuts at {[round(c, 2) for c in done['cuts']]}, nothing drawn in {no_draw}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
