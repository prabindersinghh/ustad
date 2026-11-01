"""Live sessions: guide and capture as continuous, realtime loops.

Guide used to sleep until the worker tapped "done". A live GuideSession instead
runs three things at once while a step is active:

  * a watcher: SAM grounds the step's target on the latest view about once a
    second. The boxes go straight to the phone as a live highlight, and the
    grounding is kept so a "done" only has to pay for the decision.
  * a background judge: every few seconds Muse Spark looks at the grounded
    view. Two confident passes in a row advance the step on their own; a
    danger flag is spoken immediately. Ordinary mistakes are only spoken
    when the worker asks, so nobody is nagged mid-task.
  * a voice channel: the worker's speech streams to Muse Voice; short
    commands ("done", "repeat", "skip", "what's wrong", "where is it") drive
    the session hands-free.

Every background result is tagged with the step generation it was computed
for, and dropped if the step has moved on, so a slow answer can never skip a
worker past a step they did not do.

CaptureSession streams the expert's narration the same way: utterances come
back as live captions and draft step cards while they work, and on stop the
procedure is written from a transcript that already exists, with each
utterance tied to the moment (and keyframe) it was spoken at.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from . import config, db, guide, meta_client, procedures, sam_parse
from .realtime import VoiceRelay, intent_of

log = logging.getLogger("ustad.live")

GUIDE_KEYWORDS = ["done", "repeat", "skip", "next", "what's wrong", "where"]


def inferable(step: dict[str, Any]) -> bool:
    """Can this step be credited from a later picture of the work?

    Never for a step with a hazard (a lockout is not visible afterwards), a
    reading, a timer, or a voice confirmation: those must be done and checked
    on their own, whatever the model claims.
    """
    return not step.get("hazard") and (step.get("check") or "visual") == "visual"


def decode_frame(payload: str) -> bytes:
    if payload.startswith("data:"):
        payload = payload.split(",", 1)[1]
    return base64.b64decode(payload)


class _Socket:
    """Serialised, failure-tolerant sends: several tasks talk to one socket."""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._lock = asyncio.Lock()
        self.closed = False

    async def send(self, obj: dict[str, Any]) -> None:
        if self.closed:
            return
        async with self._lock:
            try:
                await self.ws.send_json(obj)
            except Exception:  # noqa: BLE001 - socket gone; stop talking
                self.closed = True


@dataclass
class _Grounding:
    gen: int
    frame: bytes
    at: float                               # when the frame was captured
    detections: list[sam_parse.Detection]
    seen: float = 0.0                       # last time the view was known unchanged

    def __post_init__(self) -> None:
        self.seen = self.seen or self.at


# ==========================================================================
# Guide
# ==========================================================================

class GuideSession:
    def __init__(self, ws: WebSocket) -> None:
        self.out = _Socket(ws)
        self.proc: dict[str, Any] | None = None
        self.session_id: str | None = None
        self.idx = 0
        self.attempt = 1
        self.gen = 0                      # bumps on every step change
        self.step_started = 0.0
        self.lock = asyncio.Lock()        # one check/advance at a time
        self.busy = False                 # an explicit check is in flight
        self.latest: tuple[bytes, float] | None = None
        self.grounding: _Grounding | None = None
        self.auto_streak = 0
        self.auto_seen_at = 0.0
        self.last_judged_at = 0.0
        self.hazard_spoken_gen = -1
        self.last_flag: dict[str, Any] | None = None
        self.finished = False
        self.skipped: list[int] = []      # step indexes skipped; audited at the end
        self.voice: VoiceRelay | None = None
        self.watcher: asyncio.Task | None = None
        self.judging: asyncio.Task | None = None

    # ---- helpers ---------------------------------------------------------
    @property
    def step(self) -> dict[str, Any]:
        assert self.proc is not None
        return self.proc["steps"][self.idx]

    async def speak(self, text: str, **extra: Any) -> None:
        await self.out.send({"type": "speak", "text": text, **extra})

    async def announce_step(self) -> None:
        assert self.proc is not None
        self.gen += 1
        self.step_started = time.monotonic()
        self.grounding = None
        self.auto_streak = 0
        self.last_flag = None
        step = self.step
        prefix = f"Warning. {step['hazard']} " if step.get("hazard") else ""
        spec = guide.reading_spec(step)
        kind = step.get("check") or "visual"
        duration = int(step.get("duration_s") or 0)
        suffix = ""
        if spec:
            suffix = f" It should read {spec}."
        if kind == "timed" and duration:
            suffix += f" Timer: {duration} seconds."
        if kind == "confirm":
            suffix += " Say done when you've checked it."
        await self.speak(
            prefix + step["instruction"] + suffix,
            step=self.idx + 1, of=len(self.proc["steps"]),
            title=step.get("title", ""), target=step.get("target_phrase", ""),
            kind=kind, reading=spec, duration=duration if kind == "timed" else 0,
        )

    def _upcoming(self) -> list[dict[str, Any]]:
        assert self.proc is not None
        return self.proc["steps"][self.idx + 1: self.idx + 4]

    def _timer_left(self) -> float:
        step = self.step
        if (step.get("check") or "visual") != "timed":
            return 0.0
        return max(0.0, float(step.get("duration_s") or 0) - (time.monotonic() - self.step_started))

    async def _begin(self, proc: dict[str, Any], session_id: str, idx: int, resumed: bool) -> None:
        self.proc, self.session_id, self.idx, self.attempt = proc, session_id, idx, 1
        msg: dict[str, Any] = {
            "type": "session", "session_id": session_id, "title": proc["title"],
            "steps": len(proc["steps"]), "voice": config.VOICE, "auto": config.AUTO_ADVANCE,
        }
        if resumed:
            msg["resumed_at"] = idx + 1
        await self.out.send(msg)
        await self.announce_step()
        if self.watcher is None:
            self.watcher = asyncio.create_task(self._watch(), name="guide-watch")

    # ---- inbound ---------------------------------------------------------
    async def on_text(self, msg: dict[str, Any]) -> bool:
        """Handle one JSON message. Returns False when the session should end."""
        kind = msg.get("type")
        if kind == "start":
            proc = db.get_procedure(msg.get("procedure_id", ""))
            if not proc:
                await self.out.send({"type": "error", "message": "Unknown procedure"})
                return True
            sid = db.start_session(proc["id"], msg.get("worker"), "guide")
            await self._begin(proc, sid, 0, resumed=False)

        elif kind == "resume":
            # The server, not the client, decides where the worker is: resume
            # just past the last step that passed.
            sid = msg.get("session_id")
            record = db.session_log(sid) if sid else {"session": None, "events": []}
            if not record["session"]:
                await self.out.send({"type": "error", "message": "Unknown session"})
                return True
            proc = db.get_procedure(record["session"]["procedure_id"])
            if not proc:
                await self.out.send({"type": "error", "message": "Unknown procedure"})
                return True
            passed = [e["step_index"] for e in record["events"]
                      if e["verdict"] in ("pass", "inferred", "confirmed")]
            idx = (max(passed) + 1) if passed else 0
            if idx >= len(proc["steps"]):
                self.session_id = sid
                await self.out.send({"type": "complete", "session_id": sid})
                return True
            await self._begin(proc, sid, idx, resumed=True)

        elif kind == "watch":
            self.latest = (decode_frame(msg["data"]), time.monotonic())

        elif kind == "still":
            # The phone saw no change in the view: the grounding we already
            # paid for is still current, so refresh it instead of calling SAM.
            if self.grounding and self.grounding.gen == self.gen:
                self.grounding.seen = time.monotonic()

        elif kind == "check":
            frame = decode_frame(msg["data"]) if msg.get("data") else None
            asyncio.create_task(self.check(frame, said=msg.get("said") or "done", source="tap"))

        elif kind == "repeat":
            if self.proc is not None:
                self.gen += 1  # restart the step's clock
                await self.announce_step()

        elif kind == "skip":
            await self.skip()

        elif kind == "abort":
            # Leaving the screen after the last step also sends abort; never
            # let that overwrite a completed session.
            if self.session_id and not (db.session_log(self.session_id)["session"] or {}).get("finished_at"):
                db.finish_session(self.session_id, "aborted")
            return False
        return True

    async def on_audio(self, pcm: bytes) -> None:
        if not config.VOICE or self.proc is None:
            return
        if self.voice is None:
            self.voice = VoiceRelay(self._on_voice, keywords=GUIDE_KEYWORDS)
            self.voice.start()
        self.voice.feed(pcm)

    # ---- voice -------------------------------------------------------------
    async def _on_voice(self, text: str, final: bool) -> None:
        await self.out.send({"type": "heard", "text": text, "final": final})
        if self.proc is None or self.finished:
            return
        if not final:
            # A command word shows up in the partials ~0.3 s after it's said,
            # while the final waits for trailing silence (~2-3 s). Act on a
            # partial "done" once it has held still for a moment.
            if intent_of(text) == "done":
                self._partial = text
                stamp = time.monotonic()
                self._partial_at = stamp
                asyncio.create_task(self._fire_if_stable(text, stamp))
            return
        if time.monotonic() - getattr(self, "_fired_at", 0.0) < 4.0 and intent_of(text) == "done":
            return                                # the partial already triggered it
        intent = intent_of(text)
        log.info("voice %r -> %s", text, intent)
        if intent == "done":
            self._fired_at = time.monotonic()
            asyncio.create_task(self.check(None, said=text, source="voice"))
        elif intent == "repeat":
            self.gen += 1
            await self.announce_step()
        elif intent == "skip":
            await self.skip()
        elif intent == "why":
            if self.last_flag:
                await self.speak(f"{self.last_flag.get('what_is_wrong', '')}. {self.last_flag.get('how_to_fix', '')}".strip(". "),
                                 step=self.idx + 1, of=len(self.proc["steps"]))
            else:
                await self.speak("Nothing is wrong so far. Say done when the step is finished.")
        elif intent == "where":
            await self.point()

    async def _fire_if_stable(self, text: str, stamp: float) -> None:
        await asyncio.sleep(0.45)
        if getattr(self, "_partial_at", 0.0) != stamp:
            return                               # a newer partial came in
        if time.monotonic() - getattr(self, "_fired_at", 0.0) < 4.0:
            return
        self._fired_at = time.monotonic()
        log.info("voice partial %r -> done", text)
        await self.check(None, said=text, source="voice")

    async def point(self) -> None:
        """Say where the target is, from the latest live grounding."""
        target = self.step.get("target_phrase") or "the part"
        g = self.grounding
        if g and g.gen == self.gen and g.detections:
            best = max(g.detections, key=lambda d: (d.confidence or 0, d.area_fraction))
            where = best.describe().split(",")[0]
            await self.out.send({"type": "pulse"})
            await self.speak(f"The {target} is at the {where} of your view. It's highlighted.")
        else:
            await self.speak(f"I can't see the {target} yet. Point the camera at the work.")

    # ---- watcher -------------------------------------------------------------
    async def _watch(self) -> None:
        last_ground_frame_at = 0.0
        last_ground = 0.0
        last_judge = 0.0
        inflight: set[asyncio.Task] = set()
        budget_checked = 0.0
        try:
            while not self.finished and not self.out.closed:
                await asyncio.sleep(0.1)
                if self.proc is None or self.latest is None:
                    continue
                frame, frame_at = self.latest
                now = time.monotonic()

                # Budget guard: live watching is the expensive part (SAM per
                # frame). Near the cap it switches off and the session carries
                # on with tap/voice checks only.
                if now - budget_checked > 20:
                    budget_checked = now
                    spent = await asyncio.to_thread(db.usage_total)
                    if spent >= config.BUDGET_USD * 0.95:
                        log.warning("spend $%.2f is near the $%.0f budget; live watching off", spent, config.BUDGET_USD)
                        await self.out.send({"type": "budget", "spent": round(spent, 2), "budget": config.BUDGET_USD})
                        return

                # 1. ground the newest frame (live highlight + ready grounding).
                # A SAM round trip takes ~2 s, so up to WATCH_GROUND_PARALLEL
                # run at once; that roughly doubles how often the box moves.
                inflight = {t for t in inflight if not t.done()}
                kind = self.step.get("check") or "visual"
                if kind == "confirm" or not self.step.get("target_phrase"):
                    continue                      # nothing to see: no SAM, no background judge
                if (frame_at > last_ground_frame_at
                        and now - last_ground >= config.WATCH_GROUND_INTERVAL_S
                        and len(inflight) < config.WATCH_GROUND_PARALLEL):
                    last_ground, last_ground_frame_at = now, frame_at
                    inflight.add(asyncio.create_task(self._ground(self.gen, self.step, frame, frame_at)))

                # 2. background judge on the grounded frame. A confirmation
                # (second pass) must look at a newer view than the first.
                # Confirmation needs a newer view, or the same view held still
                # for a moment. A routine look only happens on a view not yet
                # judged, so a camera resting on the bench costs nothing.
                g = self.grounding
                if self.auto_streak > 0:
                    due = g is not None and (g.at > self.auto_seen_at or g.seen - self.auto_seen_at >= 1.5)
                else:
                    due = (g is not None and g.at > self.last_judged_at
                           and now - last_judge >= config.WATCH_JUDGE_INTERVAL_S)
                if (config.AUTO_ADVANCE and due and not self.busy and g.gen == self.gen
                        and (self.judging is None or self.judging.done())
                        and now - self.step_started >= config.WATCH_JUDGE_AFTER_S
                        and self._timer_left() == 0):    # a timed step can't finish early
                    last_judge = now
                    self.judging = asyncio.create_task(self._background_judge(g))
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            log.exception("watcher crashed")
        finally:
            for t in inflight:
                t.cancel()

    async def _ground(self, gen0: int, step: dict[str, Any], frame: bytes, frame_at: float) -> None:
        dets = await asyncio.to_thread(guide.ground, step, frame)
        if gen0 != self.gen:
            return
        # Results can land out of order with two in flight: keep the newest.
        if self.grounding and self.grounding.gen == gen0 and self.grounding.at >= frame_at:
            return
        self.grounding = _Grounding(gen0, frame, frame_at, dets)
        await self.out.send({
            "type": "track", "step": self.idx + 1,
            "target": step.get("target_phrase", ""),
            "detections": [d.to_dict() for d in dets],
        })

    async def _background_judge(self, g: _Grounding) -> None:
        gen0, step, attempt = g.gen, self.step, self.attempt
        self.last_judged_at = g.at
        try:
            action = await asyncio.to_thread(
                guide.judge_step, step, g.frame, attempt=attempt, detections=g.detections,
                worker_said="(nothing; this is a background look while they work)",
                upcoming=self._upcoming(), effort=config.WATCH_EFFORT,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("background judge failed: %s", exc)
            return
        if gen0 != self.gen or self.busy or self.finished:
            return
        detail = action["detail"]
        if action["tool"] == "verify_step" and (detail.get("confidence") or 0) >= config.AUTO_CONFIDENCE:
            self.auto_streak += 1
            if self.auto_streak == 1:
                self.auto_seen_at = g.at
                await self.out.send({"type": "auto", "state": "looks_done"})
                return                      # confirm on a newer frame
            async with self.lock:
                if gen0 != self.gen or self.busy:
                    return
                await self._finish(action, g.frame, source="auto")
            return
        self.auto_streak = 0
        if action["tool"] == "flag_error" and detail.get("severity") == "danger" and self.hazard_spoken_gen != self.gen:
            self.hazard_spoken_gen = self.gen
            await self.out.send({"type": "hazard", "message": detail.get("what_is_wrong", "")})
            await self.speak(action.get("say") or "Stop. That is unsafe.", step=self.idx + 1,
                             of=len(self.proc["steps"]))
        else:
            await self.out.send({"type": "auto", "state": "watching"})

    # ---- explicit check ------------------------------------------------------
    async def check(self, frame: bytes | None, *, said: str, source: str) -> None:
        if self.proc is None or self.session_id is None:
            await self.out.send({"type": "error", "message": "Session not started"})
            return
        if self.busy or self.finished:
            return
        self.busy = True
        t0 = time.monotonic()
        await self.out.send({"type": "checking", "source": source})
        try:
            async with self.lock:
                gen0, step = self.gen, self.step
                kind = step.get("check") or "visual"

                # A timed step is done when its time is up, not before.
                left = self._timer_left()
                if left > 2:
                    await self.speak(f"{int(left)} seconds left on the timer.", step=self.idx + 1,
                                     of=len(self.proc["steps"]))
                    await self.out.send({"type": "verdict", "verdict": "wait", "say": "", "advance": False,
                                         "detail": {}, "step": self.idx + 1, "source": source})
                    return

                # Some checks can't be seen (listen for the click, smell for
                # solvent). The operator's word is the record, and it says so.
                if kind == "confirm":
                    action = {"tool": "confirm", "say": "Got it.", "verdict": "confirmed",
                              "detail": {"reason": f"Operator confirmed by voice: \"{said}\" (not visually checkable)"},
                              "latency_ms": int((time.monotonic() - t0) * 1000), "grounding": "Grounding: not applicable.",
                              "detections": [], "backend": "none"}
                    await self._finish(action, self.latest[0] if self.latest else b"", source=source)
                    return

                dets = None
                g = self.grounding
                if frame is None:
                    # Prefer the frame we already grounded if it's fresh: the
                    # decision is then the only model call on the hot path.
                    if g and g.gen == gen0 and t0 - g.seen <= config.GROUNDING_FRESH_S:
                        frame, dets = g.frame, g.detections
                    elif self.latest:
                        frame = self.latest[0]
                if frame is None:
                    await self.out.send({"type": "error", "message": "No camera view yet. Point the camera at the work."})
                    return
                action = await asyncio.to_thread(
                    guide.judge_step, step, frame, worker_said=said, attempt=self.attempt, detections=dets,
                    upcoming=self._upcoming(),
                )
                if gen0 != self.gen:
                    return                  # the step moved on (auto-advance) meanwhile
                action["latency_ms"] = int((time.monotonic() - t0) * 1000)   # what the worker felt
                await self._finish(action, frame, source=source)
        except Exception as exc:  # noqa: BLE001
            log.exception("verification failed")
            await self.out.send({"type": "error", "message": str(exc)})
        finally:
            self.busy = False

    async def _finish(self, action: dict[str, Any], frame: bytes, *, source: str) -> None:
        """Record a judged action, tell the worker, and advance or hold. Lock held."""
        assert self.proc is not None and self.session_id is not None
        step = self.step
        action = await asyncio.to_thread(
            guide.record, session_id=self.session_id, step=step, frame=frame,
            action=action, attempt=self.attempt, source=source if source == "auto" else None,
        )
        n = len(self.proc["steps"])
        await self.out.send({"type": "verdict", **action, "step": self.idx + 1, "source": source})
        say = action.get("say") or ""
        if source == "auto" and action["advance"]:
            say = say or "That looks right."
        if say:
            await self.speak(say, step=self.idx + 1, of=n)

        if action["advance"]:
            # The operator may already have gone ahead: the judge reports the
            # furthest later step whose end state it can see. The steps in
            # between are credited as "inferred" (not "pass") with the same
            # photo, so the record says exactly how each one was established.
            # completed_through is the model's claim, so it is advisory: the
            # server stops the jump at the first step that must not be
            # inferred from a later picture (a hazard/lockout, a reading, a
            # timer, a voice confirmation). The operator does that one.
            through = action["detail"].get("completed_through")
            next_idx = self.idx + 1
            if isinstance(through, (int, float)) and self.idx + 1 < int(through) <= n:
                through = int(through)
                credited = []
                for j in range(self.idx + 1, through):
                    if not inferable(self.proc["steps"][j]):
                        break
                    db.log_step_event(
                        self.session_id, j, "inferred",
                        reason=f"End state of step {through} seen while checking step {self.idx + 1}",
                        evidence_path=action.get("evidence_path"), attempt=1,
                        latency_ms=action.get("latency_ms"), backend=action.get("backend"),
                    )
                    credited.append(j)
                if credited:
                    first, last = credited[0] + 1, credited[-1] + 1
                    span = f"Step {first} is" if first == last else f"Steps {first} to {last} are"
                    await self.speak(f"{span} already done.", step=self.idx + 1, of=n)
                    log.info("jumped from step %d past %d on visible end state (model claimed %d)",
                             self.idx + 1, last, through)
                next_idx = self.idx + 1 + len(credited)
            self.idx = next_idx
            self.attempt = 1
            if self.idx >= n:
                await self._complete(frame)
            else:
                await self.announce_step()
        else:
            self.attempt += 1
            self.gen += 1                    # fresh evidence needed after a verdict
            self.auto_streak = 0
            if action["verdict"] == "fail":
                self.last_flag = action["detail"]

    async def _complete(self, frame: bytes | None) -> None:
        """End of the job. If steps were skipped, look at the finished state
        once and credit each skipped step whose end state is visibly there."""
        assert self.proc is not None and self.session_id is not None
        self.finished = True
        outcome, summary = "completed", "Procedure complete. Well done."
        frame = frame or (self.latest[0] if self.latest else None)
        auditable = [i for i in self.skipped if inferable(self.proc["steps"][i])]
        if self.skipped and frame and auditable:
            steps = [self.proc["steps"][i] for i in auditable]
            try:
                verdicts = await asyncio.to_thread(guide.audit_skipped, steps, frame)
            except Exception as exc:  # noqa: BLE001
                log.warning("final audit failed: %s", exc)
                verdicts = {}
            covered = []
            for i in auditable:                   # safety-gated steps are never credited
                v = verdicts.get(i, {})
                if v.get("status") == "satisfied":
                    covered.append(i + 1)
                    db.log_step_event(self.session_id, i, "inferred",
                                      reason=f"[final audit] {v.get('why', 'end state satisfied')}", attempt=1)
            missing = [i + 1 for i in self.skipped if i + 1 not in covered]
            if missing:
                outcome = "completed_with_skips"
                summary = (f"Done, but step{'s' if len(missing) > 1 else ''} "
                           f"{', '.join(map(str, missing))} couldn't be confirmed from the end result.")
            else:
                summary = "Done. The skipped steps check out in the finished result."
            log.info("final audit: skipped %s, covered %s", [i + 1 for i in self.skipped], covered)
        elif self.skipped:
            outcome = "completed_with_skips"
            summary = (f"Done, but step{'s' if len(self.skipped) > 1 else ''} "
                       f"{', '.join(str(i + 1) for i in self.skipped)} were skipped and can't be confirmed.")
        db.finish_session(self.session_id, outcome)
        await self.out.send({"type": "complete", "session_id": self.session_id})
        await self.speak(summary)

    async def skip(self) -> None:
        if self.proc is None or self.session_id is None:
            return
        async with self.lock:
            db.log_step_event(self.session_id, self.idx, "skipped",
                              reason="Manually skipped by operator", attempt=self.attempt)
            self.skipped.append(self.idx)
            self.idx += 1
            self.attempt = 1
            if self.idx >= len(self.proc["steps"]):
                await self._complete(self.latest[0] if self.latest else None)
            else:
                await self.announce_step()

    async def close(self) -> None:
        self.finished = True
        for t in (self.watcher, self.judging):
            if t and not t.done():
                t.cancel()
        if self.voice:
            await self.voice.close()


# ==========================================================================
# Capture
# ==========================================================================

def pcm_to_wav(pcm: bytes, rate: int = 16000) -> bytes:
    """Wrap raw s16le mono PCM in a RIFF/WAVE header (what batch ASR accepts)."""
    hdr = b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVE"
    hdr += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    hdr += b"data" + struct.pack("<I", len(pcm))
    return hdr + pcm


def _mmss(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


class CaptureSession:
    def __init__(self, ws: WebSocket) -> None:
        self.out = _Socket(ws)
        self.reset()
        self.voice: VoiceRelay | None = None

    def reset(self) -> None:
        self.keyframes: list[bytes] = []
        self.frame_times: list[float] = []
        self.turns: list[dict[str, Any]] = []
        self.pcm = bytearray()
        self.audio_blobs: list[bytes] = []
        self.audio_mime = "audio/mp4"
        self.hint: str | None = None
        self.t0 = time.monotonic()
        self.recording = False
        self.paused_at: float | None = None
        self.paused_total = 0.0

    def elapsed(self) -> float:
        """Recording time, excluding pauses: what the job clock should show."""
        now = self.paused_at if self.paused_at is not None else time.monotonic()
        return now - self.t0 - self.paused_total

    async def status(self, message: str) -> None:
        await self.out.send({"type": "status", "message": message})

    async def mark_step(self, source: str) -> None:
        """The expert marked the start of a new step (button or "next step")."""
        at = self.elapsed()
        self.turns.append({"n": None, "text": "[step marker]", "at": at, "frame": max(0, len(self.keyframes) - 1)})
        n = sum(1 for t in self.turns if t["text"] == "[step marker]") + 1
        await self.out.send({"type": "marker", "n": n, "at": _mmss(at), "source": source})

    async def on_text(self, msg: dict[str, Any]) -> bool:
        kind = msg.get("type")
        if kind == "start":
            self.reset()
            self.recording = True
            self.hint = msg.get("hint") or None
            await self.status("Recording. Narrate each step as you do it.")
        elif kind == "pause":
            if self.recording and self.paused_at is None:
                self.paused_at = time.monotonic()
                await self.out.send({"type": "paused", "at": _mmss(self.elapsed())})
        elif kind == "resume":
            if self.paused_at is not None:
                self.paused_total += time.monotonic() - self.paused_at
                self.paused_at = None
                await self.out.send({"type": "resumed", "at": _mmss(self.elapsed())})
        elif kind == "mark":
            if self.recording:
                await self.mark_step("tap")
        elif kind == "frame":
            if self.paused_at is not None:
                return True                       # nothing is captured while paused
            self.keyframes.append(decode_frame(msg["data"]))
            self.frame_times.append(self.elapsed())
            await self.out.send({"type": "frame_ack", "count": len(self.keyframes)})
        elif kind == "audio":                 # whole-recording upload (fallback path)
            self.audio_blobs.append(base64.b64decode(msg["data"]))
            self.audio_mime = msg.get("mime", self.audio_mime)
        elif kind == "stop":
            self.recording = False
            await self.build()
        elif kind == "close":
            return False
        return True

    async def on_audio(self, pcm: bytes) -> None:
        if not self.recording or self.paused_at is not None:
            return
        self.pcm.extend(pcm)
        if not config.VOICE:
            return
        if self.voice is None:
            self.voice = VoiceRelay(self._on_voice)
            self.voice.start()
        self.voice.feed(pcm)

    async def _on_voice(self, text: str, final: bool) -> None:
        await self.out.send({"type": "heard", "text": text, "final": final})
        if not final:
            return
        if self.paused_at is not None:
            return
        # "Next step, open the valve..." marks a step boundary, then narrates.
        low = text.lower().lstrip(" ,.")
        if low.startswith("next step"):
            await self.mark_step("voice")
            text = text.lstrip(" ,.")[len("next step"):].lstrip(" ,.:")
            if not text:
                return
        at = self.elapsed()
        n = sum(1 for t in self.turns if t["n"] is not None) + 1
        turn = {"n": n, "text": text, "at": at, "frame": max(0, len(self.keyframes) - 1)}
        self.turns.append(turn)
        await self.out.send({"type": "draft", "n": turn["n"], "text": text, "at": _mmss(at)})
        asyncio.create_task(self._title(turn))

    async def _title(self, turn: dict[str, Any]) -> None:
        """A short step title for the live card. Text-only, cheap, best effort."""
        try:
            resp = await asyncio.to_thread(
                meta_client.reason,
                "Turn this spoken instruction from a technician into a 2 to 5 word step title "
                "in the imperative. Output only the title, no punctuation at the end.",
                [{"type": "input_text", "text": turn["text"]}],
                effort="minimal", max_output_tokens=400,
            )
            title = (getattr(resp, "output_text", "") or "").strip().strip('".')
            if title and len(title) < 60:
                await self.out.send({"type": "draft_title", "n": turn["n"], "title": title})
        except Exception as exc:  # noqa: BLE001
            log.debug("draft title failed: %s", exc)

    def _narration(self) -> str:
        """Narration with the moment each utterance was spoken, so the writer
        can line words up with the keyframes captured at the same time."""
        return "\n".join(f"[{_mmss(t['at'])}] {t['text']}" for t in self.turns)

    async def build(self) -> None:
        log.info("capture stop: %d keyframes, %d live utterances, %d bytes pcm, %d bytes blob",
                 len(self.keyframes), len(self.turns), len(self.pcm), sum(map(len, self.audio_blobs)))
        if not self.keyframes:
            await self.out.send({"type": "error", "message": "No camera frames reached the server. "
                                 "Keep recording for a few seconds with the camera on the work, then stop."})
            return
        # Let the last utterance finish arriving from the voice stream.
        if self.voice:
            await asyncio.sleep(1.2)

        spoke = any(t["n"] is not None for t in self.turns)
        transcript = self._narration() if spoke else ""
        if not transcript:
            await self.status(f"Captured {len(self.keyframes)} keyframes. Transcribing…")
            audio, name = None, "capture.m4a"
            if len(self.pcm) > 16000:          # > 0.5 s of streamed audio
                audio, name = pcm_to_wav(bytes(self.pcm)), "capture.wav"
            elif self.audio_blobs:
                ext = "m4a" if "mp4" in self.audio_mime or "m4a" in self.audio_mime else "webm"
                audio, name = b"".join(self.audio_blobs), f"capture.{ext}"
            if audio:
                try:
                    transcript = await asyncio.to_thread(meta_client.transcribe, audio, filename=name)
                except Exception:  # noqa: BLE001
                    log.exception("Transcription failed")
                    await self.status("Couldn't transcribe the narration; writing from the video alone.")

        markers = [t for t in self.turns if t["n"] is None]
        if markers and not spoke:
            transcript = (transcript + "\n" if transcript else "") + "\n".join(
                f"[{_mmss(t['at'])}] [step marker]" for t in markers)
        await self.status("Writing the procedure…")
        try:
            proc = await asyncio.to_thread(
                procedures.build_procedure, transcript, self.keyframes,
                hint=self.hint, frame_times=self.frame_times,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Procedure generation failed")
            await self.out.send({"type": "error", "message": str(exc)})
            return
        pid = db.save_procedure(proc["title"], proc["steps"], machine=proc.get("machine"),
                                transcript=transcript)
        await self.out.send({"type": "procedure", "procedure_id": pid, "procedure": proc})

    async def close(self) -> None:
        if self.voice:
            await self.voice.close()
