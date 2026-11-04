#!/usr/bin/env python3
"""Drive the real Ustad web app in Chromium and record the screen.

A video file is the camera and a WAV file is the microphone, through Chromium's
own fake-device flags, so the app runs its normal code against real models. The
screen is saved frame by frame with the time each frame appeared, and every
WebSocket message is logged with the same clock, so a voice track can be placed
at the moment the app really spoke. Frame data and audio are never logged.

Needs Playwright, kept out of the server's venv:

    data/factory/replay/pwenv/Scripts/python.exe scripts/demo_record.py capture \\
        --base http://localhost:8810 --camera feed.y4m --mic narration.wav --out run_capture --stop-after 16
    data/factory/replay/pwenv/Scripts/python.exe scripts/demo_record.py guide \\
        --base http://localhost:8810 --camera feed.y4m --mic silence.wav --out run_guide --procedure <id>
    data/factory/replay/pwenv/Scripts/python.exe scripts/demo_record.py visit \\
        --base https://... --path "/dashboard" --out run_dash --seconds 8 --scroll 0,900 --viewport 1920x1080 --zoom 1.5

The tap policy for a guided job is fixed here, not per run: wait for the server
to verify the step on its own, and tap "Done" once a step has been on screen for
--tap-after seconds. A step still not passed after --max-tries taps is skipped
with the app's own Skip button, which the record shows as a skip. Nothing here
decides a verdict.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

CHROMIUM = Path.home() / "AppData/Local/ms-playwright/chromium-1243/chrome-win64/chrome.exe"
PHONE = {"width": 390, "height": 844}
PHONE_SCALE = 2


class Recorder:
    """Screencast frames on disk, plus a log of socket messages on one clock."""

    def __init__(self, out: Path) -> None:
        self.out = out
        (out / "frames").mkdir(parents=True, exist_ok=True)
        self.frames: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.cdp = None

    def note(self, source: str, msg: dict[str, Any]) -> None:
        self.events.append({"t": time.time(), "source": source, **msg})

    def on_socket(self, ws: Any) -> None:
        def received(payload: Any) -> None:
            if not isinstance(payload, str):
                return
            try:
                msg = json.loads(payload)
            except ValueError:
                return
            if msg.get("type") in ("frame_ack", "pong"):
                return
            self.note("ws_in", msg)

        def sent(payload: Any) -> None:
            if not isinstance(payload, str):
                return
            try:
                kind = json.loads(payload).get("type")
            except ValueError:
                return
            if kind not in ("frame", "watch", "still", "ping", "diag", "audio", "audio_gap"):
                self.note("ws_out", {"type": kind})

        ws.on("framereceived", received)
        ws.on("framesent", sent)

    async def start(self, page: Any, *, max_width: int, max_height: int) -> None:
        self.cdp = await page.context.new_cdp_session(page)

        async def ack(session_id: int) -> None:
            try:
                await self.cdp.send("Page.screencastFrameAck", {"sessionId": session_id})
            except Exception:  # noqa: BLE001 - the page may be closing
                pass

        def frame(params: dict[str, Any]) -> None:
            i = len(self.frames)
            name = f"{i:06d}.jpg"
            (self.out / "frames" / name).write_bytes(base64.b64decode(params["data"]))
            self.frames.append({"file": name, "t": params["metadata"]["timestamp"]})
            asyncio.ensure_future(ack(params["sessionId"]))

        self.cdp.on("Page.screencastFrame", frame)
        await self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "everyNthFrame": 1,
                                                     "maxWidth": max_width, "maxHeight": max_height})

    async def stop(self) -> None:
        if self.cdp:
            try:
                await self.cdp.send("Page.stopScreencast")
            except Exception:  # noqa: BLE001
                pass

    def save(self, **extra: Any) -> None:
        (self.out / "frames.json").write_text(json.dumps(self.frames), encoding="utf-8")
        (self.out / "events.json").write_text(json.dumps(self.events, ensure_ascii=False, indent=0), encoding="utf-8")
        (self.out / "run.json").write_text(json.dumps(extra, ensure_ascii=False, indent=1), encoding="utf-8")


def chromium_args(camera: Path | None, mic: Path | None) -> list[str]:
    args = ["--autoplay-policy=no-user-gesture-required", "--force-device-scale-factor=1"]
    if camera:
        args += ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                 f"--use-file-for-fake-video-capture={camera}"]
    if mic:
        args.append(f"--use-file-for-fake-audio-capture={mic}%noloop")
    return args


async def open_phone(p: Any, args: argparse.Namespace, rec: Recorder) -> tuple[Any, Any]:
    browser = await p.chromium.launch(executable_path=str(CHROMIUM), headless=True,
                                      args=chromium_args(args.camera, args.mic))
    ctx = await browser.new_context(viewport=PHONE, device_scale_factor=PHONE_SCALE, is_mobile=True, has_touch=True,
                                    permissions=["camera", "microphone"], locale="en-IN")
    page = await ctx.new_page()
    page.on("websocket", rec.on_socket)
    page.on("console", lambda m: rec.note("console", {"level": m.type, "text": m.text[:300]}) if m.type == "error" else None)
    page.on("pageerror", lambda e: rec.note("console", {"level": "pageerror", "text": str(e)[:300]}))
    return browser, page


async def wait_event(rec: Recorder, kind: str, timeout: float, *, after: int = 0) -> dict[str, Any] | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for e in rec.events[after:]:
            if e.get("type") == kind:
                return e
        await asyncio.sleep(0.05)
    return None


async def run_capture(args: argparse.Namespace) -> None:
    rec = Recorder(args.out)
    async with async_playwright() as p:
        browser, page = await open_phone(p, args, rec)
        await page.goto(args.base + "/", wait_until="load")
        await rec.start(page, max_width=PHONE["width"] * PHONE_SCALE, max_height=PHONE["height"] * PHONE_SCALE)
        await page.wait_for_timeout(600)
        await page.click('button[data-go="capture"]')
        rec.note("driver", {"type": "tap_record_a_job"})
        await page.wait_for_function("document.getElementById('cap-video').videoWidth > 0", timeout=20000)
        rec.note("driver", {"type": "camera_live"})
        await page.click("#cap-btn")
        rec.note("driver", {"type": "tap_start_recording"})
        started = await wait_event(rec, "capture", 15)
        if not started:
            raise RuntimeError("the take never started")
        await asyncio.sleep(args.stop_after)
        await page.click("#cap-btn")
        rec.note("driver", {"type": "tap_stop_and_build"})
        done = await wait_event(rec, "procedure", 180)
        failed = next((e for e in rec.events if e.get("type") == "error"), None)
        await page.wait_for_timeout(3500)
        await rec.stop()
        pid = done.get("procedure_id") if done else None
        rec.save(procedure_id=pid, error=failed and failed.get("message"),
                 screen_size=[PHONE["width"] * PHONE_SCALE, PHONE["height"] * PHONE_SCALE])
        print("procedure:", pid, "| error:", failed and failed.get("message"))
        await browser.close()


async def run_guide(args: argparse.Namespace) -> None:
    rec = Recorder(args.out)
    async with async_playwright() as p:
        browser, page = await open_phone(p, args, rec)
        lang = f"&lang={args.lang}" if args.lang else ""
        await page.goto(f"{args.base}/?run={args.procedure}{lang}", wait_until="load")
        await page.wait_for_selector("#library-list .card.fresh", timeout=15000)
        await rec.start(page, max_width=PHONE["width"] * PHONE_SCALE, max_height=PHONE["height"] * PHONE_SCALE)
        await page.wait_for_timeout(700)
        await page.click("#library-list .card.fresh")
        rec.note("driver", {"type": "tap_start_job"})

        started_at = time.monotonic()
        step, step_at, tries, seen = 0, time.monotonic(), 0, 0
        finished = False
        while time.monotonic() - started_at < args.max_seconds:
            for e in rec.events[seen:]:
                if e.get("type") == "speak" and e.get("step") and e["step"] != step:
                    step, step_at, tries = e["step"], time.monotonic(), 0
                if e.get("type") == "verdict" and not e.get("advance") and e.get("verdict") != "wait":
                    step_at = time.monotonic()      # fresh wait after a verdict that did not pass
                if e.get("type") == "complete":
                    finished = True
            seen = len(rec.events)
            if finished or (args.stop_at_step and step >= args.stop_at_step):
                break
            ready = step and await page.evaluate("!document.getElementById('guide-done').disabled")
            if ready and time.monotonic() - step_at >= args.tap_after:
                if tries >= args.max_tries:
                    await page.click("#guide-skip")
                    rec.note("driver", {"type": "tap_skip", "step": step})
                    step_at = time.monotonic()
                    tries = 0
                else:
                    tries += 1
                    await page.click("#guide-done")
                    rec.note("driver", {"type": "tap_done", "step": step, "try": tries})
                    step_at = time.monotonic()
            await asyncio.sleep(0.1)
        await page.wait_for_timeout(args.tail_ms)
        await rec.stop()
        session = next((e.get("session_id") for e in rec.events if e.get("type") == "session"), None)
        rec.save(session_id=session, finished=finished, screen_size=[PHONE["width"] * PHONE_SCALE, PHONE["height"] * PHONE_SCALE])
        print("session:", session, "| finished:", finished)
        await browser.close()


async def run_visit(args: argparse.Namespace) -> None:
    """A page viewed only: no clicks, nothing sent. Optional slow scrolling and mouse moves."""
    rec = Recorder(args.out)
    w, h = (int(v) for v in args.viewport.split("x"))
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=str(CHROMIUM), headless=True)
        ctx = await browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=args.scale, locale="en-IN")
        page = await ctx.new_page()
        await page.goto(args.base + args.path, wait_until="networkidle")
        if args.zoom != 1:
            # Screencast frames come at CSS size whatever the device scale, so a
            # sharp 1080p picture needs a 1080p viewport with the page zoomed.
            await page.evaluate(f"document.documentElement.style.zoom = {args.zoom}")
        await page.wait_for_timeout(args.settle_ms)
        await rec.start(page, max_width=round(w * args.scale), max_height=round(h * args.scale))
        offsets = [int(v) for v in args.scroll.split(",")] if args.scroll else []
        hovers = [int(v) for v in args.hover.split(",")] if args.hover else []
        targets = [v for v in args.to.split(",") if v] if args.to else []
        legs = max(1, len(offsets), len(hovers), len(targets))
        per = args.seconds / legs
        for i in range(legs):
            if i < len(hovers):
                await page.mouse.move(hovers[i], h // 2, steps=20)
            if i < len(targets):
                # For pages that scroll an inner element rather than the window.
                await page.evaluate("(sel) => document.querySelector(sel).scrollIntoView({behavior: 'smooth', block: 'start'})", targets[i])
                await page.wait_for_timeout(int(per * 1000))
            elif i < len(offsets):
                # A slow, even scroll over most of this leg, then a rest on the new view.
                await page.evaluate(
                    """([to, ms]) => new Promise((done) => {
                      const from = window.scrollY, t0 = performance.now();
                      const tick = (now) => {
                        const k = Math.min(1, (now - t0) / ms);
                        window.scrollTo(0, from + (to - from) * k * k * (3 - 2 * k));
                        if (k < 1) requestAnimationFrame(tick); else done();
                      };
                      requestAnimationFrame(tick);
                    })""", [offsets[i], int(per * 600)])
                await page.wait_for_timeout(int(per * 400))
            else:
                await page.wait_for_timeout(int(per * 1000))
        await rec.stop()
        rec.save(path=args.path, screen_size=[round(w * args.scale), round(h * args.scale)])
        await browser.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["capture", "guide", "visit"])
    ap.add_argument("--base", default="http://localhost:8810")
    ap.add_argument("--camera", type=Path)
    ap.add_argument("--mic", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stop-after", type=float, default=16.0)
    ap.add_argument("--procedure")
    ap.add_argument("--lang")
    ap.add_argument("--tap-after", type=float, default=8.0)
    ap.add_argument("--max-tries", type=int, default=2)
    ap.add_argument("--max-seconds", type=float, default=90.0)
    ap.add_argument("--stop-at-step", type=int, default=0, help="end the recording once this step is on screen")
    ap.add_argument("--tail-ms", type=int, default=3500)
    ap.add_argument("--path", default="/")
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--scroll", default="")
    ap.add_argument("--viewport", default="1600x900")
    ap.add_argument("--scale", type=float, default=1.0)
    ap.add_argument("--zoom", type=float, default=1.0)
    ap.add_argument("--to", default="", help="CSS selectors to scroll to, one per leg")
    ap.add_argument("--hover", default="", help="mouse x positions to move through, one per leg")
    ap.add_argument("--settle-ms", type=int, default=800)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    asyncio.run({"capture": run_capture, "guide": run_guide, "visit": run_visit}[args.mode](args))


if __name__ == "__main__":
    main()
