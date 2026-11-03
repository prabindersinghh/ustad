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
from .realtime import VoiceRelay, intent_of, is_question

log = logging.getLogger("ustad.live")

GUIDE_KEYWORDS = ["done", "repeat", "skip", "next", "what's wrong", "where"]


def inferable(step: dict[str, Any]) -> bool:
    """Can this step be credited from a later picture of the work?

    Never for a step with a hazard (a lockout is not visible afterwards), a
    reading, a timer, or a voice confirmation: those must be done and checked
    on their own, whatever the model claims.
    """
    return not step.get("hazard") and (step.get("check") or "visual") == "visual"


def clean_for_log(value: Any, limit: int = 120) -> str:
    """One short line from untrusted input: control characters escaped, so a
    phone (or anyone) can't forge log lines with embedded newlines."""
    text = value if isinstance(value, str) else repr(value)
    text = "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)
    return text[:limit]


_DIAG_KEYS = ("video", "audio", "ctx", "rate", "path", "mic_chunks", "dark", "hidden",
              "frames", "stills", "streamed", "recorded", "ua")


def log_diag(where: str, session: str | None, msg: dict[str, Any]) -> None:
    """Log a phone's self-report on one bounded line (see Diag in web/app.js)."""
    fields = {k: clean_for_log(msg[k], 140 if k == "ua" else 40) for k in _DIAG_KEYS if k in msg}
    notes = msg.get("notes")
    if isinstance(notes, list):
        fields["notes"] = [clean_for_log(n, 100) for n in notes[:8]]
    log.info("diag %s %s %s", where, clean_for_log(session or "-", 20), fields)


class _RateGate:
    """At most `n` events per `per` seconds. Telemetry must never be a way to
    flood the log."""

    def __init__(self, n: int, per: float) -> None:
        self.n, self.per, self.times = n, per, []

    def allow(self) -> bool:
        now = time.monotonic()
        self.times = [t for t in self.times if now - t < self.per]
        if len(self.times) >= self.n:
            return False
        self.times.append(now)
        return True


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
    context: str = ""                       # hazards/tools also seen, for the judge

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
        self.counts = {"frames": 0, "dark": 0, "audio_bytes": 0}   # logged at close
        self.dark_said = 0.0
        self.diag_gate = _RateGate(6, 60.0)     # the phone sends one every 15 s
        # Ask Ustad
        self.asking: asyncio.Task | None = None
        self.listen_until = 0.0                 # Ask button: the next utterance is a question
        self.ask_gate = _RateGate(8, 60.0)      # each answer is a model call
        self.fails: dict[int, list[str]] = {}   # step index -> what went wrong, for coaching
        self.coached: set[int] = set()

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
            ref=bool(step.get("ref_image")),
        )

    def _reference_msg(self, step: dict[str, Any]) -> dict[str, Any] | None:
        """The expert's photo of this step, with where the part sits in it."""
        rel = step.get("ref_image")
        if not rel:
            return None
        return {"type": "reference", "step": step.get("index", self.idx) + 1,
                "image": "/" + str(rel).lstrip("/"), "box": step.get("ref_box"),
                "target": step.get("target_phrase", ""), "said": step.get("expert_said") or ""}

    def _upcoming(self) -> list[dict[str, Any]]:
        assert self.proc is not None
        return self.proc["steps"][self.idx + 1: self.idx + 4]

    def _timer_left(self) -> float:
        step = self.step
        if (step.get("check") or "visual") != "timed":
            return 0.0
        return max(0.0, float(step.get("duration_s") or 0) - (time.monotonic() - self.step_started))

    async def _begin(self, proc: dict[str, Any], session_id: str, idx: int, resumed: bool,
                     attempt: int = 1) -> None:
        self.proc, self.session_id, self.idx, self.attempt = proc, session_id, idx, attempt
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
            sid = db.start_session(proc["id"], msg.get("worker"), "guide", version=proc.get("version"))
            await self._begin(proc, sid, 0, resumed=False)

        elif kind == "resume":
            # The server, not the client, decides where the worker is: resume
            # just past the last step that was finished (passed, inferred,
            # confirmed or skipped), with the attempt count and skips intact,
            # so a dropped connection costs the worker nothing.
            sid = msg.get("session_id")
            record = db.session_log(sid) if sid else {"session": None, "events": []}
            if not record["session"]:
                await self.out.send({"type": "error", "message": "Unknown session"})
                return True
            proc = db.get_procedure(record["session"]["procedure_id"])
            if not proc:
                await self.out.send({"type": "error", "message": "Unknown procedure"})
                return True
            if record["session"].get("finished_at"):
                self.session_id = sid
                await self.out.send({"type": "complete", "session_id": sid})
                return True
            events = record["events"]
            done = [e["step_index"] for e in events if e["verdict"] in db.DONE_VERDICTS]
            idx = (max(done) + 1) if done else 0
            self.skipped = sorted({e["step_index"] for e in events if e["verdict"] == "skipped"})
            for e in events:
                if e["verdict"] == "fail" and e["step_index"] == idx:
                    self.fails.setdefault(idx, []).append(e["reason"] or "")
            if idx >= len(proc["steps"]):
                self.proc, self.session_id = proc, sid
                await self._complete(None)
                return True
            tries = sum(1 for e in events if e["step_index"] == idx and e["verdict"] in db.CHECK_VERDICTS)
            log.info("guide resume: session %s at step %d (attempt %d)", sid, idx + 1, tries + 1)
            await self._begin(proc, sid, idx, resumed=True, attempt=tries + 1)

        elif kind == "ping":
            await self.out.send({"type": "pong"})

        elif kind == "listen":
            # The Ask button: whatever the worker says next is a question.
            self.listen_until = time.monotonic() + 12

        elif kind == "ask":
            text = " ".join(str(msg.get("text") or "").split())[:300]
            if text:
                await self.ask(text, source="typed")

        elif kind == "show":
            await self.point()

        elif kind == "watch":
            self.latest = (decode_frame(msg["data"]), time.monotonic())
            self.counts["frames"] += 1

        elif kind == "dark":
            # The phone's view is black (lens covered, camera muted by iOS).
            # Say so out loud, at most every 12 s, rather than check nothing.
            self.counts["dark"] += 1
            now = time.monotonic()
            if now - self.dark_said > 12:
                self.dark_said = now
                await self.out.send({"type": "notice",
                                     "text": "I can't see anything. Check the camera lens is clear."})

        elif kind == "diag":
            if self.diag_gate.allow():
                log_diag("guide", self.session_id, msg)

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
        self.counts["audio_bytes"] += len(pcm)
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
        words = text.lower().split()
        # "What's next?" holds "next", but it's a question, not a "done".
        if intent == "done" and words and words[0].strip("',.?") in ("how", "what", "what's", "which", "why",
                                                                      "where", "when", "can", "should", "do", "does"):
            intent = None
        # "Where is it" points at the target; "where's the spanner" needs Spark.
        if intent == "where" and len(words) > 3:
            intent = None
        listening = time.monotonic() < self.listen_until
        if listening and intent not in ("done", "skip", "repeat"):
            intent = None
        log.info("voice %r -> %s", text, intent or ("ask" if listening or is_question(text) else "-"))
        if intent is None and (listening or is_question(text)):
            self.listen_until = 0.0
            await self.ask(text, source="voice")
        elif intent == "done":
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
        """"Where is it?" / "Show me": highlight the target in the live view
        and show the expert's own photo of the step with the part boxed."""
        if self.proc is None:
            return
        step = self.step
        target = step.get("target_phrase") or "the part"
        ref = self._reference_msg(step)
        if ref:
            await self.out.send(ref)
        g = self.grounding
        if g and g.gen == self.gen and g.detections:
            best = max(g.detections, key=lambda d: (d.confidence or 0, d.area_fraction))
            where = best.describe().split(",")[0]
            await self.out.send({"type": "pulse"})
            await self.speak(f"The {target} is at the {where} of your view. It's highlighted.", aside=True)
        elif ref and isinstance(ref.get("box"), dict):
            box = ref["box"]
            try:
                where = sam_parse.Detection(**{k: box[k] for k in (
                    "frame", "ordinal", "x1", "y1", "x2", "y2", "img_w", "img_h")}).describe().split(",")[0]
            except (KeyError, TypeError):
                where = ""
            hint = f", at the {where} of the expert's view" if where else ""
            await self.speak(f"I can't see the {target} in your view yet. Here it is in the expert's video{hint}. "
                             "Point the camera there.", aside=True)
        elif ref:
            await self.speak(f"I can't see the {target} yet. This is how the expert's view looked. Point the camera there.", aside=True)
        else:
            await self.speak(f"I can't see the {target} yet. Point the camera at the work.", aside=True)

    # ---- Ask Ustad -----------------------------------------------------------
    async def ask(self, question: str, *, source: str, coach: bool = False) -> None:
        """Answer a question (or coach after repeated fails) with one Spark
        tool call, then act on it: speak, point, show the expert's photo,
        run a check, or call a supervisor."""
        if self.proc is None or self.session_id is None or self.finished:
            return
        if self.asking and not self.asking.done():
            return                                   # one answer at a time
        if not self.ask_gate.allow():
            await self.speak("Give me a moment, then ask again.")
            return
        # Its own task: the voice reader that heard the question keeps
        # transcribing while Spark thinks.
        self.asking = asyncio.create_task(self._answer(question, source=source, coach=coach))

    async def _answer(self, question: str, *, source: str, coach: bool) -> None:
        assert self.proc is not None and self.session_id is not None
        gen0, idx = self.gen, self.idx
        await self.out.send({"type": "thinking", "question": question, "coach": coach})
        g = self.grounding
        frame = g.frame if g and g.gen == gen0 else (self.latest[0] if self.latest else None)
        grounding = ""
        if g and g.gen == gen0:
            grounding = sam_parse.summarise(self.step.get("target_phrase") or "target", g.detections)
            grounding += ("\n" + g.context) if g.context else ""
        try:
            ans = await asyncio.to_thread(
                guide.answer_question, self.proc, idx, question, frame,
                grounding=grounding, what_went_wrong=self.fails.get(idx), coach=coach)
        except Exception as exc:  # noqa: BLE001
            log.warning("ask failed: %s", exc)
            await self.out.send({"type": "answer", "say": "", "failed": True})
            await self.speak("Sorry, I couldn't work that out. Ask me again.")
            return
        if self.finished:
            return
        log.info("ask (%s) %r -> %s in %dms%s", source if not coach else "coach", question[:80], ans["tool"],
                 ans["latency_ms"], f" point_at={ans['point_at']!r}" if ans["point_at"] else "")
        n = len(self.proc["steps"])
        q = "(coaching after repeated fails)" if coach else question

        if ans["tool"] == "check_now" and gen0 == self.gen:
            await self.out.send({"type": "answer", "say": ans["say"], "tool": "check_now"})
            await self.check(None, said=question, source="voice")
            return
        if ans["tool"] == "escalate":
            db.log_step_event(self.session_id, idx, "help",
                              reason=f"Q: {clean_for_log(q, 200)} | {ans['reason'] or 'needs a supervisor'}")
            await self.out.send({"type": "answer", "say": ans["say"], "tool": "escalate",
                                 "reason": ans["reason"] or ""})
            await self.speak(ans["say"] or "Stop here and get your supervisor.", step=idx + 1, of=n, aside=True)
            return

        db.log_step_event(self.session_id, idx, "asked",
                          reason=f"Q: {clean_for_log(q, 200)} | A: {clean_for_log(ans['say'], 300)}",
                          latency_ms=ans["latency_ms"])
        await self.out.send({"type": "answer", "say": ans["say"], "tool": "answer", "coach": coach,
                             "latency_ms": ans["latency_ms"]})
        if ans["say"]:
            await self.speak(ans["say"], step=idx + 1, of=n, aside=True)
        if gen0 != self.gen:
            return                                   # the step moved on while thinking
        if ans["point_at"] and frame:
            label = ans["point_at"]
            try:
                dets = await asyncio.to_thread(meta_client.segment, label, frame)
                # A wordy phrase ("white cap on the bottle in your hand") can
                # miss where the step's own target phrase, already proven on
                # the expert's footage, hits. Fall back to it when they overlap.
                target = self.step.get("target_phrase") or ""
                if not dets and target and target != label and set(target.lower().split()) & set(label.lower().split()):
                    label = target
                    dets = await asyncio.to_thread(meta_client.segment, label, frame)
            except Exception as exc:  # noqa: BLE001
                log.warning("point grounding failed: %s", exc)
                dets = []
            ans["point_at"] = label
            await self.out.send({"type": "point", "label": ans["point_at"],
                                 "detections": [d.to_dict() for d in dets]})
            if not dets and self.step.get("ref_image"):
                ans["show_reference"] = True         # not in view: show where it was for the expert
        if ans["show_reference"] or coach:
            ref = self._reference_msg(self.step)
            if ref:
                await self.out.send(ref)

    async def _maybe_coach(self, idx: int) -> None:
        """After repeated fails on one step, coach once, unasked."""
        if idx in self.coached or len(self.fails.get(idx, [])) < config.COACH_AFTER_FAILS:
            return
        self.coached.add(idx)
        await asyncio.sleep(1.5)                     # let the correction be heard first
        if self.idx == idx and not self.finished:
            await self.ask("", source="coach", coach=True)

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
        scene = await asyncio.to_thread(guide.ground_scene, step, frame)
        if gen0 != self.gen:
            return
        # Results can land out of order with two in flight: keep the newest.
        if self.grounding and self.grounding.gen == gen0 and self.grounding.at >= frame_at:
            return
        dets = scene["target"]
        self.grounding = _Grounding(gen0, frame, frame_at, dets, context=guide.scene_summary(scene))
        await self.out.send({
            "type": "track", "step": self.idx + 1,
            "target": step.get("target_phrase", ""),
            "detections": [d.to_dict() for d in dets],
            "objects": [{"role": o["role"], "label": o["label"],
                         "detections": [d.to_dict() for d in o["detections"]]} for o in scene["objects"]],
        })
        # Live safety: a hand inside (or just outside) a hazard's box is
        # warned about at once, not when the operator next says "done".
        for o in scene["objects"]:
            if o["role"] == "hazard" and guide.hand_near(scene["hands"], o["detections"]):
                now = time.monotonic()
                if now - getattr(self, "_proximity_at", 0.0) > 6:
                    self._proximity_at = now
                    log.warning("hand near %r on step %d", o["label"], self.idx + 1)
                    await self.out.send({"type": "proximity", "hazard": o["label"]})
                    await self.speak(f"Hand near the {o['label']}. Keep clear.",
                                     step=self.idx + 1, of=len(self.proc["steps"]))
                break

    async def _background_judge(self, g: _Grounding) -> None:
        gen0, step, attempt = g.gen, self.step, self.attempt
        self.last_judged_at = g.at
        try:
            action = await asyncio.to_thread(
                guide.judge_step, step, g.frame, attempt=attempt, detections=g.detections, context=g.context,
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

                dets, ctx = None, ""
                g = self.grounding
                if frame is None:
                    # Prefer the frame we already grounded if it's fresh: the
                    # decision is then the only model call on the hot path.
                    if g and g.gen == gen0 and t0 - g.seen <= config.GROUNDING_FRESH_S:
                        frame, dets, ctx = g.frame, g.detections, g.context
                    elif self.latest:
                        frame = self.latest[0]
                if frame is None:
                    await self.out.send({"type": "error", "message": "No camera view yet. Point the camera at the work."})
                    return
                action = await asyncio.to_thread(
                    guide.judge_step, step, frame, worker_said=said, attempt=self.attempt, detections=dets, context=ctx,
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
                self.fails.setdefault(self.idx, []).append(
                    action["detail"].get("what_is_wrong") or action.get("say") or "")
                asyncio.create_task(self._maybe_coach(self.idx))

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

        # Nothing left behind: a spanner left inside a guard is a classic,
        # costly incident. Look for this job's tools (or any hand tool) in the
        # final view before the machine goes back into service.
        if frame:
            left = await asyncio.to_thread(guide.left_behind, self.proc, frame)
            if left:
                db.log_step_event(self.session_id, len(self.proc["steps"]) - 1, "notice",
                                  reason=f"[left behind] {left} still in the work area at the end of the job")
                await self.out.send({"type": "hazard", "message": f"{left} still in the work area"})
                summary = f"Before you restart: the {left} is still in the work area. " + summary
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
        # One line per guide session: enough to tell "the phone sent nothing"
        # from "the server ignored it" without a debugger on the phone.
        log.info("guide close: session %s, step %d, %d frames, %d dark, %d audio bytes",
                 self.session_id, self.idx + 1, self.counts["frames"], self.counts["dark"],
                 self.counts["audio_bytes"])
        for t in (self.watcher, self.judging, self.asking):
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


# A recording outlives its socket. Phones on a shop floor drop Wi-Fi, so a
# take is kept here by id while the phone keeps recording offline, and a new
# socket rejoins it (see CaptureSocket). Takes left alone this long are freed.
TAKE_TTL_S = 20 * 60
MAX_TAKES = 12                              # takes held in memory at once
MAX_TAKES_PER_CLIENT = 4                    # per client address (a factory NAT shares one)
MAX_KEYFRAMES = 400                         # per take
MAX_GAP_BYTES = 16000 * 2 * 600             # 10 min of offline narration per take
_TAKES: dict[str, "CaptureSession"] = {}


TAKE_TTL_EMPTY_S = 120                      # a detached take with almost nothing in it
_take_gates: dict[str, _RateGate] = {}      # new takes per client: 6 per 10 min


def _client_of(ws: WebSocket) -> str:
    """Who opened this socket. Behind the Cloudflare tunnel every peer is
    the local cloudflared, which sets CF-Connecting-IP to the phone's real
    address (Cloudflare overwrites any value a client sends). The header is
    trusted only from loopback: a direct LAN client is its own address."""
    host = ws.client.host if getattr(ws, "client", None) else "?"
    if host in ("127.0.0.1", "::1"):
        ip = ws.headers.get("cf-connecting-ip") if hasattr(ws, "headers") else None
        if ip:
            return ip
    return host


def _admit_take(owner: str) -> bool:
    """Make room for a new take from `owner`, or say no.

    Takes past the TTL are freed. A take still inside its TTL is someone's
    recording waiting for their phone to come back, so it is never evicted
    for anyone else: only the same client's own oldest detached take can
    give way. When full, new takes are refused (a busy server, not lost work).
    """
    now = time.monotonic()
    if len(_take_gates) > 2000:
        _take_gates.clear()                      # bounded; a reset only loosens limits briefly
    gate = _take_gates.setdefault(owner, _RateGate(6, 600.0))
    if not gate.allow():
        return False
    for cid, take in list(_TAKES.items()):
        # A real recording keeps its full grace period; a near-empty one
        # (a few seconds, or junk holding a slot) is freed after 2 minutes.
        ttl = TAKE_TTL_S if len(take.keyframes) >= 5 else TAKE_TTL_EMPTY_S
        if take.detached_at and now - take.detached_at > ttl:
            _TAKES.pop(cid, None)
    mine = sorted((t.detached_at, cid) for cid, t in _TAKES.items() if t.owner == owner and t.detached_at)
    while mine and (len(_TAKES) >= MAX_TAKES
                    or sum(1 for t in _TAKES.values() if t.owner == owner) >= MAX_TAKES_PER_CLIENT):
        _TAKES.pop(mine.pop(0)[1], None)
    return (len(_TAKES) < MAX_TAKES
            and sum(1 for t in _TAKES.values() if t.owner == owner) < MAX_TAKES_PER_CLIENT)


class CaptureSocket:
    """One capture WebSocket. It either starts a take or rejoins one:

      -> {"type":"rejoin","capture_id":"...","at":<phone's job clock>,"paused":bool}
      <- {"type":"rejoined","frames":N,"state":"recording"|"building"|"done"}
         or {"type":"rejoin_failed"} when the server no longer has the take.
    """

    def __init__(self, ws: WebSocket) -> None:
        self.out = _Socket(ws)
        self.take: CaptureSession | None = None
        self.owner = _client_of(ws)

    async def on_text(self, msg: dict[str, Any]) -> bool:
        kind = msg.get("type")
        if kind == "ping":
            await self.out.send({"type": "pong"})
            return True
        if kind == "rejoin":
            take = _TAKES.get(str(msg.get("capture_id") or ""))
            if take is None:
                await self.out.send({"type": "rejoin_failed"})
                return True
            self.take = take
            await take.attach(self.out, msg)
            return True
        if self.take is None:
            if not _admit_take(self.owner):
                log.warning("capture refused for %s: %d takes held", clean_for_log(self.owner, 60), len(_TAKES))
                await self.out.send({"type": "error", "message": "The server is busy with other recordings. Try again shortly."})
                return False
            self.take = CaptureSession(self.out)
            self.take.owner = self.owner
            _TAKES[self.take.capture_id] = self.take
        return await self.take.on_text(msg)

    async def on_audio(self, pcm: bytes) -> None:
        if self.take:
            await self.take.on_audio(pcm)

    async def close(self) -> None:
        if self.take:
            await self.take.detach(self.out)


class CaptureSession:
    def __init__(self, out: "_Socket | WebSocket") -> None:
        self.out = out if isinstance(out, _Socket) else _Socket(out)
        self.capture_id = db.new_id()
        self.reset()
        self.voice: VoiceRelay | None = None
        self.diag_gate = _RateGate(6, 60.0)
        self.state = "idle"                   # idle | recording | building | done
        self.result: dict[str, Any] | None = None
        self.detached_at = 0.0
        self.rejoins = 0
        self.owner = "?"                      # client address that started it

    def reset(self) -> None:
        self.keyframes: list[bytes] = []
        self.frame_times: list[float] = []
        self.turns: list[dict[str, Any]] = []
        self.pcm = bytearray()
        self.gaps: list[tuple[float, bytes]] = []   # (at, pcm) recorded while offline
        self.audio_blobs: list[bytes] = []
        self.audio_mime = "audio/mp4"
        self.hint: str | None = None
        self.t0 = time.monotonic()
        self.recording = False
        self.paused_at: float | None = None
        self.paused_total = 0.0

    # ---- connection ------------------------------------------------------
    async def attach(self, out: _Socket, msg: dict[str, Any]) -> None:
        """A new socket picked this take up after a drop. The phone's job
        clock is the truth for what happened offline (it may have paused)."""
        self.out, self.detached_at = out, 0.0
        self.rejoins += 1
        at = msg.get("at")
        if self.recording and isinstance(at, (int, float)) and 0 <= at < 4 * 3600:
            now = time.monotonic()
            paused = bool(msg.get("paused"))
            self.paused_at = now if paused else None
            self.t0 = now - float(at) - self.paused_total
        log.info("capture %s rejoined (%d): %s, %d keyframes so far", self.capture_id, self.rejoins,
                 self.state, len(self.keyframes))
        await self.out.send({"type": "rejoined", "capture_id": self.capture_id, "state": self.state,
                             "frames": len(self.keyframes)})
        if self.result:
            await self.out.send(self.result)

    async def detach(self, out: _Socket) -> None:
        if self.out is not out:
            return                            # an older socket closing after a rejoin
        self.detached_at = time.monotonic()
        if self.voice:
            voice, self.voice = self.voice, None
            await voice.close()
        if self.state in ("recording", "building"):
            log.info("capture %s: socket dropped while %s; keeping the take", self.capture_id, self.state)
        elif self.state == "done" or not self.keyframes:
            _TAKES.pop(self.capture_id, None)

    def elapsed(self) -> float:
        """Recording time, excluding pauses: what the job clock should show."""
        now = self.paused_at if self.paused_at is not None else time.monotonic()
        return now - self.t0 - self.paused_total

    async def status(self, message: str) -> None:
        await self.out.send({"type": "status", "message": message})

    async def mark_step(self, source: str, at: float | None = None, quiet: bool = False) -> None:
        """The expert marked the start of a new step (button or "next step").
        A mark made offline arrives later with the job-clock time it was made."""
        at = self.elapsed() if at is None else max(0.0, min(float(at), self.elapsed()))
        frame = max(0, sum(1 for t in self.frame_times if t <= at) - 1)
        self.turns.append({"n": None, "text": "[step marker]", "at": at, "frame": frame})
        self.turns.sort(key=lambda t: t["at"])
        n = sum(1 for t in self.turns if t["text"] == "[step marker]") + 1
        if not quiet:
            await self.out.send({"type": "marker", "n": n, "at": _mmss(at), "source": source})

    async def on_text(self, msg: dict[str, Any]) -> bool:
        kind = msg.get("type")
        if kind == "start":
            self.reset()
            self.recording = True
            self.state = "recording"
            self.hint = msg.get("hint") or None
            await self.out.send({"type": "capture", "capture_id": self.capture_id})
            await self.status("Recording. Narrate each step as you do it.")
        elif kind == "diag":
            if self.diag_gate.allow():
                log_diag("capture", None, msg)
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
                at = msg.get("at")
                await self.mark_step("tap", at if isinstance(at, (int, float)) else None,
                                     quiet=bool(msg.get("quiet")))
        elif kind == "frame":
            if self.paused_at is not None and "at" not in msg:
                return True                       # nothing is captured while paused
            # Frames buffered on the phone while offline carry the job-clock
            # time they were taken at; live ones are stamped on arrival.
            if len(self.keyframes) >= MAX_KEYFRAMES:
                return True                       # 10 min at one every 2 s, plus offline slack
            at = self.elapsed()
            if isinstance(msg.get("at"), (int, float)):
                lo = self.frame_times[-1] if self.frame_times else 0.0
                at = min(max(float(msg["at"]), lo), max(at, lo))
            self.keyframes.append(decode_frame(msg["data"]))
            self.frame_times.append(at)
            await self.out.send({"type": "frame_ack", "count": len(self.keyframes)})
        elif kind == "audio_gap":                 # narration recorded while offline
            data = msg.get("data") or ""
            room = MAX_GAP_BYTES - sum(len(p) for _, p in self.gaps)
            # Bounded before decoding: at most the 10 minutes the phone keeps.
            if not isinstance(data, str) or room <= 0 or len(data) > (room * 4) // 3 + 4:
                return True
            try:
                pcm = base64.b64decode(data)
            except (ValueError, TypeError):
                pcm = b""
            if pcm and isinstance(msg.get("at"), (int, float)) and len(self.gaps) < 64:
                self.gaps.append((float(msg["at"]), pcm[:room]))
        elif kind == "audio":                 # whole-recording upload (fallback path)
            self.audio_blobs.append(base64.b64decode(msg["data"]))
            self.audio_mime = msg.get("mime", self.audio_mime)
        elif kind == "stop":
            if self.state == "recording":
                self.recording = False
                self.state = "building"
                # Its own task, not this socket's: writing the procedure takes
                # 30 s or more and must survive the phone losing signal, and
                # the socket keeps answering pings meanwhile.
                self.builder = asyncio.create_task(self.build(), name="capture-build")
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

    async def _finish_with(self, msg: dict[str, Any]) -> None:
        """Deliver the outcome, and keep it for a phone that rejoins later
        (the socket may have dropped while the procedure was being written)."""
        self.result = msg
        self.state = "done"
        await self.out.send(msg)

    async def _transcribe_gaps(self) -> None:
        """Narration recorded on the phone while it was offline becomes
        timestamped turns, in place, as if it had streamed live."""
        for at, pcm in self.gaps:
            if len(pcm) < 32000:                 # under a second: nothing said
                continue
            try:
                text = await asyncio.to_thread(meta_client.transcribe, pcm_to_wav(pcm), filename="gap.wav")
            except Exception as exc:  # noqa: BLE001
                log.warning("offline narration at %s could not be transcribed: %s", _mmss(at), exc)
                continue
            if text:
                frame = max(0, sum(1 for t in self.frame_times if t <= at) - 1)
                self.turns.append({"n": 0, "text": text, "at": at, "frame": frame, "offline": True})
        self.turns.sort(key=lambda t: t["at"])

    async def build(self) -> None:
        log.info("capture stop: %d keyframes, %d live utterances, %d bytes pcm, %d offline gaps, %d bytes blob",
                 len(self.keyframes), len(self.turns), len(self.pcm), len(self.gaps),
                 sum(map(len, self.audio_blobs)))
        if not self.keyframes:
            self.state = "idle"
            await self.out.send({"type": "error", "message": "No camera frames reached the server. "
                                 "Keep recording for a few seconds with the camera on the work, then stop."})
            return
        self.state = "building"
        # Let the last utterance finish arriving from the voice stream.
        if self.voice:
            await asyncio.sleep(1.2)
        if self.gaps:
            await self.status("Adding the narration recorded while offline…")
            await self._transcribe_gaps()

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
            await self._finish_with({"type": "error", "message": str(exc)})
            return
        save_reference_photos(proc, self.capture_id)
        pid = db.save_procedure(proc["title"], proc["steps"], machine=proc.get("machine"),
                                transcript=transcript)
        await self._finish_with({"type": "procedure", "procedure_id": pid, "procedure": proc})

    async def close(self) -> None:
        if self.voice:
            await self.voice.close()


def save_reference_photos(proc: dict[str, Any], tag: str) -> None:
    """Keep each step's keyframe from the expert's take as its reference
    photo: what "show me" displays and what the judge compares against.
    Removes the raw frames from `proc` so it can be sent as JSON."""
    frames = proc.pop("_frames", None) or []
    for step in proc.get("steps", []):
        ki = step.get("keyframe_index")
        if not frames or not isinstance(ki, int) or not (0 <= ki < len(frames)):
            continue
        name = f"ref_{tag}_s{step.get('index', 0):02d}.jpg"
        (config.MEDIA_DIR / name).write_bytes(frames[ki])
        step["ref_image"] = f"media/{name}"
