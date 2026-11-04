#!/usr/bin/env python3
"""Build a procedure from a video file, through the real capture code.

Frames are sampled at the rate a phone sends keyframes, then go through the same
`procedures.build_procedure` a live take uses: the writer, then the SAM self-check.

    python -m scripts.capture_video --video factory.mp4 --end 16 --narration-file narration.txt
    python -m scripts.capture_video --video factory.mp4 --end 16 --narration-file voiceover.m4a
    python -m scripts.capture_video --video job.mp4

--narration-file replaces the video's own sound, which is what a recording with
only machine noise needs. A .txt is used as written and costs no ASR ("[0:05]
words" lines let the writer match words to frames). An audio file such as .m4a is
transcribed. Without the option, the video's own audio is transcribed.
These calls cost money. Footage goes to Meta's hosted API only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from backend import config, db, live, meta_client, procedures
from scripts.replay import ReplayError, _require_hosted, decode_frame, resolve_video, spend_of_this_process

AUDIO_SUFFIXES = {".m4a", ".mp3", ".wav", ".aac", ".mp4", ".webm", ".ogg"}


def read_narration(path: Path) -> str:
    """The expert's words from a transcript, or from a separate voiceover."""
    if not path.is_file():
        raise ReplayError(f"Narration file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".txt":
        text = path.read_text(encoding="utf-8").strip()
    elif suffix in AUDIO_SUFFIXES:
        text = meta_client.transcribe(path.read_bytes(), filename=path.name)
    else:
        raise ReplayError(f"Narration must be a .txt transcript or an audio file, not {suffix or path.name}")
    if not text:
        raise ReplayError(f"No words in narration file: {path}")
    return text


def video_narration(video: Path) -> str:
    """The video's own sound, for the case with no narration file. No words is
    not an error: a capture can be written from the frames alone."""
    return meta_client.transcribe(video.read_bytes(), filename=video.name)


def sample_times(start: float, end: float, every: float) -> list[float]:
    if end <= start or every <= 0:
        raise ReplayError(f"Nothing to sample between {start:g} and {end:g} s")
    count = int((end - start) / every) + 1
    return [round(start + i * every, 3) for i in range(count)]


def duration_of(video: Path) -> float:
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ReplayError("ffprobe not found")
    out = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, timeout=30)
    try:
        return float(out.stdout.strip())
    except ValueError as exc:
        raise ReplayError(f"Could not read the length of {video}") from exc


def capture(video: Path, *, start: float = 0.0, end: float | None = None, narration_file: Path | None = None,
            every: float = config.CAPTURE_FRAME_INTERVAL_S, hint: str | None = None) -> dict[str, Any]:
    """Saves the procedure and its reference photos, as the end of a live take does."""
    _require_hosted()
    db.init()
    stop = duration_of(video) if end is None else end
    times = sample_times(start, stop, every)
    frames = [decode_frame(video, t) for t in times]
    transcript = read_narration(narration_file) if narration_file else video_narration(video)
    proc = procedures.build_procedure(transcript, frames, hint=hint,
                                      frame_times=[t - start for t in times])
    tag = "file_" + db.new_id()
    live.save_reference_photos(proc, tag)
    pid = db.save_procedure(proc["title"], proc["steps"], machine=proc.get("machine"), transcript=transcript)
    return {"procedure_id": pid, "procedure": proc, "frames": len(frames), "transcript": transcript}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build a procedure from a video file with the real capture code.")
    ap.add_argument("--video", required=True, help="a path, or a file name in data/factory/")
    ap.add_argument("--start", type=float, default=0.0, help="seconds into the video")
    ap.add_argument("--end", type=float, help="seconds into the video; default the end")
    ap.add_argument("--narration-file", type=Path, help=".txt transcript or audio voiceover; skips the video's audio")
    ap.add_argument("--hint", help="context from the supervisor, as on a live take")
    args = ap.parse_args(argv)
    try:
        with spend_of_this_process() as spent:
            out = capture(resolve_video(args.video), start=args.start, end=args.end,
                          narration_file=args.narration_file, hint=args.hint)
    except ReplayError as exc:
        print(f"capture_video: {exc}", file=sys.stderr)
        return 2
    proc = out["procedure"]
    print(f"Procedure {out['procedure_id']}: {proc.get('title')} ({len(proc['steps'])} steps, {out['frames']} frames)")
    for s in proc["steps"]:
        print(f"  {s['index'] + 1}. {s['title']} | target: {s.get('target_phrase')} | check: {s.get('check')}")
    print("Self-check:", json.dumps(proc.get("_validation") or {}))
    print(f"Spent by this run: ${spent['usd']:.4f} over {int(spent['calls'])} metered calls")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
