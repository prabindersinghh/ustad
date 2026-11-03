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


def test_repair_keeps_the_first_alternative_sam_finds():
    frames = [b"f0", b"f1"]
    proc = {"title": "T", "machine": None,
            "steps": [_step(0, "vague thing", 0), _step(1, "red valve", 1)]}
    good = {"red valve", "left pressure gauge"}
    asked = {}

    def repair(items, fr):
        asked["items"] = [(i["id"], i["kind"], i["old"]) for i in items]
        return {0: ["still vague", "left pressure gauge", "gauge"]}

    out = procedures._validate_and_repair(
        proc, frames, segment_fn=lambda p, f: ["det"] if p in good else [], repair_fn=repair)
    assert asked["items"] == [(0, "target", "vague thing")]          # only the failure is sent
    assert out["steps"][0]["target_phrase"] == "left pressure gauge"
    assert out["steps"][1]["target_phrase"] == "red valve"             # untouched
    v = out["_validation"]
    assert v["repaired"] is True and v["failed_before"] == ["vague thing"] and v["failed_after"] == []


def test_repair_with_no_working_alternative_keeps_the_phrase():
    proc = {"title": "T", "machine": None, "steps": [_step(0, "gauge", 0)]}
    out = procedures._validate_and_repair(
        proc, [b"f0"], segment_fn=lambda p, f: [], repair_fn=lambda items, fr: {0: ["dial", "meter"]})
    assert out["steps"][0]["target_phrase"] == "gauge"
    assert out["_validation"]["repaired"] is False and out["_validation"]["failed_after"] == ["gauge"]


def test_repair_failure_is_not_fatal():
    def boom(items, fr):
        raise RuntimeError("Spark down")
    proc = {"title": "T", "machine": None, "steps": [_step(0, "gauge", 0)]}
    out = procedures._validate_and_repair(proc, [b"f0"], segment_fn=lambda p, f: [], repair_fn=boom)
    assert out["steps"][0]["target_phrase"] == "gauge" and out["_validation"]["failed_after"] == ["gauge"]


def test_no_failures_means_no_repair_call():
    proc = {"title": "T", "machine": None, "steps": [_step(0, "red valve", 0)]}

    def must_not_be_called(items, fr):
        raise AssertionError("repair should not run when nothing failed")

    out = procedures._validate_and_repair(
        proc, [b"f0"], segment_fn=lambda p, f: ["det"], repair_fn=must_not_be_called)
    assert out["_validation"]["repaired"] is False and out["_validation"]["failed_after"] == []


def _watched(i, target, hazard):
    s = _step(i, target, 0)
    s["watch_for"] = [{"phrase": hazard, "role": "hazard"}]
    return s


def test_unfound_hazard_phrase_is_repaired():
    proc = {"title": "T", "machine": None, "steps": [_watched(0, "red stop button", "circular saw blade")]}
    good = {"red stop button", "gear"}
    out = procedures._validate_and_repair(
        proc, [b"f0"], segment_fn=lambda p, f: ["det"] if p in good else [],
        repair_fn=lambda items, fr: {items[0]["id"]: ["toothed disc", "gear"]})
    v = out["_validation"]
    assert v["repaired"] is True and v["watch_missing_before"] == ["circular saw blade"]
    assert out["steps"][0]["watch_for"][0] == {"phrase": "gear", "role": "hazard", "trackable": True}
    assert v["watch_untracked"] == []


def test_hazard_still_unfound_is_marked_not_trackable():
    proc = {"title": "T", "machine": None, "steps": [_watched(0, "red stop button", "saw blade")]}
    out = procedures._validate_and_repair(
        proc, [b"f0"], segment_fn=lambda p, f: ["det"] if p == "red stop button" else [],
        repair_fn=lambda items, fr: {items[0]["id"]: ["blade"]})
    item = out["steps"][0]["watch_for"][0]
    assert item["trackable"] is False and item["phrase"] == "saw blade"
    assert out["_validation"]["watch_untracked"] == ["saw blade"]


def test_merge_maps_each_step_to_a_frame_of_the_whole_take(monkeypatch):
    from backend import meta_client
    seen = {}

    def fake(system, content, **k):
        seen["text"] = content[0]["text"]
        return {"title": "Job", "steps": [{"title": "A", "instruction": "Do A", "target_phrase": "valve", "frame": 57},
                                          {"title": "B", "instruction": "Do B", "target_phrase": "cap", "frame": 999}]}
    monkeypatch.setattr(meta_client, "reason_json", fake)
    drafts = [{"title": "Job", "steps": [{"title": "A", "instruction": "Do A", "frame": 57}]},
              {"title": "Job", "steps": [{"title": "B", "instruction": "Do B", "frame": 120}]}]
    out = procedures.merge_drafts(drafts, n_frames=200)
    assert '"frame": 57' in seen["text"] and '"part": 2' in seen["text"]
    assert out["steps"][0]["keyframe_index"] == 57
    assert 0 <= out["steps"][1]["keyframe_index"] < 200           # a bad frame number is clamped


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
