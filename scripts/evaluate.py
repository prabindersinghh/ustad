#!/usr/bin/env python3
"""Replay labelled frames against a procedure and measure verification quality.

This is the number that wins: not "the demo worked," but an auditable accuracy
figure from ground-truth frames. It runs the SAME decision code as the live
guide loop (`backend.guide.judge_step`), so the numbers are honest.

Frame naming (ground truth encoded in the filename):

    step_03_correct.jpg              step 3, done correctly  -> expect PASS
    step_03_wrong_valve_open.jpg     step 3, planted error   -> expect FAIL
    step_03_unclear_blurry.jpg       step 3, bad view        -> expect RETRY

`step_NN` is the 1-based step number. The label token right after it sets the
ground truth: correct/ok/good -> PASS, wrong/bad/error -> FAIL,
unclear/noview/obscured/partial -> RETRY. Everything after is free text.

Usage:

    .venv/Scripts/python.exe -m scripts.evaluate --procedure <id> --frames ./eval_frames
    .venv/Scripts/python.exe -m scripts.evaluate --procedure-file proc.json --frames ./f --out report.md

Cost per session is reported only if you pass --cost-per-check (USD); the
per-call price depends on your plan, so the harness will not invent it.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, ".")

from backend import db, guide  # noqa: E402

_FRAME_RE = re.compile(r"step[_-]?(\d+)[_-](.+?)\.(jpe?g|png)$", re.IGNORECASE)

# label token -> ground-truth verdict the system should produce
_LABELS = {
    "correct": "pass", "ok": "pass", "good": "pass", "right": "pass",
    "pass": "pass", "done": "pass",
    "wrong": "fail", "bad": "fail", "error": "fail", "fail": "fail",
    "incorrect": "fail", "danger": "fail", "hazard": "fail",
    "unclear": "retry", "noview": "retry", "obscured": "retry",
    "partial": "retry", "blur": "retry", "blurry": "retry", "far": "retry",
}


def parse_frame_filename(name: str) -> tuple[int, str, str] | None:
    """'step_03_wrong_valve_open.jpg' -> (3, 'wrong_valve_open', 'fail')."""
    m = _FRAME_RE.search(name)
    if not m:
        return None
    step_num = int(m.group(1))
    label = m.group(2)
    first_tok = re.split(r"[_-]", label, 1)[0].lower()
    category = _LABELS.get(first_tok, "other")
    return step_num, label, category


def load_procedure(args: argparse.Namespace) -> dict[str, Any]:
    if args.procedure_file:
        data = json.loads(Path(args.procedure_file).read_text(encoding="utf-8"))
        return data
    if args.procedure:
        proc = db.get_procedure(args.procedure)
        if not proc:
            raise SystemExit(f"No procedure with id {args.procedure!r} in the database.")
        return proc
    raise SystemExit("Provide --procedure <id> or --procedure-file <path>.")


def discover_frames(frames_dir: Path) -> list[tuple[Path, int, str, str]]:
    out = []
    for p in sorted(frames_dir.iterdir()):
        if not p.is_file():
            continue
        parsed = parse_frame_filename(p.name)
        if parsed:
            out.append((p, *parsed))
        else:
            print(f"  skip (unrecognised name): {p.name}")
    return out


def run(
    procedure: dict[str, Any],
    frames: list[tuple[Path, int, str, str]],
    *,
    judge_fn: Callable[..., dict[str, Any]] = guide.judge_step,
) -> list[dict[str, Any]]:
    steps = {s["index"]: s for s in procedure["steps"]}
    results = []
    for path, step_num, label, expected in frames:
        step = steps.get(step_num - 1)  # filenames are 1-based
        if step is None:
            print(f"  skip (no step {step_num}): {path.name}")
            continue
        frame = path.read_bytes()
        t0 = time.perf_counter()
        action = judge_fn(step, frame, worker_said="done", attempt=1)
        wall_ms = int((time.perf_counter() - t0) * 1000)
        results.append({
            "file": path.name,
            "step": step_num,
            "label": label,
            "expected": expected,
            "actual": action["verdict"],
            "latency_ms": action.get("latency_ms", wall_ms),
            "backend": action.get("backend", "?"),
            "reason": (action["detail"].get("reason")
                       or action["detail"].get("what_is_wrong")
                       or action["detail"].get("what_to_show") or ""),
        })
        ok = "OK " if action["verdict"] == expected else "XX "
        print(f"  {ok}{path.name:<40} expected {expected:<6} got {action['verdict']:<6} "
              f"{results[-1]['latency_ms']}ms")
    return results


def score(results: list[dict[str, Any]]) -> dict[str, Any]:
    def n(expected, actual=None):
        return sum(1 for r in results
                   if r["expected"] == expected and (actual is None or r["actual"] == actual))

    n_correct, n_wrong, n_retry = n("pass"), n("fail"), n("retry")
    confirmed = n("pass", "pass")
    caught = n("fail", "fail")
    false_pass = n("fail", "pass")          # missed a planted error -> dangerous
    false_alarm = n("pass", "fail")         # flagged a good step as wrong
    appropriate_retry = n("retry", "retry")

    lat = [r["latency_ms"] for r in results if r["latency_ms"]]
    lat_sorted = sorted(lat)

    def pct(a, b):
        return round(100 * a / b, 1) if b else None

    return {
        "n_frames": len(results),
        "n_correct": n_correct, "n_wrong": n_wrong, "n_retry": n_retry,
        "confirmed": confirmed, "confirm_rate": pct(confirmed, n_correct),
        "caught": caught, "catch_rate": pct(caught, n_wrong),
        "false_pass": false_pass, "false_pass_rate": pct(false_pass, n_wrong),
        "false_alarm": false_alarm, "false_alarm_rate": pct(false_alarm, n_correct),
        "appropriate_retry": appropriate_retry, "retry_rate": pct(appropriate_retry, n_retry),
        "mean_latency_ms": round(statistics.mean(lat)) if lat else None,
        "median_latency_ms": round(statistics.median(lat)) if lat else None,
        "p95_latency_ms": lat_sorted[int(0.95 * (len(lat_sorted) - 1))] if lat else None,
        "backends": sorted({r["backend"] for r in results}),
    }


def to_markdown(m: dict[str, Any], meta: dict[str, Any]) -> str:
    def row(label, value):
        return f"| {label} | {value} |"

    def rate(count, pct, denom):
        if pct is None:
            return "—"
        return f"{pct}% ({count}/{denom})"

    cost_line = ""
    if meta.get("cost_per_check") is not None:
        per_check = meta["cost_per_check"]
        n_steps = meta.get("n_steps", 0)
        cost_line = (
            f"\n**Cost** - ${per_check:.4f} per check, "
            f"~${per_check * n_steps:.3f} per clean session ({n_steps} steps, one pass each)."
        )

    lines = [
        f"### Ustad verification eval — {meta.get('title', 'procedure')}",
        "",
        f"_{m['n_frames']} ground-truth frames · backend(s): {', '.join(m['backends']) or 'n/a'} "
        f"· {meta.get('when', '')}_",
        "",
        "| Metric | Result |",
        "|---|---|",
        row("Correct steps confirmed", rate(m["confirmed"], m["confirm_rate"], m["n_correct"])),
        row("Planted errors caught", rate(m["caught"], m["catch_rate"], m["n_wrong"])),
        row("**Missed errors (false pass)**", rate(m["false_pass"], m["false_pass_rate"], m["n_wrong"])),
        row("False alarms (good flagged wrong)", rate(m["false_alarm"], m["false_alarm_rate"], m["n_correct"])),
        row("Bad views correctly deferred", rate(m["appropriate_retry"], m["retry_rate"], m["n_retry"])),
        row("Mean latency / check", f"{m['mean_latency_ms']} ms" if m["mean_latency_ms"] else "—"),
        row("Median / p95 latency", f"{m['median_latency_ms']} / {m['p95_latency_ms']} ms"
            if m["median_latency_ms"] else "—"),
        "",
        cost_line,
    ]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    try:  # Windows console defaults to cp1252; keep unicode in the report safe.
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Evaluate Ustad step verification.")
    ap.add_argument("--procedure", help="procedure id from the database")
    ap.add_argument("--procedure-file", help="procedure JSON file instead of the DB")
    ap.add_argument("--frames", required=True, help="folder of labelled frames")
    ap.add_argument("--out", help="write the markdown report to this file")
    ap.add_argument("--cost-per-check", type=float, default=None,
                    help="USD per check (1 SAM + 1 reason call) for a cost estimate")
    args = ap.parse_args()

    frames_dir = Path(args.frames)
    if not frames_dir.is_dir():
        raise SystemExit(f"Frames folder not found: {frames_dir}")

    procedure = load_procedure(args)
    frames = discover_frames(frames_dir)
    if not frames:
        raise SystemExit("No labelled frames found. Name them like step_03_correct.jpg")

    print(f"\nEvaluating '{procedure['title']}' against {len(frames)} frames...\n")
    results = run(procedure, frames)
    metrics = score(results)
    meta = {
        "title": procedure["title"],
        "n_steps": len(procedure["steps"]),
        "when": time.strftime("%Y-%m-%d"),
        "cost_per_check": args.cost_per_check,
    }
    md = to_markdown(metrics, meta)
    print("\n" + md)
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
