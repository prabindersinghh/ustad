#!/usr/bin/env python3
"""Assemble the demo from real recordings and one audio file per voice line.

    python -m scripts.demo_cut --tts <path to edge-tts>

To change a line, edit its text in scripts/demo_lines.json and run this again:
that file is regenerated and the cut is rebuilt, reusing every section whose
timing did not change. Everything drawn in part 1's side panels comes from the
WebSocket log of the real run. Spoken app lines are placed with the app's own
queue rules (scripts.demo_voice), since headless Chromium has no audible speech.

Output, under data/factory/replay/, all 1920x1080 at 30 fps:
  demo_capture.mp4, demo_guide.mp4, demo_part1.mp4, demo_catch.mp4, demo_site.mp4,
  demo_check.mp4, demo_hindi.mp4, demo_glimmer.mp4, demo_close.mp4, demo_full.mp4
Each has a .voice.wav stem and a .lines.json listing every spoken line with its
start time and source.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from PIL import Image, ImageDraw, ImageFont

from backend import db
from scripts.demo_voice import Spoken, map_time, output_length, place_queue

REPLAY = Path("data/factory/replay")
MEDIA = Path("data/factory/media")
VOICE = REPLAY / "voice"
LINES = Path(__file__).with_name("demo_lines.json")
PUBLIC_URL = "https://prabindersinghh--ustad-web.us-east.modal.run"
W, H, FPS = 1920, 1080, 30

# Design tokens from web/app.css. Figtree is a web font, so the side panels use
# the app's own next fallback on this machine, Segoe UI.
INK, WHITE, MUTED, BLUE = "#0a1020", "#ffffff", "#93a3c0", "#2d7bff"
GOOD, BAD, WARN = "#12d98a", "#ff5a5f", "#ffb020"
FONT = "C:/Windows/Fonts/segoeui.ttf"
FONT_BOLD = "C:/Windows/Fonts/seguisb.ttf"

PHONE_H = 1040
PHONE_W = round(PHONE_H * 390 / 844)
PHONE_X, PHONE_Y = (W - PHONE_W) // 2, (H - PHONE_H) // 2

# What is kept of each real recording, in its own seconds. A third number holds
# that moment still. Nothing here changes a verdict or a time shown on screen.
CAPTURE_EDL = [(2.1, 22.6), (40.0, 41.0), (50.0, 50.8), (54.3, 56.2), (56.2, 56.2, 1.2)]
GUIDE_EDL = [(1.5, 12.4), (16.9, 18.2), (25.0, 40.3), (42.0, 48.0)]
HINDI_EDL = [(1.6, 4.1), (9.1, 23.3)]
RL_REPORT = Path("data/factory/rl/episodes_report.json")
# Our own test run with a really wrong step. A desk, not a factory, and labelled so.
CATCH_SESSION, CATCH_STEP = "af3b0ea0a00f", 1
EDGE_SESSION = "41af986467a1"            # its second opinion is already stored: viewed, never requested
# Each photo's crop, as fractions of the frame: keeps the hand, the bottle and the
# table, and leaves out the Coca-Cola bottle on the right.
CATCH_CROPS = {1: (0.0, 0.14, 0.66, 0.80), 2: (0.0, 0.14, 0.62, 0.80), 3: (0.0, 0.14, 0.70, 0.80)}
TEST_RUN_LABEL = "From our own test run"
VOICES_LABEL = "Voices are synthetic."
EXPERT_FILES = [("n1", 4.8), ("n2", 11.85), ("n3", 15.5)]   # seconds after the fake mic opened


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT, size)


def wrap(text: str, f: ImageFont.FreeTypeFont, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        probe = f"{line} {word}".strip()
        if f.getlength(probe) <= width:
            line = probe
        else:
            out.append(line)
            line = word
    return out + [line] if line else out


def text_block(d: ImageDraw.ImageDraw, x: int, y: int, text: str, f: ImageFont.FreeTypeFont, fill: str,
               width: int, gap: int = 8) -> int:
    for line in wrap(text, f, width):
        d.text((x, y), line, font=f, fill=fill)
        y += f.size + gap
    return y


# ------------------------------------------------------------------ encoding

class Encoder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.frames = 0
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-pix_fmt", "yuv420p", "-r", str(FPS), "-video_track_timescale", "30000", str(path)],
            stdin=subprocess.PIPE)

    def add(self, image: Image.Image, count: int = 1) -> None:
        raw = image.tobytes()
        for _ in range(count):
            self.proc.stdin.write(raw)
        self.frames += count

    def close(self) -> None:
        self.proc.stdin.close()
        if self.proc.wait() != 0:
            raise RuntimeError(f"ffmpeg failed on {self.path}")


def probe_duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=30).stdout.strip()
    return float(out)


def trimmed(path: Path) -> Path:
    """The line without the silence edge-tts leaves around it (a spoken "Done." came
    back 1.9 s long, which threw off the app's speech queue and the pacing)."""
    out = path.with_suffix(".wav")
    if out.exists() and out.stat().st_mtime >= path.stat().st_mtime:
        return out
    edge = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.03"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(path), "-af",
                    f"{edge},areverse,{edge},areverse", "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(out)],
                   check=True, timeout=60)
    return out


def make_voice(tts: str, name: str, text: str, voice: str, rate: str = "+0%") -> Path:
    """One audio file per line; made again only when its words or voice change."""
    VOICE.mkdir(parents=True, exist_ok=True)
    path, stamp = VOICE / f"{name}.mp3", VOICE / f"{name}.txt"
    wanted = f"{voice}|{rate}|{text}"
    if path.exists() and stamp.exists() and stamp.read_text(encoding="utf-8") == wanted:
        return trimmed(path)
    for attempt in range(6):
        done = subprocess.run([tts, f"--voice={voice}", f"--rate={rate}", f"--text={text}", "--write-media", str(path)],
                              capture_output=True, timeout=90)
        if done.returncode == 0 and path.exists() and path.stat().st_size:
            stamp.write_text(wanted, encoding="utf-8")
            return trimmed(path)
        time.sleep(2 + attempt)
    raise RuntimeError(f"Could not make speech for {name}")


def write_stem(spoken: list[Spoken], length: float, out: Path) -> None:
    """All lines at their start times, as one voice track."""
    inputs: list[str] = []
    chains, labels = [], []
    for i, s in enumerate(spoken):
        inputs += ["-i", str(s.file)]
        trim = f"atrim=0:{s.length:.3f}," if s.cut_short else ""
        chains.append(f"[{i}]{trim}aresample=48000,adelay={int(s.start * 1000)}:all=1[a{i}]")
        labels.append(f"[a{i}]")
    graph = ";".join(chains) + ";" + "".join(labels) + (
        f"amix=inputs={len(spoken)}:normalize=0:duration=longest,apad=whole_dur={length:.3f},atrim=0:{length:.3f}[out]")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
                    "-map", "[out]", "-ac", "2", "-c:a", "pcm_s16le", str(out)], check=True, timeout=300)


def mux(video: Path, stem: Path, out: Path) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-i", str(stem),
                    "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                    "-ac", "2", "-shortest", str(out)], check=True, timeout=300)


def join(parts: list[Path], out: Path) -> None:
    listing = out.with_suffix(".txt")
    listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", str(out)], check=True, timeout=300)
    listing.unlink()


def save_lines(spoken: list[Spoken], out: Path, **extra: Any) -> None:
    rows = [{"start_s": round(s.start, 3), "end_s": round(s.start + s.length, 3), "file": s.file.name,
             "speaker": s.speaker, "text": s.text, "source": s.source, "cut_short": s.cut_short} for s in spoken]
    out.write_text(json.dumps({**extra, "lines": rows}, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------- real screens

class Run:
    """A screen recording: frames plus the socket log on the same clock."""

    def __init__(self, name: str) -> None:
        self.dir = REPLAY / name
        self.frames = json.loads((self.dir / "frames.json").read_text(encoding="utf-8"))
        self.t0 = self.frames[0]["t"]
        self.times = [f["t"] - self.t0 for f in self.frames]
        self.events = json.loads((self.dir / "events.json").read_text(encoding="utf-8"))
        for e in self.events:
            e["at"] = e["t"] - self.t0
        self.info = json.loads((self.dir / "run.json").read_text(encoding="utf-8"))
        self._cursor, self._last, self._image = 0, None, None

    def image_at(self, t: float) -> Image.Image:
        i = self._cursor if t >= self.times[self._cursor] else 0
        while i + 1 < len(self.times) and self.times[i + 1] <= t:
            i += 1
        self._cursor = i
        if i != self._last:
            self._image = Image.open(self.dir / "frames" / self.frames[i]["file"]).convert("RGB")
            self._last = i
        return self._image


def edl_times(edl: list[tuple[float, ...]]) -> Iterator[float]:
    """Source time of each output frame."""
    for seg in edl:
        if len(seg) == 3:
            for _ in range(round(seg[2] * FPS)):
                yield seg[0]
        else:
            n = round((seg[1] - seg[0]) * FPS)
            for k in range(n):
                yield seg[0] + k / FPS


def rounded_mask() -> Image.Image:
    scale = 4
    m = Image.new("L", (PHONE_W * scale, PHONE_H * scale), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, PHONE_W * scale - 1, PHONE_H * scale - 1), 34 * scale, fill=255)
    return m.resize((PHONE_W, PHONE_H), Image.LANCZOS)


MASK: Image.Image | None = None


def phone_on(panel: Image.Image, screen: Image.Image) -> Image.Image:
    global MASK
    MASK = MASK or rounded_mask()
    frame = panel.copy()
    frame.paste(screen.resize((PHONE_W, PHONE_H), Image.LANCZOS), (PHONE_X, PHONE_Y), MASK)
    return frame


def draw_panel(stage: str, title: str, note: str, speaker: str, said: str, log: list[tuple[str, str, str]]) -> Image.Image:
    """Dark sides of the phone. Left says where we are, right what was said and
    what the record shows. Only text from the run goes in the right column."""
    im = Image.new("RGB", (W, H), INK)
    d = ImageDraw.Draw(im)
    left, width = 70, PHONE_X - 140
    d.rounded_rectangle((left, 70, left + 58, 128), 14, fill="#1a56e0")
    d.text((left + 17, 72), "U", font=font(38, True), fill=WHITE)
    d.text((left + 74, 74), "Ustad", font=font(38, True), fill=WHITE)
    d.text((left, 300), stage.upper(), font=font(28, True), fill=BLUE)
    y = text_block(d, left, 345, title, font(54, True), WHITE, width, 12)
    text_block(d, left, max(y + 30, 900), note, font(26), MUTED, width, 8)

    right, rwidth = PHONE_X + PHONE_W + 70, W - (PHONE_X + PHONE_W) - 130
    y = 74
    if said:
        d.text((right, y), speaker.upper(), font=font(26, True), fill=BLUE)
        y = text_block(d, right, y + 42, said, font(38, True), WHITE, rwidth, 10) + 30
    y = max(y, 470)
    for head, body, colour in log[-5:]:
        d.text((right, y), head, font=font(30, True), fill=colour)
        y = text_block(d, right, y + 40, body, font(26), MUTED, rwidth, 6) + 18
    return im


def said_at(spoken: list[Spoken], t: float) -> Spoken | None:
    now = [s for s in spoken if s.start <= t < s.start + s.length + 0.4]
    return now[-1] if now else None


def render_phone_part(run: Run, edl: list[tuple[float, ...]], out: Path, spoken: list[Spoken], stage: str, title: str,
                      note: str, log_at: Callable[[float], list[tuple[str, str, str]]]) -> None:
    enc = Encoder(out)
    panels: dict[str, Image.Image] = {}
    for i, src in enumerate(edl_times(edl)):
        t = i / FPS
        line = said_at(spoken, t)
        log = log_at(src)
        key = json.dumps([line.text if line else "", log])
        if key not in panels:
            panels.clear()
            panels[key] = draw_panel(stage, title, note, line.speaker if line else "", line.text if line else "", log)
        enc.add(phone_on(panels[key], run.image_at(src)))
    enc.close()


def render_screens(enc: Encoder, run: Run, start: float, seconds: float, overlay: Image.Image | None = None,
                   crop: tuple[int, int, int, int] | None = None, top_left: Image.Image | None = None) -> None:
    """A real page for `seconds`, from `start` in its recording, then held still."""
    for k in range(round(seconds * FPS)):
        img = run.image_at(start + k / FPS)
        img = img.crop(crop) if crop else img
        img = img.resize((W, H), Image.LANCZOS) if img.size != (W, H) else img.copy()
        if overlay is not None:
            img.paste(overlay, (0, H - overlay.height))
        if top_left is not None:
            img.paste(top_left, (24, 24))
        enc.add(img)


# -------------------------------------------------------------------- site

def still_shot(enc: Encoder, name: str, box: tuple[int, int, int, int], seconds: float) -> None:
    """A photo, cropped by the rules in PLAN.md, with a very slow push in."""
    src = Image.open(MEDIA / name).convert("RGB")
    x0, y0, x1, y1 = box
    n = round(seconds * FPS)
    for k in range(n):
        z = 0.05 * k / max(1, n - 1)
        dx, dy = (x1 - x0) * z / 2, (y1 - y0) * z / 2
        enc.add(src.resize((W, H), Image.BICUBIC, box=(x0 + dx, y0 + dy, x1 - dx, y1 - dy)))


def clip_frames(name: str, start: float, end: float, speed: float, h: int, seconds: float) -> list[bytes]:
    """A portrait clip as raw frames at height h; the last frame holds if it runs short."""
    w = round(h * 720 / 1280)
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(start), "-to", str(end), "-i", str(MEDIA / name),
         "-an", "-vf", f"setpts=PTS/{speed},fps={FPS},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, timeout=120, check=True)
    size = w * h * 3
    frames = [proc.stdout[i:i + size] for i in range(0, len(proc.stdout) - size + 1, size)]
    need = round(seconds * FPS)
    return (frames + [frames[-1]] * need)[:need]


def site_panels(enc: Encoder, seconds: float) -> None:
    """Three real clips side by side, ranges chosen for small or turned-away faces,
    with no control panel in view and no laptop."""
    h, gap = H, 30
    w = round(h * 720 / 1280)
    clips = [clip_frames("att3.mp4", 4.2, 8.2, 0.6, h, seconds),
             clip_frames("att1.mp4", 0.2, 4.6, 0.65, h, seconds),
             clip_frames("att2.mp4", 20.6, 22.3, 0.5, h, seconds)]
    x0 = (W - (3 * w + 2 * gap)) // 2
    pill = font(36, True)
    label = "MCPL (Modern Colours Private Limited), Ludhiana"
    tw = int(pill.getlength(label))
    for k in range(round(seconds * FPS)):
        frame = Image.new("RGB", (W, H), INK)
        for i, c in enumerate(clips):
            frame.paste(Image.frombytes("RGB", (w, h), c[k]), (x0 + i * (w + gap), 0))
        d = ImageDraw.Draw(frame)
        d.rounded_rectangle(((W - tw) // 2 - 30, H - 110, (W + tw) // 2 + 30, H - 40), 35, fill=INK)
        d.text(((W - tw) // 2, H - 98), label, font=pill, fill=WHITE)
        enc.add(frame)


def measured_card() -> Image.Image:
    """The numbers from DECISIONS.md D-053, shown as plain figures. Not app UI."""
    im = Image.new("RGB", (W, H), "#f5f8fe")
    d = ImageDraw.Draw(im)
    d.text((180, 130), "Measured on 8 test cases, 4 of them deliberately wrong", font=font(52, True), fill=INK)
    cols = (180, 880, 1300)
    y = 280
    for c, head in zip(cols, ("Model", "Right", "Median time per check")):
        d.text((c, y), head, font=font(36, True), fill="#64728c")
    rows = [("Muse Spark", "8 of 8", "4.6 s"), ("Muse Glimmer", "8 of 8", "63 s")]
    y += 80
    for row in rows:
        d.line((180, y, W - 180, y), fill="#dde7f7", width=3)
        y += 34
        for c, cell in zip(cols, row):
            d.text((c, y), cell, font=font(64, True), fill=INK)
        y += 110
    d.line((180, y, W - 180, y), fill="#dde7f7", width=3)
    text_block(d, 180, y + 44, "Same judge code and photos. No false passes from either model. Muse Glimmer is the "
               "base open-weight model, on NVIDIA's API. Live guiding stays on Muse Spark.", font(34), "#33405c", W - 360, 12)
    return im


def link_bar() -> Image.Image:
    bar = Image.new("RGB", (W, 120), "#1a56e0")
    d = ImageDraw.Draw(bar)
    f = font(46, True)
    d.text(((W - f.getlength(PUBLIC_URL)) / 2, 30), PUBLIC_URL, font=f, fill=WHITE)
    small = font(26)
    d.text((W - small.getlength(VOICES_LABEL) - 40, 44), VOICES_LABEL, font=small, fill=WHITE)
    return bar


def pill(text: str, size: int = 34) -> Image.Image:
    f = font(size, True)
    im = Image.new("RGB", (int(f.getlength(text)) + 56, size + 34), INK)
    ImageDraw.Draw(im).text((28, 12), text, font=f, fill=WHITE)
    return im


def stored_opinion_bar() -> Image.Image:
    """The stored second opinion, from the database: the page shows the verdict and
    time, but not the confidence."""
    row = db.second_opinions(EDGE_SESSION)[0]
    said = "agreed with" if row.get("agrees") else "disagreed with"
    text = (f"Stored result: Muse Glimmer {said} the {row['first_verdict']}, confidence {row['confidence']:.2f}, "
            f"{row['latency_ms'] / 1000:.0f} s, model {row['model']}.")
    bar = Image.new("RGB", (W, 84), INK)
    f = font(32, True)
    ImageDraw.Draw(bar).text(((W - f.getlength(text)) / 2, 20), text, font=f, fill=WHITE)
    return bar


def rl_numbers() -> dict[str, int]:
    """From the episode builder's own report. Held out is both evaluation sets."""
    report = json.loads(RL_REPORT.read_text(encoding="utf-8"))["episodes"]
    train = sum(report["train"].values())
    held = sum(sum(v.values()) for k, v in report.items() if k != "train")
    return {"train": train, "held": held, "total": train + held}


def rl_card(line: str) -> Image.Image:
    """The numbers from the report, plainly. The spoken total must match them."""
    n = rl_numbers()
    if f"{n['total']:,}" not in line:
        raise RuntimeError(f"rl_1 says something other than the report's total of {n['total']:,}")
    im = Image.new("RGB", (W, H), "#f5f8fe")
    d = ImageDraw.Draw(im)
    d.text((180, 130), "Reinforcement learning on Muse Glimmer", font=font(52, True), fill=INK)
    d.text((180, 230), f"{n['total']:,}", font=font(150, True), fill="#1a56e0")
    d.text((180, 430), "labelled training examples, from one 42 second clip", font=font(44), fill="#33405c")
    for x, value, label in ((180, n["train"], "train"), (640, n["held"], "held out")):
        d.text((x, 530), f"{value:,}", font=font(110, True), fill=INK)
        d.text((x, 680), label, font=font(44), fill="#33405c")
    status, f = "Not trained yet", font(52, True)
    w = f.getlength(status)
    d.rounded_rectangle((180, 800, 180 + w + 80, 900), 50, fill="#e8900a")
    d.text((220, 818), status, font=f, fill=INK)
    return im


def catch_rows() -> list[dict[str, Any]]:
    """Step 2's attempts, from the database, photos and reasons as they were saved."""
    rows = [e for e in db.session_log(CATCH_SESSION)["events"]
            if e["step_index"] == CATCH_STEP and e["verdict"] in ("pass", "fail")]
    if [e["attempt"] for e in rows] != [1, 2, 3]:
        raise RuntimeError("Expected three attempts on the test run's step 2")
    return rows


def catch_frame(shown: int) -> Image.Image:
    """The first `shown` attempts, side by side: each saved photo, cropped only to
    leave out a branded bottle, with its real verdict and reason beneath."""
    proc = db.get_procedure(db.session_log(CATCH_SESSION)["session"]["procedure_id"])
    step = proc["steps"][CATCH_STEP]
    im = Image.new("RGB", (W, H), "#f5f8fe")
    d = ImageDraw.Draw(im)
    d.text((70, 44), TEST_RUN_LABEL, font=font(32, True), fill="#1a56e0")
    d.text((70, 88), f"Step {CATCH_STEP + 1}: {step['title']}", font=font(50, True), fill=INK)
    d.text((70, 156), f"Done when: {step['success_criteria']}", font=font(32), fill="#33405c")
    names = {"fail": ("Error caught", "#e5484d"), "pass": ("Verified", "#12a06a")}
    width, gap, top, photo_h = 560, 40, 215, 640
    for i, row in enumerate(catch_rows()[:shown]):
        x = 70 + i * (width + gap)
        src = Image.open(Path("data") / row["evidence_path"]).convert("RGB")
        a, b, c, e = CATCH_CROPS[row["attempt"]]
        cropped = src.crop((int(a * src.width), int(b * src.height), int(c * src.width), int(e * src.height)))
        scale = min(width / cropped.width, photo_h / cropped.height)
        pic = cropped.resize((round(cropped.width * scale), round(cropped.height * scale)), Image.LANCZOS)
        im.paste(pic, (x, top))
        name, colour = names[row["verdict"]]
        conf = f" {row['confidence']:.2f}" if row.get("confidence") is not None else ""
        y = top + photo_h + 18
        d.text((x, y), f"Attempt {row['attempt']}: {name}{conf}", font=font(36, True), fill=colour)
        text_block(d, x, y + 50, row["reason"], font(30), INK, width, 6)
    return im


# ----------------------------------------------------------------- part 1

def side_log_capture(run: Run) -> Callable[[float], list[tuple[str, str, str]]]:
    proc = next(e for e in run.events if e.get("type") == "procedure")

    def log(src: float) -> list[tuple[str, str, str]]:
        if src < proc["at"]:
            return []
        p = proc["procedure"]
        check = p.get("_validation") or {}
        missed = check.get("failed_after") or []
        rows = [(f"{len(p['steps'])} steps written", p["title"], GOOD)]
        rows.append(("SAM checked every part name on the footage",
                     ("Not found: " + ", ".join(missed)) if missed else "All found", WARN if missed else GOOD))
        return rows
    return log


def side_log_guide(run: Run) -> Callable[[float], list[tuple[str, str, str]]]:
    names = {"pass": ("Verified", GOOD), "fail": ("Error caught", BAD), "retry": ("Need a better view", WARN)}
    entries: list[tuple[float, tuple[str, str, str]]] = []
    warnings: list[tuple[float, str]] = []
    for e in run.events:
        if e.get("type") == "verdict" and e.get("verdict") in names:
            name, colour = names[e["verdict"]]
            conf = (e.get("detail") or {}).get("confidence")
            bits = [f"confidence {conf:.2f}" if isinstance(conf, (int, float)) else None,
                    f"{e['latency_ms'] / 1000:.1f} s" if e.get("latency_ms") else None,
                    "SAM found the part" if e.get("detections") else "no SAM box: judged from the image alone",
                    "on its own" if e.get("source") == "auto" else "after a Done tap", str(e.get("backend"))]
            entries.append((e["at"], (f"Step {e.get('step')}: {name}", ", ".join(b for b in bits if b), colour)))
        elif e.get("type") == "proximity":
            warnings.append((e["at"], str(e.get("text"))))

    def log(src: float) -> list[tuple[str, str, str]]:
        """Each step's result in order, with the hand warnings so far as one row."""
        rows = [(at, row) for at, row in entries if at <= src]
        seen = [text for at, text in warnings if at <= src]
        if seen:
            first = next(at for at, _ in warnings)
            label = "Live hand warning" + (f" (x{len(seen)})" if len(seen) > 1 else "")
            rows.append((first, (label, seen[-1], BAD)))
        return [row for _, row in sorted(rows, key=lambda r: r[0])]
    return log


def expert_lines(run: Run, edl: list[tuple[float, ...]], narration: list[str]) -> list[Spoken]:
    camera = next(e["at"] for e in run.events if e.get("type") == "camera_live")
    opened = camera - 0.3
    out = []
    for (name, delay), text in zip(EXPERT_FILES, narration):
        file = REPLAY / "feeds" / f"{name}.mp3"
        out.append(Spoken(file, map_time(edl, opened + delay), probe_duration(file), "Expert (draft narration)",
                          text, "narration_draft.txt, also played as the fake microphone"))
    return out


def ustad_events(run: Run, tts: str, voice: str) -> list[dict[str, Any]]:
    """What the app really said, in order, with what cancels speech in app.js."""
    out: list[dict[str, Any]] = []
    for e in run.events:
        kind = e.get("type")
        if kind == "speak" and e.get("text"):
            name = "ustad_" + hashlib.sha1(e["text"].encode("utf-8")).hexdigest()[:10]
            file = make_voice(tts, name, e["text"], voice, "+8%")      # app.js speaks at rate 1.08
            out.append({"kind": "speak", "at": e["at"], "file": file, "length": probe_duration(file), "text": e["text"],
                        "source": f"websocket speak, session {run.info.get('session_id')}, {e['at']:.2f} s into the run"})
        elif kind == "procedure":
            text = f"Procedure saved. {len(e['procedure']['steps'])} steps."
            name = "ustad_" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
            file = make_voice(tts, name, text, voice, "+8%")
            out.append({"kind": "speak", "at": e["at"], "file": file, "length": probe_duration(file), "text": text,
                        "source": f"app.js Voice.say on the procedure message, procedure {e['procedure_id']}"})
        elif kind == "checking":
            out.append({"kind": "cancel", "at": e["at"]})             # Voice.stop() in Guide.handle
        elif kind == "complete":
            out.append({"kind": "cancel", "at": e["at"] - 0.001})     # Report.open -> Guide.teardown -> Voice.stop()
    return out


NOTE_REAL = ("Real app at phone size. Footage fed as the camera. Waits are cut; the times shown are the real ones. "
             "Voices are synthetic.")


def build_part1(tts: str, voices: dict[str, str]) -> tuple[list[Path], list[Spoken], float]:
    capture, guide = Run("run_capture2"), Run("run_guide1")
    narration = [line.split("] ", 1)[1].strip()
                 for line in (REPLAY / "narration_draft.txt").read_text(encoding="utf-8").splitlines() if line.strip()]

    cap_len = output_length(CAPTURE_EDL)
    cap_lines = expert_lines(capture, CAPTURE_EDL, narration)
    cap_lines += place_queue(ustad_events(capture, tts, voices["ustad"]), CAPTURE_EDL, "Ustad says", cap_len)
    guide_len = output_length(GUIDE_EDL)
    guide_lines = place_queue(ustad_events(guide, tts, voices["ustad"]), GUIDE_EDL, "Ustad says", guide_len)

    cap_v, guide_v = REPLAY / "_capture_video.mp4", REPLAY / "_guide_video.mp4"
    render_phone_part(capture, CAPTURE_EDL, cap_v, cap_lines, "1. Capture", "An expert records the job once.",
                      "The expert's words are a draft for this clip. " + NOTE_REAL, side_log_capture(capture))
    render_phone_part(guide, GUIDE_EDL, guide_v, guide_lines, "2. Guide", "A worker is guided, and every step is checked.",
                      NOTE_REAL, side_log_guide(guide))

    results: list[Path] = []
    for name, video, lines, length in (("demo_capture", cap_v, cap_lines, cap_len), ("demo_guide", guide_v, guide_lines, guide_len)):
        stem = REPLAY / f"{name}.voice.wav"
        write_stem(lines, length, stem)
        mux(video, stem, REPLAY / f"{name}.mp4")
        save_lines(lines, REPLAY / f"{name}.lines.json", video=f"{name}.mp4")
        results.append(REPLAY / f"{name}.mp4")
    return results, cap_lines + [s.shifted(cap_len) for s in guide_lines], cap_len + guide_len


# ----------------------------------------------------------------- sections

def lay_out(tts: str, keys: list[str], texts: dict[str, str], voice: str, start: float = 0.3, gap: float = 0.4) -> dict[str, Spoken]:
    placed, at = {}, start
    for k in keys:
        file = make_voice(tts, k, texts[k], voice)
        length = probe_duration(file)
        placed[k] = Spoken(file, at, length, "Narrator", texts[k], f"scripts/demo_lines.json: {k}")
        at += length + gap
    return placed


def section(name: str, spoken: list[Spoken], length: float, draw: Callable[[Encoder], None]) -> Path:
    """Rebuilt only if its timing or lines changed, so one line swap is quick."""
    sign = hashlib.sha1(json.dumps([[s.file.name, round(s.start, 3), s.text] for s in spoken] + [length]).encode()).hexdigest()
    video, stamp = REPLAY / f"_{name}_video.mp4", REPLAY / f"_{name}.sign"
    if not (video.exists() and stamp.exists() and stamp.read_text() == sign):
        enc = Encoder(video)
        draw(enc)
        enc.close()
        stamp.write_text(sign)
    stem, out = REPLAY / f"{name}.voice.wav", REPLAY / f"{name}.mp4"
    write_stem(spoken, length, stem)
    mux(video, stem, out)
    save_lines(spoken, REPLAY / f"{name}.lines.json", video=out.name)
    return out


def build_site(tts: str, texts: dict[str, str], voices: dict[str, str]) -> tuple[Path, list[Spoken], float]:
    p = lay_out(tts, ["site_1", "site_2", "site_3", "site_4", "site_5"], texts, voices["vo"])
    t1 = p["site_2"].start + p["site_2"].length + 0.15
    t2 = p["site_3"].start + p["site_3"].length + 0.15
    t3 = p["site_4"].start + p["site_4"].length + 0.15
    end = p["site_5"].start + p["site_5"].length + 0.4

    def draw(enc: Encoder) -> None:
        site_panels(enc, t1)
        # Crops leave out the control panel with the painted symbol, and the near faces.
        still_shot(enc, "photo_warehouse.png", (600, 500, 4752, 2836), t2 - t1)
        still_shot(enc, "photo_mixers.png", (0, 1214, 2587, 2669), t3 - t2)
        still_shot(enc, "photo_floor_a.png", (0, 1214, 2745, 2758), end - t3)
    return section("demo_site", list(p.values()), end, draw), list(p.values()), end


def build_check(tts: str, texts: dict[str, str], voices: dict[str, str]) -> tuple[Path, list[Spoken], float]:
    p = lay_out(tts, ["check_1", "check_2", "check_3", "check_4"], texts, voices["vo"])
    t1 = p["check_2"].start + p["check_2"].length + 0.15
    end = p["check_4"].start + p["check_4"].length + 0.5

    def draw(enc: Encoder) -> None:
        render_screens(enc, Run("run_loop"), 0.5, t1)
        render_screens(enc, Run("run_evidence"), 0.5, end - t1)
    return section("demo_check", list(p.values()), end, draw), list(p.values()), end


def build_catch(tts: str, texts: dict[str, str], voices: dict[str, str]) -> tuple[Path, list[Spoken], float]:
    p = lay_out(tts, ["catch_1", "catch_2"], texts, voices["vo"])
    second_at = p["catch_2"].start
    end = p["catch_2"].start + p["catch_2"].length + 0.5
    page_at = second_at + 2.8            # then the real evidence page, to the end
    label = pill(TEST_RUN_LABEL)
    # Window of the page recorded at 2560x1440: all of the rows, and the photo up to
    # the branded bottle at its right edge. Nothing in the rows is cut.
    window = (0, 180, 1920, 1260)

    def draw(enc: Encoder) -> None:
        for k in range(round(page_at * FPS)):
            t = k / FPS
            shown = 1 if t < p["catch_1"].start + p["catch_1"].length * 0.45 else 2 if t < second_at else 3
            enc.add(catch_frame(shown))
        render_screens(enc, Run("run_bottle"), 0.2, end - page_at, crop=window, top_left=label)
    return section("demo_catch", list(p.values()), end, draw), list(p.values()), end


def build_hindi(tts: str, texts: dict[str, str], voices: dict[str, str]) -> tuple[Path, list[Spoken], float]:
    run = Run("run_hindi")
    first = next(e for e in run.events if e.get("type") == "speak")
    # The first two of the line's three sentences, to keep the section short.
    spoken_text = "\u0964".join(first["text"].split("\u0964")[:2]).strip() + "\u0964"
    file = make_voice(tts, "hindi_app_line", spoken_text, voices["hindi"])
    one = lay_out(tts, ["hindi_1"], texts, voices["vo"])["hindi_1"]
    hindi_at = max(map_time(HINDI_EDL, first["at"]), one.start + one.length + 0.2)
    app_line = Spoken(file, hindi_at, probe_duration(file), "Ustad says", spoken_text,
                      f"websocket speak, first two of its three sentences, session {run.info.get('session_id')}, "
                      f"{first['at']:.2f} s into the run")
    two = lay_out(tts, ["hindi_2"], texts, voices["vo"], start=hindi_at + app_line.length + 0.3)["hindi_2"]
    spoken = [one, app_line, two]
    end = two.start + two.length + 0.4
    kept = output_length(HINDI_EDL)
    edl = HINDI_EDL + ([(HINDI_EDL[-1][1], HINDI_EDL[-1][1], end - kept)] if end > kept else [])
    english = [one, two]

    def draw(enc: Encoder) -> None:
        panel_cache: dict[str, Image.Image] = {}
        for i, src in enumerate(edl_times(edl)):
            t = i / FPS
            line = said_at(english, t)
            key = line.text if line else ""
            if key not in panel_cache:
                panel_cache.clear()
                panel_cache[key] = draw_panel("Hindi", "The same job, in Hindi.", "Real app at phone size, real translation. " + NOTE_REAL.split(". ", 1)[1],
                                              line.speaker if line else "", key, [])
            enc.add(phone_on(panel_cache[key], run.image_at(src)))
    return section("demo_hindi", spoken, end, draw), spoken, end


def build_glimmer(tts: str, texts: dict[str, str], voices: dict[str, str]) -> tuple[Path, list[Spoken], float]:
    p = lay_out(tts, ["glimmer_1", "glimmer_2", "glimmer_3", "data_1", "rl_1"], texts, voices["vo"])
    t1 = p["glimmer_2"].start - 0.1
    t2 = p["glimmer_3"].start - 0.1
    t3 = p["data_1"].start - 0.1
    t4 = p["rl_1"].start - 0.1
    end = p["rl_1"].start + p["rl_1"].length + 0.5
    edge = Run("run_edge")
    card, numbers = measured_card(), rl_card(texts["rl_1"])
    stored = stored_opinion_bar()

    def draw(enc: Encoder) -> None:
        render_screens(enc, edge, 0.0, t1)
        enc.add(card, round((t2 - t1) * FPS))
        # The real page, on the doubtful-steps button and the stored result. Not pressed.
        render_screens(enc, edge, edge.times[-1], t3 - t2, stored)
        render_screens(enc, Run("run_evidence"), 6.0, t4 - t3)
        enc.add(numbers, round((end - t4) * FPS))
    return section("demo_glimmer", list(p.values()), end, draw), list(p.values()), end


def build_close(tts: str, texts: dict[str, str], voices: dict[str, str]) -> tuple[Path, list[Spoken], float]:
    p = lay_out(tts, ["close_1", "close_2", "close_3", "close_4"], texts, voices["vo"])
    end = p["close_4"].start + p["close_4"].length + 2.0
    bar = link_bar()

    def draw(enc: Encoder) -> None:
        # The measured table first, then up to the MCPL photos, where it ends.
        render_screens(enc, Run("run_proof"), 2.8, end, bar)
    return section("demo_close", list(p.values()), end, draw), list(p.values()), end


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tts", required=True, help="path to the edge-tts program")
    ap.add_argument("--skip-part1", action="store_true", help="reuse demo_capture and demo_guide as they are")
    args = ap.parse_args()
    data = json.loads(LINES.read_text(encoding="utf-8"))
    texts, voices = data["lines"], data["voices"]

    if args.skip_part1:
        names = ["demo_capture", "demo_guide"]
        part_files = [REPLAY / f"{n}.mp4" for n in names]
        rows = []
        at = 0.0
        for n, f in zip(names, part_files):
            for r in json.loads((REPLAY / f"{n}.lines.json").read_text(encoding="utf-8"))["lines"]:
                rows.append({**r, "start_s": round(r["start_s"] + at, 3), "end_s": round(r["end_s"] + at, 3)})
            at += probe_duration(f)
        part1_rows, part1_len = rows, at
    else:
        part_files, spoken, part1_len = build_part1(tts=args.tts, voices=voices)
        part1_rows = [{"start_s": round(s.start, 3), "end_s": round(s.start + s.length, 3), "file": s.file.name,
                       "speaker": s.speaker, "text": s.text, "source": s.source, "cut_short": s.cut_short} for s in spoken]
    join(part_files, REPLAY / "demo_part1.mp4")
    (REPLAY / "demo_part1.lines.json").write_text(
        json.dumps({"video": "demo_part1.mp4", "lines": part1_rows}, ensure_ascii=False, indent=1), encoding="utf-8")

    built = [b(args.tts, texts, voices) for b in (build_catch, build_site, build_check, build_hindi, build_glimmer, build_close)]
    join([REPLAY / "demo_part1.mp4"] + [b[0] for b in built], REPLAY / "demo_full.mp4")

    rows, at = list(part1_rows), probe_duration(REPLAY / "demo_part1.mp4")
    for path, spoken_lines, _ in built:
        rows += [{"start_s": round(s.start + at, 3), "end_s": round(s.start + s.length + at, 3), "file": s.file.name,
                  "speaker": s.speaker, "text": s.text, "source": s.source, "cut_short": s.cut_short} for s in spoken_lines]
        at += probe_duration(path)
    (REPLAY / "demo_full.lines.json").write_text(
        json.dumps({"video": "demo_full.mp4", "lines": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    total = probe_duration(REPLAY / "demo_full.mp4")
    print(f"demo_full.mp4: {total:.2f} s" + ("" if total <= 180 else "  OVER 3:00"))


if __name__ == "__main__":
    main()
