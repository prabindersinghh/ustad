"""Parser for the SAM 3.1 output token grammar.

SAM 3.1 returns results as text, one line per image (or per video frame),
with records that look like:

    <0f>0<|box;x1=181;y1=365;x2=556;y2=486;w=1516;h=1600|><|mask;x=0;y=0;data=...|>

We deliberately parse boxes and confidence only. Masks are expensive to
decode and buy us nothing for step verification -- a box plus a confidence
score is enough to tell Muse Spark *where* the target part is, which is the
whole point of grounding the reasoning in real pixels.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any

# <0f> frame marker, then an object ordinal, then the box token.
_FRAME_RE = re.compile(r"<(\d+)f>")
_BOX_RE = re.compile(r"<\|box;([^|]*)\|>")


@dataclass
class Detection:
    frame: int
    ordinal: int
    x1: int
    y1: int
    x2: int
    y2: int
    img_w: int
    img_h: int
    confidence: float | None = None

    @property
    def area_fraction(self) -> float:
        """How much of the frame this detection covers (0-1)."""
        if not self.img_w or not self.img_h:
            return 0.0
        return ((self.x2 - self.x1 + 1) * (self.y2 - self.y1 + 1)) / (
            self.img_w * self.img_h
        )

    @property
    def center(self) -> tuple[float, float]:
        """Centre point normalised to 0-1, origin top-left."""
        if not self.img_w or not self.img_h:
            return (0.0, 0.0)
        return (
            ((self.x1 + self.x2) / 2) / self.img_w,
            ((self.y1 + self.y2) / 2) / self.img_h,
        )

    def describe(self) -> str:
        """Plain-language position, for feeding to the reasoning model."""
        cx, cy = self.center
        horiz = "left" if cx < 0.37 else "right" if cx > 0.63 else "centre"
        vert = "top" if cy < 0.37 else "bottom" if cy > 0.63 else "middle"
        where = "centre of frame" if (horiz, vert) == ("centre", "middle") else f"{vert}-{horiz}"
        conf = f", confidence {self.confidence:.2f}" if self.confidence is not None else ""
        return f"{where}, covering {self.area_fraction * 100:.1f}% of the frame{conf}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["center"] = self.center
        d["area_fraction"] = round(self.area_fraction, 4)
        return d


def _kv(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in blob.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def parse(output_text: str) -> list[Detection]:
    """Turn raw SAM output_text into Detection records.

    Returns an empty list when the concept was not found, which SAM signals
    with an empty output_text. That is a normal result, not an error.
    """
    if not output_text or not output_text.strip():
        return []

    detections: list[Detection] = []
    current_frame = 0
    cursor = 0

    # Walk the string, tracking the most recent frame marker and the integer
    # ordinal that precedes each box token.
    for box_match in _BOX_RE.finditer(output_text):
        segment = output_text[cursor:box_match.start()]
        for fm in _FRAME_RE.finditer(segment):
            current_frame = int(fm.group(1))

        # The object ordinal is the trailing integer just before the box token.
        tail = _FRAME_RE.sub("", segment).strip().rstrip(",").strip()
        ordinal_match = re.search(r"(\d+)\s*$", tail)
        ordinal = int(ordinal_match.group(1)) if ordinal_match else len(detections)

        fields = _kv(box_match.group(1))
        try:
            det = Detection(
                frame=current_frame,
                ordinal=ordinal,
                x1=int(float(fields.get("x1", 0))),
                y1=int(float(fields.get("y1", 0))),
                x2=int(float(fields.get("x2", 0))),
                y2=int(float(fields.get("y2", 0))),
                img_w=int(float(fields.get("w", 0))),
                img_h=int(float(fields.get("h", 0))),
                confidence=float(fields["c"]) if "c" in fields else None,
            )
        except (TypeError, ValueError):
            continue

        detections.append(det)
        cursor = box_match.end()

    return detections


def summarise(phrase: str, detections: list[Detection], limit: int = 4) -> str:
    """One short paragraph describing what SAM found, for the reasoning prompt."""
    if not detections:
        return f'Grounding: searched the frame for "{phrase}" and found no match.'

    ranked = sorted(
        detections,
        key=lambda d: (d.confidence if d.confidence is not None else 0.0, d.area_fraction),
        reverse=True,
    )[:limit]

    lines = [
        f'Grounding: searched the frame for "{phrase}" and found '
        f"{len(detections)} match{'es' if len(detections) != 1 else ''}."
    ]
    for i, det in enumerate(ranked, 1):
        lines.append(f"  {i}. {det.describe()}")
    return "\n".join(lines)
