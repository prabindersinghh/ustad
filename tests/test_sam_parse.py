"""Unit tests for the SAM 3.1 token-grammar parser.

The guide loop lives or dies on this parser: if a box is misread, the overlay
lands on the wrong pixels and the reasoning model is fed a lie. These tests pin
the grammar documented in the spec so a future edit can't silently break it.

Run:  .venv/Scripts/python.exe -m pytest tests/ -q
"""
from __future__ import annotations

import math

from backend import sam_parse

# The exact record shape from the API docs.
ONE_BOX = "<0f>0<|box;x1=181;y1=365;x2=556;y2=486;w=1516;h=1600|><|mask;x=0;y=0;data=AAAA|>"


def test_empty_output_is_no_detection_not_error():
    # SAM signals "concept not found" with an empty output_text. Normal result.
    assert sam_parse.parse("") == []
    assert sam_parse.parse("   \n ") == []


def test_single_box_fields():
    dets = sam_parse.parse(ONE_BOX)
    assert len(dets) == 1
    d = dets[0]
    assert (d.x1, d.y1, d.x2, d.y2) == (181, 365, 556, 486)
    assert (d.img_w, d.img_h) == (1516, 1600)
    assert d.frame == 0
    assert d.ordinal == 0
    assert d.confidence is None


def test_mask_token_is_ignored():
    # Two tokens follow the box in ONE_BOX; only the box must be parsed.
    dets = sam_parse.parse(ONE_BOX)
    assert len(dets) == 1  # the mask must not produce a phantom detection


def test_confidence_field_parsed_when_present():
    raw = "<0f>0<|box;x1=10;y1=10;x2=110;y2=110;w=1000;h=1000;c=0.94|>"
    d = sam_parse.parse(raw)[0]
    assert d.confidence is not None
    assert math.isclose(d.confidence, 0.94, abs_tol=1e-6)


def test_area_fraction_and_center():
    # A 100x100 box in a 1000x1000 frame -> ~1% area, centred near (0.06, 0.06).
    raw = "<0f>0<|box;x1=0;y1=0;x2=99;y2=99;w=1000;h=1000|>"
    d = sam_parse.parse(raw)[0]
    assert math.isclose(d.area_fraction, (100 * 100) / (1000 * 1000), abs_tol=1e-6)
    cx, cy = d.center
    assert math.isclose(cx, 49.5 / 1000, abs_tol=1e-6)
    assert math.isclose(cy, 49.5 / 1000, abs_tol=1e-6)


def test_describe_positions():
    def box(cx_frac, cy_frac):
        # Build a small box centred at the requested fraction of a 1000px frame.
        cx, cy = int(cx_frac * 1000), int(cy_frac * 1000)
        return f"<0f>0<|box;x1={cx-5};y1={cy-5};x2={cx+5};y2={cy+5};w=1000;h=1000|>"

    assert "centre of frame" in sam_parse.parse(box(0.5, 0.5))[0].describe()
    assert "top-left" in sam_parse.parse(box(0.1, 0.1))[0].describe()
    assert "bottom-right" in sam_parse.parse(box(0.9, 0.9))[0].describe()


def test_multiple_detections_same_frame():
    raw = (
        "<0f>0<|box;x1=10;y1=10;x2=50;y2=50;w=500;h=500|>"
        "<0f>1<|box;x1=100;y1=100;x2=140;y2=140;w=500;h=500|>"
    )
    dets = sam_parse.parse(raw)
    assert len(dets) == 2
    assert dets[0].ordinal == 0
    assert dets[1].ordinal == 1
    assert dets[0].x1 == 10 and dets[1].x1 == 100


def test_multiple_video_frames():
    raw = (
        "<0f>0<|box;x1=1;y1=1;x2=9;y2=9;w=100;h=100|>"
        "<1f>0<|box;x1=2;y1=2;x2=9;y2=9;w=100;h=100|>"
    )
    dets = sam_parse.parse(raw)
    assert len(dets) == 2
    assert dets[0].frame == 0
    assert dets[1].frame == 1


def test_degenerate_dims_do_not_divide_by_zero():
    raw = "<0f>0<|box;x1=1;y1=1;x2=9;y2=9;w=0;h=0|>"
    d = sam_parse.parse(raw)[0]
    assert d.area_fraction == 0.0
    assert d.center == (0.0, 0.0)
    # describe() must not raise even with zero dims.
    assert isinstance(d.describe(), str)


def test_malformed_box_is_skipped_not_fatal():
    raw = "<0f>0<|box;x1=notanumber;y1=5;x2=9;y2=9;w=100;h=100|>"
    # Non-numeric field -> that record is dropped, parser keeps going.
    assert sam_parse.parse(raw) == []


def test_to_dict_is_json_friendly():
    d = sam_parse.parse(ONE_BOX)[0]
    out = d.to_dict()
    assert out["x1"] == 181 and out["img_w"] == 1516
    assert "center" in out and "area_fraction" in out


def test_summarise_no_match_and_match():
    assert "no match" in sam_parse.summarise("red valve", [])
    dets = sam_parse.parse("<0f>0<|box;x1=10;y1=10;x2=90;y2=90;w=100;h=100;c=0.8|>")
    text = sam_parse.summarise("red valve", dets)
    assert "found 1 match" in text
    assert "confidence 0.80" in text
