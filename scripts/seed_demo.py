#!/usr/bin/env python3
"""Seed the database so a demo never opens on an empty screen.

By default this inserts ONE hand-authored procedure with concrete target
phrases and visual success criteria -- the kind capture mode produces when it
works -- so the library, guide mode and dashboard all have something to show
before any recording is made.

    .venv/Scripts/python.exe -m scripts.seed_demo            # one procedure
    .venv/Scripts/python.exe -m scripts.seed_demo --with-demo-session
    .venv/Scripts/python.exe -m scripts.seed_demo --reset    # wipe sessions+events

The procedure is legitimately authored content for the event. The optional
--with-demo-session inserts a SYNTHETIC session with placeholder evidence images
purely so the dashboard's session view can be checked offline; it taints
/api/metrics, so never run it before capturing real demo numbers. --reset clears
sessions and step events (not procedures) to get back to clean metrics.
"""
from __future__ import annotations

import argparse
import struct
import sys
import time
import zlib

sys.path.insert(0, ".")

from backend import config, db  # noqa: E402

# A generic maintenance task -- no client, no confidential data. Every
# target_phrase names a concrete visible object; every success_criteria is
# something a camera can confirm by looking.
DEMO_PROCEDURE = {
    "title": "Air compressor daily check",
    "machine": "Reciprocating air compressor",
    "steps": [
        {
            "index": 0,
            "title": "Confirm power isolated",
            "instruction": "Check the red isolator switch is in the down, off position.",
            "target_phrase": "red isolator switch",
            "success_criteria": "The red isolator lever points down and the panel shows no lit indicator.",
            "keyframe_index": 0,
            "common_mistake": "Leaving the isolator up while working on the unit.",
            "hazard": "Confirm isolation before touching any guard.",
        },
        {
            "index": 1,
            "title": "Check oil sight glass",
            "instruction": "Look at the oil sight glass and confirm the level sits between the two marks.",
            "target_phrase": "oil sight glass",
            "success_criteria": "Oil visible in the sight glass, level between the minimum and maximum lines.",
            "keyframe_index": 1,
            "common_mistake": "Reading the glass at an angle and misjudging the level.",
            "hazard": None,
        },
        {
            "index": 2,
            "title": "Drain the receiver tank",
            "instruction": "Open the drain valve under the tank until condensate stops, then close it.",
            "target_phrase": "tank drain valve",
            "success_criteria": "Drain valve lever turned open, then returned fully closed; no water dripping.",
            "keyframe_index": 2,
            "common_mistake": "Forgetting to re-close the drain valve.",
            "hazard": "Condensate is under pressure. Keep hands clear of the outlet.",
        },
        {
            "index": 3,
            "title": "Inspect the belt guard",
            "instruction": "Confirm the belt guard is fitted and both fasteners are in place.",
            "target_phrase": "belt guard",
            "success_criteria": "Guard covers the belt fully and both corner bolts are seated.",
            "keyframe_index": 3,
            "common_mistake": "Running the compressor with the guard left off after service.",
            "hazard": "Never run the compressor with the belt exposed.",
        },
        {
            "index": 4,
            "title": "Check the pressure gauge",
            "instruction": "Start the unit and confirm the pressure gauge rises to the green band.",
            "target_phrase": "pressure gauge",
            "success_criteria": "Gauge needle settles inside the green band, not the red.",
            "keyframe_index": 4,
            "common_mistake": "Accepting a needle still in the red low zone.",
            "hazard": None,
        },
    ],
}


def _png(rgb: tuple[int, int, int], w: int = 320, h: int = 240) -> bytes:
    """A tiny solid-colour PNG, used only as placeholder evidence offline."""
    row = bytes([0]) + bytes(rgb) * w
    raw = zlib.compress(row * h, 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def seed_procedure() -> str:
    db.init()
    for p in db.list_procedures():
        if p["title"] == DEMO_PROCEDURE["title"]:
            print(f"Demo procedure already present ({p['id']}).")
            return p["id"]
    pid = db.save_procedure(
        DEMO_PROCEDURE["title"],
        DEMO_PROCEDURE["steps"],
        machine=DEMO_PROCEDURE["machine"],
        transcript="(hand-authored demo seed)",
    )
    print(f"Seeded procedure '{DEMO_PROCEDURE['title']}' -> {pid} "
          f"({len(DEMO_PROCEDURE['steps'])} steps)")
    return pid


def seed_demo_session(pid: str) -> str:
    """Synthetic session with placeholder evidence, for offline dashboard checks."""
    sid = db.start_session(pid, worker_name="Demo worker", mode="guide")
    colours = {"pass": (47, 207, 122), "fail": (255, 77, 77)}
    plan = [
        ("pass", 0.95, "Red isolator lever is down; no indicator lit."),
        ("pass", 0.90, "Oil sits mid-way between the two marks."),
        ("fail", 0.88, "Drain valve left open — condensate still dripping."),
        ("pass", 0.92, "Guard fully covers the belt; both bolts seated."),
        ("pass", 0.91, "Gauge needle settled inside the green band."),
    ]
    config.MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    for i, (verdict, conf, reason) in enumerate(plan):
        name = f"{sid}_s{i:02d}_a1.png"
        (config.MEDIA_DIR / name).write_bytes(_png(colours[verdict]))
        db.log_step_event(
            sid, i, verdict, confidence=conf, reason=reason,
            grounding=f'Grounding: searched the frame and found 1 match.',
            evidence_path=f"media/{name}", attempt=1,
            latency_ms=1200 + i * 130, backend="hosted",
        )
    db.finish_session(sid, "completed")
    print(f"Seeded SYNTHETIC session {sid} (taints metrics — run --reset before real numbers).")
    return sid


def reset() -> None:
    db.init()
    with db.connect() as conn:
        conn.execute("DELETE FROM step_events")
        conn.execute("DELETE FROM sessions")
    print("Cleared sessions and step events. Procedures kept.")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Seed Ustad demo data.")
    ap.add_argument("--with-demo-session", action="store_true",
                    help="also insert a synthetic session (offline dashboard check only)")
    ap.add_argument("--reset", action="store_true",
                    help="wipe sessions and step events (keeps procedures)")
    args = ap.parse_args()

    if args.reset:
        reset()
        return 0
    pid = seed_procedure()
    if args.with_demo_session:
        seed_demo_session(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
