"""What the overlay renderer may draw, and when. No model calls, no network."""
from __future__ import annotations

import pytest

from scripts.render_overlay import clean_text, overlay_at, parse_ranges, resolve_out

STEPS = [{"index": 0, "title": "One", "instruction": "do one"}, {"index": 1, "title": "Two", "instruction": "do two"}]


def det(conf, x1=10, img=480):
    return {"x1": x1, "y1": 20, "x2": x1 + 30, "y2": 60, "img_w": img, "img_h": 752, "confidence": conf}


def point(at, step, dets, verdict="pass", **kw):
    return {"checkpoint_s": at, "step_index": step, "detections": dets, "verdict": verdict, "reason": "seen",
            "backend": "hosted", "phrase": "part", "video_w": 480, "video_h": 752, **kw}


POINTS = [point(6.0, 0, [det(0.5, x1=100), det(0.9, x1=200), det(0.7, x1=300)]), point(14.0, 1, [], verdict="retry")]
ARGS = dict(cuts=[6.1, 40.5], no_draw=[(16.1, 19.1)])


def test_only_the_best_box_is_drawn_like_the_app():
    o = overlay_at(6.0, POINTS, STEPS, **ARGS)
    assert o.box == (200, 20, 230, 60)
    assert o.box_label == "part 0.90"


def test_a_box_stays_near_its_moment():
    assert overlay_at(5.4, POINTS, STEPS, **ARGS).box is None
    assert overlay_at(5.6, POINTS, STEPS, **ARGS).box is not None


def test_a_box_never_crosses_a_cut():
    assert overlay_at(6.05, POINTS, STEPS, **ARGS).box is not None
    assert overlay_at(6.2, POINTS, STEPS, **ARGS).box is None


def test_an_upscaled_box_comes_back_to_source_pixels():
    o = overlay_at(6.0, [point(6.0, 0, [det(0.9, x1=200, img=960)])], STEPS, **ARGS)
    assert o.box[0] == 100


def test_no_detection_means_no_box_and_says_so():
    o = overlay_at(14.0, POINTS, STEPS, **ARGS)
    assert o.box is None and o.box_label is None
    assert "did not find" in o.no_box_note
    failed = overlay_at(14.0, [point(14.0, 1, [], sam_error=True)], STEPS, **ARGS)
    assert "failed" in failed.no_box_note


def test_verdict_holds_for_two_seconds_then_the_next_step_shows():
    assert overlay_at(5.9, POINTS, STEPS, **ARGS).verdict is None
    held = overlay_at(7.9, POINTS, STEPS, **ARGS)
    assert held.verdict == "pass" and held.step["title"] == "One" and held.reason == "seen"
    after = overlay_at(8.1, POINTS, STEPS, **ARGS)
    assert after.verdict is None and after.step["title"] == "Two"


def test_nothing_at_all_over_the_title_card():
    assert overlay_at(17.0, POINTS, STEPS, **ARGS) == overlay_at(17.0, [], [], **ARGS)
    for t in (16.1, 18.9):
        o = overlay_at(t, POINTS, STEPS, **ARGS)
        assert not (o.step or o.box or o.verdict or o.backend or o.reason)


def test_only_a_jump_far_above_ordinary_motion_counts_as_a_cut():
    from scripts.render_overlay import cuts_from_diffs
    assert cuts_from_diffs([2.0, 2.5, 31.0, 2.0, 6.0, 2.0], 60) == [3 / 60]
    assert cuts_from_diffs([], 60) == []


def test_backend_label_comes_from_the_record():
    assert overlay_at(6.0, [point(6.0, 0, [], backend="open")], STEPS, **ARGS).backend == "open"


def test_drawn_text_has_no_em_dash():
    assert clean_text("seen — clear\n now") == "seen, clear now"


def test_ranges_and_output_folder(tmp_path):
    assert parse_ranges("16.1-19.1,30-31") == [(16.1, 19.1), (30.0, 31.0)]
    assert parse_ranges("") == []
    assert str(resolve_out("x.mp4")).replace("\\", "/").endswith("data/factory/replay/x.mp4")
    assert resolve_out(str(tmp_path / "y.mp4")) == tmp_path / "y.mp4"


@pytest.mark.parametrize("verdict", ["pass", "fail", "retry"])
def test_each_verdict_has_a_label(verdict):
    from scripts.render_overlay import VERDICTS
    assert verdict in VERDICTS
