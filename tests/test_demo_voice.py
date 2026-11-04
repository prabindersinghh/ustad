"""The rules for placing spoken lines in the demo edit. No media, no network."""
from __future__ import annotations

from pathlib import Path

from scripts.demo_voice import map_time, output_length, place_queue

EDL = [(10.0, 20.0), (30.0, 40.0), (40.0, 40.0, 1.0)]


def speak(at, length, text="x"):
    return {"kind": "speak", "at": at, "file": Path("a.mp3"), "length": length, "text": text, "source": "test"}


def test_times_map_through_cuts():
    assert output_length(EDL) == 21.0
    assert map_time(EDL, 12.0) == 2.0
    assert map_time(EDL, 25.0) == 10.0         # inside a cut: the next frame kept
    assert map_time(EDL, 31.0) == 11.0
    assert map_time(EDL, 5.0) == 0.0           # before the first frame kept
    assert map_time(EDL, 99.0) == 21.0


def test_lines_queue_instead_of_overlapping():
    lines = place_queue([speak(11, 3, "a"), speak(12, 2, "b")], EDL, "U", 21)
    assert [(s.text, s.start) for s in lines] == [("a", 1.0), ("b", 4.0)]


def test_a_cancel_cuts_the_current_line_and_drops_the_queue():
    events = [speak(11, 5, "a"), speak(12, 2, "dropped"), {"kind": "cancel", "at": 13.0}, speak(14, 1, "c")]
    lines = place_queue(events, EDL, "U", 21)
    assert [s.text for s in lines] == ["a", "c"]
    assert lines[0].length == 2.0 and lines[0].cut_short
    assert lines[1].start == 4.0 and not lines[1].cut_short


def test_a_line_past_the_end_is_cut_off_at_the_end():
    lines = place_queue([speak(39.5, 5, "a"), speak(40, 5, "b")], EDL, "U", 21)
    assert lines[0].cut_short and lines[0].start + lines[0].length == 21
    assert len(lines) == 1
