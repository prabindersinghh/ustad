"""Episode labelling for the fine-tune set. No network, no footage: what matters
is that a wrong label can't slip in, above all a false alarm or a mixed split."""
from __future__ import annotations

from PIL import Image

from backend import config, meta_client, sam_parse
from scripts import make_episodes as me

STEPS = [{"index": i, "title": f"T{i}", "instruction": "do it", "target_phrase": f"thing {i}",
          "success_criteria": f"crit {i}", "common_mistake": None} for i in range(5)]


def _det(x1, y1, x2, y2, conf=0.9, w=480, h=752):
    return sam_parse.Detection(frame=0, ordinal=0, x1=x1, y1=y1, x2=x2, y2=y2, img_w=w, img_h=h, confidence=conf)


def test_the_split_never_mixes_and_leaves_a_gap():
    assert me.split_of(31.49) == "train"
    assert me.split_of(31.5) is None and me.split_of(31.99) is None
    assert me.split_of(32.0) == "eval"


def test_no_phase_table_range_crosses_the_split():
    for first, last, _ in me.PHASES:
        assert not (first < me.TRAIN_END_S and last >= me.EVAL_START_S), (first, last)


def test_step_zero_is_never_a_mismatch():
    """The button is in every frame: 'wrong step' there would teach a false alarm."""
    for phase, truth in me.TRUTH.items():
        assert 0 not in me.mismatch_options(truth), phase


def test_a_step_whose_criteria_holds_is_never_a_mismatch():
    for phase, truth in me.TRUTH.items():
        for s in me.mismatch_options(truth):
            assert truth[s] != "hold", (phase, s)


def test_hands_clear_never_passes_with_a_hand_in_view():
    for phase in me.HAND_PHASES:
        assert me.TRUTH[phase].get(4) == "fail"


def test_forward_print_head_with_a_hand_gives_no_passes():
    assert "hold" not in me.TRUTH["F"].values()


def test_title_card_is_detected_not_assumed():
    assert me.is_title_card(Image.new("RGB", (480, 752), (25, 72, 230)))
    assert not me.is_title_card(Image.new("RGB", (480, 752), (140, 140, 140)))


def test_frames_beside_the_title_card_and_cuts_are_excluded():
    title, cuts = (16.13, 19.07), [6.1, 40.47]
    assert me.exclusion(17.0, title, cuts) == "title card"
    assert me.exclusion(15.95, title, cuts) == "title card"     # inside the margin
    assert me.exclusion(19.25, title, cuts) == "title card"
    assert me.exclusion(6.05, title, cuts) == "beside a cut"
    assert me.exclusion(10.0, title, cuts) is None


def test_a_crop_that_removes_the_target_changes_the_label():
    label, note = me.after_geometry("verify_step", 2, [0.0], [])
    assert label == "request_better_view" and note == "target_cropped_out"
    label, _ = me.after_geometry("flag_error", 4, [0.1], [1.0])
    assert label == "request_better_view"


def test_a_half_cut_target_is_dropped_not_guessed():
    assert me.after_geometry("verify_step", 2, [0.5], [])[0] is None


def test_cropping_a_hand_away_never_makes_hands_clear():
    label, note = me.after_geometry("flag_error", 4, [1.0], [0.3])
    assert label is None and note == "hand_cut"
    assert me.after_geometry("verify_step", 1, [1.0], [0.5])[0] is None


def test_a_step_that_ignores_hands_survives_a_hand_being_cut():
    assert me.after_geometry("verify_step", 0, [1.0], [0.1])[0] == "verify_step"


def test_a_removal_crop_really_excludes_every_box():
    boxes = [(400.0, 110.0, 470.0, 180.0)]
    crop = me.removal_crop(boxes, 480, 752)
    assert crop is not None and all(me.visible_share(b, crop) == 0 for b in boxes)


def test_no_removal_crop_when_the_target_fills_the_frame():
    assert me.removal_crop([(5.0, 5.0, 475.0, 745.0)], 480, 752) is None


def test_every_viewed_region_can_be_cropped_out():
    for step, region in me.VIEWED_REGION.items():
        crop = me.removal_crop([region], 480, 752)
        assert crop is not None and me.visible_share(region, crop) == 0, step


def test_rotation_shares_track_the_trimmed_frame():
    centre = [(200.0, 300.0, 280.0, 450.0)]
    assert me.shares_after({"kind": "rotate", "angle": 3.0}, centre, 480, 752)[0] > 0.9
    corner = [(0.0, 0.0, 30.0, 30.0)]
    assert me.shares_after({"kind": "rotate", "angle": 4.0}, corner, 480, 752)[0] < 0.9


def test_prompt_is_the_production_judge_prompt(monkeypatch):
    """Captured from judge_step, and no model is called."""
    monkeypatch.setattr(config, "JUDGE_REFERENCE", False)
    monkeypatch.setattr(meta_client, "client", lambda: (_ for _ in ()).throw(AssertionError("no model call")))
    real = meta_client.reason
    cap = me.capture_prompt(STEPS[1], b"\xff\xd8", [_det(10, 10, 100, 100)], STEPS)
    assert meta_client.reason is real
    assert cap["system"].startswith("You are Ustad, verifying one step")
    assert [t["name"] for t in cap["tools"]] == ["verify_step", "flag_error", "request_better_view"]
    text = cap["content"][0]["text"]
    assert "Step 2: T1" in text and 'searched the frame for "thing 1"' in text
    assert "Step 3: T2" in text and "Step 5: T4" in text      # the next three, as live passes them
    assert cap["content"][1]["type"] == "input_image"


def test_rows_are_tool_calls_with_tools_and_an_image(monkeypatch):
    monkeypatch.setattr(config, "JUDGE_REFERENCE", False)
    cap = me.capture_prompt(STEPS[4], b"\xff\xd8", [], STEPS)
    row = me.to_row(cap, me.answer("flag_error", STEPS[4], "H", []), b"\xff\xd8")
    sys_msg, user, asst = row["messages"]
    assert sys_msg["role"] == "system" and asst["role"] == "assistant"
    assert user["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert asst["tool_calls"][0]["function"]["name"] == "flag_error"
    assert {t["function"]["name"] for t in row["tools"]} == {"verify_step", "flag_error", "request_better_view"}


def test_every_answer_fits_its_tool_schema_and_cites_the_grounding():
    from backend import guide
    schema = {t["name"]: t["parameters"]["required"] for t in guide.TOOLS}
    dets = [_det(10, 10, 100, 100)]
    for label, phase, step in (("verify_step", "N", 2), ("flag_error", "H", 4), ("flag_error", "F", 2),
                               ("flag_error", "Ef", 4), ("flag_error", "N", 1), ("request_better_view", "N", 3)):
        call = me.answer(label, STEPS[step], phase, dets, "blur")
        assert call["name"] == label and set(schema[label]) <= set(call["arguments"])
        text = " ".join(str(v) for v in call["arguments"].values())
        assert "Grounding found" in text
        assert len(call["arguments"]["say"].split()) < 20      # the system prompt's limit
