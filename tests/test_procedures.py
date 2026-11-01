"""Tests for the capture-mode grounding self-check.

The network calls (SAM grounding, procedure regeneration) are injected, so these
run offline and pin the repair logic: which phrases count as failures, when a
repair is accepted, and that keyframe indices are clamped to valid frames.
"""
from __future__ import annotations

from backend import procedures


def _step(index, phrase, ki=0, title=None):
    return {
        "index": index,
        "title": title or f"Step {index + 1}",
        "instruction": "do the thing",
        "target_phrase": phrase,
        "success_criteria": "it looks done",
        "keyframe_index": ki,
        "common_mistake": None,
        "hazard": None,
    }


def test_check_grounding_flags_only_unfound_phrases():
    frames = [b"f0", b"f1"]
    steps = [_step(0, "red valve", 0), _step(1, "ghost widget", 1)]

    # SAM finds "red valve" but not "ghost widget".
    def fake_segment(phrase, frame):
        return ["det"] if phrase == "red valve" else []

    failures = procedures._check_grounding(steps, frames, fake_segment)
    assert [f["target_phrase"] for f in failures] == ["ghost widget"]


def test_check_grounding_treats_sam_error_as_not_a_failure():
    frames = [b"f0"]
    steps = [_step(0, "something", 0)]

    def boom(phrase, frame):
        raise RuntimeError("SAM timeout")

    # A transport error is not evidence the phrase is bad -> not flagged.
    assert procedures._check_grounding(steps, frames, boom) == []


def test_empty_phrase_is_a_failure():
    frames = [b"f0"]
    steps = [_step(0, "", 0)]
    assert procedures._check_grounding(steps, frames, lambda p, f: ["x"]) == steps


def test_bad_keyframe_index_falls_back_in_range():
    frames = [b"f0", b"f1"]
    seen = {}

    def fake_segment(phrase, frame):
        seen["frame"] = frame
        return ["det"]

    # keyframe_index 99 is out of range -> must clamp to a valid frame, not crash.
    procedures._check_grounding([_step(0, "valve", 99)], frames, fake_segment)
    assert seen["frame"] in frames


def test_validate_and_repair_accepts_a_successful_repair():
    frames = [b"f0", b"f1"]
    proc = {"title": "T", "machine": None,
            "steps": [_step(0, "vague thing", 0), _step(1, "red valve", 1)]}

    repaired = {"title": "T", "machine": None,
                "steps": [_step(0, "left pressure gauge", 0), _step(1, "red valve", 1)]}

    good = {"red valve", "left pressure gauge"}

    def fake_segment(phrase, frame):
        return ["det"] if phrase in good else []

    out = procedures._validate_and_repair(
        proc, frames, "transcript", hint=None,
        segment_fn=fake_segment, generate_fn=lambda: repaired,
    )
    assert out["_validation"]["repaired"] is True
    assert out["_validation"]["failed_before"] == ["vague thing"]
    assert out["_validation"]["failed_after"] == []
    assert out["steps"][0]["target_phrase"] == "left pressure gauge"


def test_validate_and_repair_rejects_a_worse_repair():
    frames = [b"f0"]
    proc = {"title": "T", "machine": None, "steps": [_step(0, "gauge", 0)]}
    # The "repair" introduces two failures where there was one -> reject it.
    worse = {"title": "T", "machine": None,
             "steps": [_step(0, "nope", 0), _step(1, "also nope", 0)]}

    out = procedures._validate_and_repair(
        proc, frames, "t", hint=None,
        segment_fn=lambda p, f: [],       # nothing grounds
        generate_fn=lambda: worse,
    )
    # Original kept because repair did not reduce the failure count.
    assert out["steps"] == proc["steps"]


def test_no_failures_means_no_regeneration():
    frames = [b"f0"]
    proc = {"title": "T", "machine": None, "steps": [_step(0, "red valve", 0)]}

    def must_not_be_called():
        raise AssertionError("generate_fn should not run when nothing failed")

    out = procedures._validate_and_repair(
        proc, frames, "t", hint=None,
        segment_fn=lambda p, f: ["det"], generate_fn=must_not_be_called,
    )
    assert out["_validation"]["repaired"] is False
    assert out["_validation"]["failed_after"] == []


def _watched(i, target, hazard):
    s = _step(i, target, 0)
    s["watch_for"] = [{"phrase": hazard, "role": "hazard"}]
    return s


def test_unfound_hazard_phrase_triggers_a_repair():
    frames = [b"f0"]
    proc = {"title": "T", "machine": None, "steps": [_watched(0, "red stop button", "circular saw blade")]}
    repaired = {"title": "T", "machine": None, "steps": [_watched(0, "red stop button", "gear")]}
    good = {"red stop button", "gear"}

    out = procedures._validate_and_repair(
        proc, frames, "t", hint=None,
        segment_fn=lambda p, f: ["det"] if p in good else [], generate_fn=lambda: repaired,
    )
    v = out["_validation"]
    assert v["repaired"] is True and v["watch_missing_before"] == ["circular saw blade"]
    assert out["steps"][0]["watch_for"][0] == {"phrase": "gear", "role": "hazard", "trackable": True}
    assert v["watch_untracked"] == []


def test_hazard_still_unfound_is_marked_not_trackable():
    frames = [b"f0"]
    proc = {"title": "T", "machine": None, "steps": [_watched(0, "red stop button", "saw blade")]}

    out = procedures._validate_and_repair(
        proc, frames, "t", hint=None,
        segment_fn=lambda p, f: ["det"] if p == "red stop button" else [],
        generate_fn=lambda: {"title": "T", "machine": None,
                             "steps": [_watched(0, "red stop button", "blade")]},
    )
    item = out["steps"][0]["watch_for"][0]
    assert item["trackable"] is False
    assert out["_validation"]["watch_untracked"] == [item["phrase"]]


def test_sam_error_does_not_mark_a_phrase_untrackable():
    def boom(p, f):
        raise RuntimeError("SAM down")
    steps = [_watched(0, "x", "saw blade")]
    assert procedures._check_watch(steps, [b"f0"], boom) == []
    assert steps[0]["watch_for"][0]["trackable"] is True


def test_normalise_clamps_keyframe_index_and_drops_empty_instructions():
    data = {
        "title": "Changeover",
        "steps": [
            {"title": "A", "instruction": "turn the valve", "target_phrase": "valve",
             "keyframe_index": 99},            # out of range -> clamp
            {"title": "B", "instruction": "", "target_phrase": "x"},  # dropped
        ],
    }
    out = procedures._normalise(data, n_frames=3)
    assert len(out["steps"]) == 1
    assert 0 <= out["steps"][0]["keyframe_index"] < 3
