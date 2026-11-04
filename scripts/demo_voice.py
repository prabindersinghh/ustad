"""Where each spoken line goes in an edited screen recording.

Headless Chromium's speech never reaches a recording, so the voice is added after,
from the text the app really spoke. The rules copy app.js: lines queue rather than
overlap (Voice.say), and Voice.stop() ends the current line and drops the queue.
Kept apart from the video code so it can be tested with no media.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any


@dataclass
class Spoken:
    file: Path
    start: float
    length: float
    speaker: str
    text: str
    source: str
    cut_short: bool = False

    def shifted(self, by: float) -> "Spoken":
        return replace(self, start=self.start + by)


def output_length(edl: list[tuple[float, ...]]) -> float:
    return sum(seg[2] if len(seg) == 3 else seg[1] - seg[0] for seg in edl)


def map_time(edl: list[tuple[float, ...]], t: float) -> float:
    """A moment in the recording, as a moment in the edit. A moment inside a cut
    lands on the first frame kept after it; one past the end, on the last frame."""
    out = 0.0
    for seg in edl:
        a, b = seg[0], seg[1]
        if t < a:
            return out
        if len(seg) == 2 and t < b:
            return out + (t - a)
        out += seg[2] if len(seg) == 3 else b - a
    return out


def place_queue(events: list[dict[str, Any]], edl: list[tuple[float, ...]], speaker: str, length: float) -> list[Spoken]:
    """Run the app's speech queue on the edit's clock. `events` are in recording
    time: {"kind": "speak", at, file, length, text, source} or {"kind": "cancel", at}.
    A line the edit has no time left for is cut off at the end of the part."""
    placed: list[Spoken] = []
    free_at = 0.0
    for e in sorted(events, key=lambda x: x["at"]):
        at = map_time(edl, e["at"])
        if e["kind"] == "cancel":
            kept = [s for s in placed if s.start < at]
            if kept and kept[-1].start + kept[-1].length > at:
                kept[-1] = replace(kept[-1], length=at - kept[-1].start, cut_short=True)
            placed = kept
            free_at = at
            continue
        start = max(at, free_at)
        placed.append(Spoken(e["file"], start, e["length"], speaker, e["text"], e["source"]))
        free_at = start + e["length"]
    out = []
    for s in placed:
        if s.start >= length:
            continue
        if s.start + s.length > length:
            s = replace(s, length=length - s.start, cut_short=True)
        out.append(s)
    return out
