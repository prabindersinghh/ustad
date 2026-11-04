#!/usr/bin/env python3
"""Feed a video file through the real guide pipeline instead of a live camera.

Frames are decoded at the given timestamps and each goes to the existing
`guide.verify_step()`: the real SAM 3.1 call, the real Muse Spark tool call, no
mocking and no parallel code path. Every verdict lands in the database as a
genuine session, named `replay: <file>` so nobody mistakes it for a live person.
`replay_checkpoints` keeps the video time and every box for the overlay renderer.

    python -m scripts.replay --video factory.mp4 --procedure <id> --checkpoints 6,14,22,30,38
    python -m scripts.replay --video factory.mp4 --procedure <id> --checkpoints 6,14,22,30,38 --upscale 2

Is SAM any good at this width? Grounds every step's phrase on every checkpoint,
at native width and upscaled, and draws the boxes to look at. Writes no session:

    python -m scripts.replay --video factory.mp4 --procedure <id> --checkpoints 6,14,22,30,38 \\
        --compare-upscale 2 --out-dir data/factory/replay/sam_compare

Checkpoints map to steps in order. The worker says nothing, so `worker_said`
stays empty. These calls cost money: the spend of this process is printed.
Footage goes to Meta's hosted API only, so this refuses to run if reasoning is
pointed at an open-weights endpoint.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from backend import config, db, guide, meta_client

FACTORY_DIR = Path("data") / "factory"
JPEG_QSCALE = "3"                     # ffmpeg -q:v, 2 is best; the live app sends canvas quality 0.7


class ReplayError(RuntimeError):
    """Something a person can fix: a bad path, timestamp or configuration."""


def parse_checkpoints(text: str) -> list[float]:
    try:
        points = [float(p) for p in text.split(",") if p.strip()]
    except ValueError as exc:
        raise ReplayError(f"Checkpoints must be seconds, comma separated: {text!r}") from exc
    if not points or any(p < 0 for p in points) or points != sorted(set(points)):
        raise ReplayError(f"Checkpoints must be non-negative and strictly increasing: {text!r}")
    return points


def resolve_video(name: str) -> Path:
    """A path as given, else the same name in the factory footage folder."""
    for path in (Path(name), FACTORY_DIR / name):
        if path.is_file():
            return path
    raise ReplayError(f"Video not found: {name}")


def _ffmpeg() -> str:
    found = config.FFMPEG or shutil.which("ffmpeg")
    if not found:
        raise ReplayError("ffmpeg not found: install it or set FFMPEG in .env")
    return found


def probe_size(video: Path) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ReplayError("ffprobe not found")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
         "-of", "csv=p=0", str(video)], capture_output=True, text=True, timeout=30)
    try:
        w, h = out.stdout.strip().split(",")[:2]
        return int(w), int(h)
    except ValueError as exc:
        raise ReplayError(f"Could not read the video size of {video}") from exc


def decode_frame(video: Path, at_s: float, upscale: float = 1) -> bytes:
    """One JPEG at `at_s` seconds, optionally upscaled. Raises if past the end."""
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{at_s:.3f}", "-i", str(video),
           "-frames:v", "1"]
    if upscale != 1:
        cmd += ["-vf", f"scale=trunc(iw*{upscale:g}):trunc(ih*{upscale:g}):flags=lanczos"]
    cmd += ["-q:v", JPEG_QSCALE, "-f", "image2pipe", "-c:v", "mjpeg", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=60)
    if proc.returncode != 0 or not proc.stdout:
        raise ReplayError(f"No frame at {at_s:g} s of {video}: {proc.stderr.decode(errors='ignore')[:200]}")
    return proc.stdout


def to_source_box(det: dict[str, Any], video_w: int, video_h: int) -> tuple[float, float, float, float]:
    """A SAM box in source video pixels. SAM reports the image size it measured
    against, so an upscaled frame's box is divided back by that size."""
    w, h = det.get("img_w") or video_w, det.get("img_h") or video_h
    sx, sy = video_w / w, video_h / h
    return (det["x1"] * sx, det["y1"] * sy, det["x2"] * sx, det["y2"] * sy)


def best_detection(dets: list[dict[str, Any]]) -> dict[str, Any] | None:
    return max(dets, key=lambda d: d.get("confidence") or 0.0, default=None)


@contextlib.contextmanager
def watch_sam_errors() -> Iterator[list[str]]:
    """`guide.ground` turns a failed SAM call into "no match", which is right for
    a live worker and wrong for a measurement. Collect those so they can be
    reported as errors instead of counted as SAM not finding the part."""
    seen: list[str] = []

    class Catch(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.getMessage().startswith("Grounding failed"):
                seen.append(record.getMessage())

    handler, logger = Catch(level=logging.WARNING), logging.getLogger("ustad.guide")
    logger.addHandler(handler)
    try:
        yield seen
    finally:
        logger.removeHandler(handler)


@contextlib.contextmanager
def spend_of_this_process() -> Iterator[dict[str, float]]:
    """Dollars metered by this process alone. The usage table's total also moves
    with anything else using the same database, such as the running server."""
    tally = {"usd": 0.0, "calls": 0.0}
    real = db.log_usage

    def counted(model: str, kind: str, units: float, cost: float, note: str | None = None) -> None:
        tally["usd"] += cost
        tally["calls"] += 1
        real(model, kind, units, cost, note)

    db.log_usage = counted  # type: ignore[assignment]
    try:
        yield tally
    finally:
        db.log_usage = real  # type: ignore[assignment]


def _require_hosted() -> None:
    if meta_client.active_backend() != "hosted":
        raise ReplayError("Reasoning is not on the hosted Meta API. Factory footage may only go to Meta: "
                          "set REASONING_BACKEND=hosted for this run.")


def _cell(text: Any) -> str:
    return " ".join(str(text if text is not None else "-").split()).replace("|", "\\|")


def describe_sam(row: dict[str, Any]) -> str:
    if row.get("sam_error"):
        return "SAM ERROR (not a miss)"
    dets = row.get("detections") or []
    best = best_detection(dets)
    if best is None:
        return "not found"
    score = f"{best['confidence']:.2f}" if best.get("confidence") is not None else "no score"
    box = to_source_box(best, row["video_w"], row["video_h"])
    return f"{len(dets)} found, best {score} at ({box[0]:.0f},{box[1]:.0f})-({box[2]:.0f},{box[3]:.0f})"


def results_table(rows: list[dict[str, Any]]) -> str:
    """The run as markdown. Boxes are in source video pixels whatever the upscale."""
    lines = ["| Checkpoint | Step | Verdict | Confidence | SAM (phrase: detection, score, box) | Latency | Backend | Reason |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        conf = f"{r['confidence']:.2f}" if isinstance(r.get("confidence"), (int, float)) else "-"
        lines.append("| " + " | ".join(_cell(c) for c in (
            f"{r['checkpoint_s']:g} s", f"{r['step_index'] + 1}. {r.get('title', '')}", r.get("verdict"), conf,
            f"{r.get('phrase')}: {describe_sam(r)}", f"{(r.get('latency_ms') or 0) / 1000:.1f} s",
            r.get("backend"), r.get("reason"))) + " |")
    return "\n".join(lines)


def run_replay(video: Path, procedure_id: str, checkpoints: list[float], *, upscale: float = 1) -> dict[str, Any]:
    """One genuine session from a video. Frames are decoded before anything is
    written, so a bad timestamp can't leave half a session behind."""
    _require_hosted()
    db.init()
    proc = db.get_procedure(procedure_id)
    if not proc:
        raise ReplayError(f"Unknown procedure: {procedure_id}")
    steps = proc["steps"]
    if len(checkpoints) > len(steps):
        raise ReplayError(f"{len(checkpoints)} checkpoints but only {len(steps)} steps")
    video_w, video_h = probe_size(video)
    frames = [decode_frame(video, t, upscale) for t in checkpoints]
    frame_w, frame_h = int(video_w * upscale), int(video_h * upscale)

    worker = f"replay: {video.name}" + (f" ({upscale:g}x)" if upscale != 1 else "")
    sid = db.start_session(proc["id"], worker, "guide", version=proc.get("version"))
    rows: list[dict[str, Any]] = []
    outcome = "replay: stopped by an error"
    try:
        for i, (at_s, frame) in enumerate(zip(checkpoints, frames)):
            step = steps[i]
            with watch_sam_errors() as sam_errors:
                action = guide.verify_step(session_id=sid, step=step, frame=frame, upcoming=steps[i + 1: i + 4])
            if action.get("backend") != "hosted":
                raise ReplayError(f"Check answered by {action.get('backend')}, not the hosted Meta API")
            event = db.session_log(sid)["events"][-1]
            row = {
                "checkpoint_s": at_s, "event_id": event["id"], "step_index": step.get("index", i), "video": video.name,
                "phrase": step.get("target_phrase"), "tool": action["tool"], "say": action.get("say"),
                "upscale": upscale, "video_w": video_w, "video_h": video_h, "frame_w": frame_w, "frame_h": frame_h,
                "detections": action.get("detections") or [], "sam_error": bool(sam_errors),
            }
            db.log_replay_checkpoint(sid, row)
            rows.append({**row, "title": step.get("title"), "verdict": event["verdict"],
                         "confidence": event["confidence"], "reason": event["reason"],
                         "latency_ms": event["latency_ms"], "backend": event["backend"],
                         "completed_through": action["detail"].get("completed_through")})
        passed = sum(1 for r in rows if r["verdict"] == "pass")
        # Never "completed": nobody did this job. The pill says what happened.
        outcome = f"replay: {passed} of {len(rows)} verified"
    finally:
        db.finish_session(sid, outcome)
    return {"session_id": sid, "worker": worker, "outcome": outcome, "rows": rows}


# ---------------------------------------------------------- native vs upscaled

def compare_upscale(video: Path, procedure_id: str, checkpoints: list[float], upscale: float, out_dir: Path) -> list[dict[str, Any]]:
    """Every step's phrase on every checkpoint, at native width and upscaled,
    through `guide.ground`. Boxes are drawn because a confident box on the wrong
    object is the failure a score can't show."""
    _require_hosted()
    db.init()
    proc = db.get_procedure(procedure_id)
    if not proc:
        raise ReplayError(f"Unknown procedure: {procedure_id}")
    video_w, video_h = probe_size(video)
    natives = {t: decode_frame(video, t) for t in checkpoints}
    uppeds = {t: decode_frame(video, t, upscale) for t in checkpoints}
    results: list[dict[str, Any]] = []
    for step in proc["steps"]:
        for t in checkpoints:
            for scale, frame in ((1.0, natives[t]), (upscale, uppeds[t])):
                started = time.perf_counter()
                with watch_sam_errors() as sam_errors:
                    dets = [d.to_dict() for d in guide.ground(step, frame)]
                results.append({"checkpoint_s": t, "step_index": step["index"], "phrase": step["target_phrase"],
                                "upscale": scale, "sam_ms": int((time.perf_counter() - started) * 1000),
                                "sent_w": int(video_w * scale), "sent_h": int(video_h * scale),
                                "video_w": video_w, "video_h": video_h,
                                "detections": dets, "sam_error": bool(sam_errors)})
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "compare.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    _draw_comparison(results, natives, checkpoints, upscale, out_dir)
    return results


def _draw_comparison(results: list[dict[str, Any]], natives: dict[float, bytes], checkpoints: list[float],
                     upscale: float, out_dir: Path) -> None:
    """One sheet per phrase: checkpoints across, native on top, upscaled below,
    all boxes mapped to source pixels and drawn on the same native frame."""
    import io

    from PIL import Image, ImageDraw

    frames = {t: Image.open(io.BytesIO(b)).convert("RGB") for t, b in natives.items()}
    w, h = next(iter(frames.values())).size
    gap, head = 8, 22
    for phrase in dict.fromkeys(r["phrase"] for r in results):
        sheet = Image.new("RGB", ((w + gap) * len(checkpoints), (h + head) * 2), "white")
        pen = ImageDraw.Draw(sheet)
        for col, t in enumerate(checkpoints):
            for line, scale in enumerate((1.0, upscale)):
                x0, y0 = col * (w + gap), line * (h + head)
                r = next(r for r in results if r["phrase"] == phrase and r["checkpoint_s"] == t and r["upscale"] == scale)
                note = "SAM ERROR" if r["sam_error"] else f"{len(r['detections'])} found"
                pen.text((x0 + 4, y0 + 5), f"{phrase} | {t:g}s | {scale:g}x | {note}", fill="black")
                sheet.paste(frames[t], (x0, y0 + head))
                for d in r["detections"]:
                    a, b, c, e = to_source_box(d, r["video_w"], r["video_h"])
                    pen.rectangle((x0 + a, y0 + head + b, x0 + c, y0 + head + e), outline=(255, 0, 255), width=3)
                    score = f"{d['confidence']:.2f}" if d.get("confidence") is not None else "?"
                    pen.rectangle((x0 + a, y0 + head + b, x0 + a + 30, y0 + head + b + 13), fill=(255, 0, 255))
                    pen.text((x0 + a + 2, y0 + head + b + 1), score, fill="white")
        sheet.save(out_dir / f"sheet_{phrase.replace(' ', '_')}.jpg", quality=88)


def comparison_table(results: list[dict[str, Any]], upscale: float) -> str:
    def cell(r: dict[str, Any]) -> str:
        if r["sam_error"]:
            return "ERROR"
        best = best_detection(r["detections"])
        if best is None:
            return "not found"
        score = f"{best['confidence']:.2f}" if best.get("confidence") is not None else "?"
        return f"{len(r['detections'])}, best {score}"

    lines = [f"| Phrase | Checkpoint | Native: found, score | {upscale:g}x: found, score | SAM's reported size at {upscale:g}x |",
             "|---|---|---|---|---|"]
    for r in results:
        if r["upscale"] != 1.0:
            continue
        up = next(u for u in results if u["upscale"] == upscale and u["phrase"] == r["phrase"]
                  and u["checkpoint_s"] == r["checkpoint_s"])
        size = next(((d["img_w"], d["img_h"]) for d in up["detections"]), None)
        lines.append(f"| {r['phrase']} | {r['checkpoint_s']:g} s | {cell(r)} | {cell(up)} | "
                     f"{f'{size[0]}x{size[1]}' if size else '-'} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run a video file through the real guide pipeline.")
    ap.add_argument("--video", required=True, help="a path, or a file name in data/factory/")
    ap.add_argument("--procedure", required=True)
    ap.add_argument("--checkpoints", required=True, help="seconds into the video, comma separated, one per step")
    ap.add_argument("--upscale", type=float, default=1, help="enlarge each frame before the models see it")
    ap.add_argument("--compare-upscale", type=float, help="measure SAM at native width vs this factor; no session")
    ap.add_argument("--out-dir", default=str(FACTORY_DIR / "replay" / "sam_compare"), help="for --compare-upscale")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.ERROR)
    try:
        if args.upscale < 1 or (args.compare_upscale is not None and args.compare_upscale <= 1):
            raise ReplayError("Upscale must be at least 1")
        video, points = resolve_video(args.video), parse_checkpoints(args.checkpoints)
        with spend_of_this_process() as spent:
            if args.compare_upscale:
                results = compare_upscale(video, args.procedure, points, args.compare_upscale, Path(args.out_dir))
                print(comparison_table(results, args.compare_upscale))
                print(f"\nSheets and raw results: {args.out_dir}")
            else:
                run = run_replay(video, args.procedure, points, upscale=args.upscale)
                print(results_table(run["rows"]))
                print(f"\nSession: {run['session_id']} | worker: {run['worker']} | outcome: {run['outcome']}")
                print(f"Evidence: /evidence?s={run['session_id']}")
    except ReplayError as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 2
    print(f"Spent by this run: ${spent['usd']:.4f} over {int(spent['calls'])} metered calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
