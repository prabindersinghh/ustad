"""Offline tests for the eval harness scoring, so the winning numbers are
computed by code that is itself tested. No network: the judge is faked."""
from __future__ import annotations

from pathlib import Path

from scripts import evaluate


def test_parse_frame_filename_categories():
    assert evaluate.parse_frame_filename("step_03_correct.jpg") == (3, "correct", "pass")
    assert evaluate.parse_frame_filename("step_03_wrong_valve_open.jpg") == (
        3, "wrong_valve_open", "fail")
    assert evaluate.parse_frame_filename("step_01_unclear_blurry.png") == (
        1, "unclear_blurry", "retry")
    assert evaluate.parse_frame_filename("step-7-ok.jpeg") == (7, "ok", "pass")
    assert evaluate.parse_frame_filename("notaframe.jpg") is None


def test_run_maps_1based_filenames_to_0based_steps(tmp_path: Path):
    # Two frames for a 2-step procedure.
    (tmp_path / "step_01_correct.jpg").write_bytes(b"x")
    (tmp_path / "step_02_wrong_x.jpg").write_bytes(b"y")
    proc = {"title": "T", "steps": [
        {"index": 0, "title": "A", "target_phrase": "a"},
        {"index": 1, "title": "B", "target_phrase": "b"},
    ]}
    frames = evaluate.discover_frames(tmp_path)

    seen = []

    def fake_judge(step, frame, **kw):
        seen.append(step["index"])
        # step 1 correct -> pass; step 2 wrong -> we (mistakenly) pass to test scoring
        return {"verdict": "pass", "latency_ms": 1000, "backend": "hosted", "detail": {}}

    results = evaluate.run(proc, frames, judge_fn=fake_judge)
    assert seen == [0, 1]            # 1-based filenames mapped to 0-based steps
    assert len(results) == 2


def test_score_computes_rates_and_catches_false_pass():
    results = [
        {"expected": "pass", "actual": "pass", "latency_ms": 1000, "backend": "hosted"},
        {"expected": "pass", "actual": "pass", "latency_ms": 1200, "backend": "hosted"},
        {"expected": "fail", "actual": "fail", "latency_ms": 1100, "backend": "hosted"},
        {"expected": "fail", "actual": "pass", "latency_ms": 900, "backend": "hosted"},  # missed error
        {"expected": "retry", "actual": "retry", "latency_ms": 800, "backend": "hosted"},
    ]
    m = evaluate.score(results)
    assert m["n_correct"] == 2 and m["confirm_rate"] == 100.0
    assert m["n_wrong"] == 2 and m["catch_rate"] == 50.0
    assert m["false_pass"] == 1 and m["false_pass_rate"] == 50.0
    assert m["false_alarm"] == 0
    assert m["appropriate_retry"] == 1 and m["retry_rate"] == 100.0
    assert m["mean_latency_ms"] == 1000


def test_to_markdown_includes_cost_when_price_given():
    m = evaluate.score([
        {"expected": "pass", "actual": "pass", "latency_ms": 1000, "backend": "hosted"},
    ])
    md = evaluate.to_markdown(m, {"title": "Demo", "n_steps": 5, "when": "2026-11-01",
                                  "cost_per_check": 0.01})
    assert "Correct steps confirmed" in md
    assert "Cost" in md and "per clean session" in md
    assert "0.050" in md  # 5 steps * $0.01


def test_to_markdown_omits_cost_without_price():
    m = evaluate.score([{"expected": "pass", "actual": "pass", "latency_ms": 1,
                         "backend": "hosted"}])
    md = evaluate.to_markdown(m, {"title": "Demo", "n_steps": 5, "cost_per_check": None})
    assert "Cost" not in md
