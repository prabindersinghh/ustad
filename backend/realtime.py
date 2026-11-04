"""Realtime speech: relay the phone's microphone to Muse Voice Transcribe.

The phone streams 16 kHz mono PCM to our own WebSocket; this relay forwards it
to wss://api.meta.ai/v1/asr/realtime so the API key never leaves the server.
Protocol per dev.meta.ai/docs/api-reference/voice/realtime and voice/schemas:

  * first frame: JSON handshake with authorization.accessToken, audioEncoding,
    model, mode; the reply is the only frame without a `type` (has sessionId)
  * audio: raw binary frames, signed 16-bit LE mono, paced at real time
  * events: transcript (partials), speechStart/End, speechComplete (a finished
    utterance), audioProgress, error
  * end: {"type": "endStream"}, then read until the server closes with 1000

The relay keeps one session open for as long as the screen is live and
reconnects transparently if the upstream drops (e.g. the phone paused audio
long enough to trip the below-real-time policy, or max session duration).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from typing import Awaitable, Callable

from websockets.asyncio.client import connect

from . import config

log = logging.getLogger("ustad.voice")

REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
OnText = Callable[[str, bool], Awaitable[None]]   # (text, final)


class VoiceRelay:
    """One phone microphone -> one Muse Voice realtime session (with reconnects)."""

    def __init__(self, on_text: OnText, *, keywords: list[str] | None = None,
                 language_bias: list[str] | None = None) -> None:
        self.on_text = on_text
        self.keywords = keywords or []
        self.language_bias = language_bias or []
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=200)
        self._task: asyncio.Task | None = None
        self._closing = False
        self.last_audio = 0.0
        self.connected = False

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="voice-relay")

    async def close(self) -> None:
        self._closing = True
        if self._task:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
            try:
                await asyncio.wait_for(self._task, timeout=6)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                self._task.cancel()

    def feed(self, pcm: bytes) -> None:
        """Queue a chunk of 16 kHz s16le mono audio. Drops if badly backlogged."""
        self.last_audio = time.monotonic()
        try:
            self._queue.put_nowait(pcm)
        except asyncio.QueueFull:
            log.warning("voice backlog full; dropping audio chunk")

    # ---- upstream session --------------------------------------------------
    async def _run(self) -> None:
        backoff = 0.5
        while not self._closing:
            try:
                await self._session()
                backoff = 0.5
            except Exception as exc:  # noqa: BLE001 - reconnect on any upstream failure
                self.connected = False
                if self._closing:
                    break
                log.warning("voice upstream dropped (%s); reconnecting in %.1fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 8)

    async def _session(self) -> None:
        # Drain stale audio so a reconnect does not replay a backlog.
        while not self._queue.empty():
            self._queue.get_nowait()
        async with connect(REALTIME_URL, max_size=None, open_timeout=10) as ws:
            handshake = {
                "authorization": {"accessToken": f"Bearer {config.MODEL_API_KEY}"},
                "audioEncoding": "PCM_16KHZ",
                "model": config.ASR_MODEL,
                "mode": "ENDPOINTING",
                "emitAudioProgress": False,
            }
            if self.keywords:
                handshake["keywords"] = self.keywords
            if self.language_bias:                # language names, e.g. ["Hindi", "English"]
                handshake["languageBias"] = self.language_bias
            await ws.send(json.dumps(handshake))
            ack = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if "sessionId" not in ack:
                raise RuntimeError(f"voice handshake rejected: {ack}")
            self.connected = True
            log.info("voice session %s open", ack["sessionId"])

            reader = asyncio.create_task(self._read(ws))
            sent = 0
            try:
                while True:
                    # Wait for audio or for the upstream to fail, whichever
                    # comes first: an upstream error while no audio flows
                    # (stop, pause) must surface, not sit in an orphaned task.
                    getter = asyncio.ensure_future(self._queue.get())
                    done, _ = await asyncio.wait({getter, reader}, return_when=asyncio.FIRST_COMPLETED)
                    if getter not in done:
                        getter.cancel()
                        reader.result()   # upstream error -> reconnect
                        return            # upstream closed: start a new session
                    chunk = getter.result()
                    if chunk is None:
                        await ws.send(json.dumps({"type": "endStream"}))
                        await asyncio.wait_for(reader, timeout=5)
                        return
                    await ws.send(chunk)
                    sent += len(chunk)
            finally:
                if not reader.done():
                    reader.cancel()
                secs = sent // 32000      # 16 kHz s16 mono; billed in whole seconds
                if secs:
                    from .meta_client import _meter
                    _meter(config.ASR_MODEL, "audio_s", secs, secs * config.PRICE_ASR_SECOND, "realtime")

    async def _read(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, bytes):
                continue
            msg = json.loads(raw)
            kind = msg.get("type")
            if kind == "transcript" and msg.get("transcript"):
                await self.on_text(msg["transcript"], False)
            elif kind == "speechComplete":
                text = (msg.get("transcript") or "").strip()
                if text:
                    await self.on_text(text, True)
            elif kind == "error":
                raise RuntimeError(msg.get("message", "voice error"))


# --------------------------------------------------------------------------
# Spoken commands for guide mode
# --------------------------------------------------------------------------

def _nf(text: str) -> str:
    """One spelling for the same Hindi letters: speech models and keyboards
    write some of them (like ड़) in two different ways."""
    return unicodedata.normalize("NFC", text).lower().replace("।", " ").replace("॥", " ")


_INTENTS: list[tuple[str, tuple[str, ...]]] = [
    # order matters: more specific phrases first
    ("why", ("what's wrong", "what is wrong", "why", "what did i do wrong", "explain",
             "क्यों", "क्यूँ", "क्या गलत", "क्या ग़लत", "kyun", "kyon", "kya galat")),
    ("where", ("where is", "where's", "show me", "which one", "what should i touch",
               "कहाँ", "कहां", "दिखाओ", "दिखाइए", "दिखा दो", "कौन सा", "kahan", "kahaan", "dikhao", "kaun sa")),
    ("repeat", ("repeat", "say again", "say that again", "come again", "pardon", "what was that",
                "फिर से", "दोबारा", "दुबारा", "फिर बोलो", "एक बार और", "phir se", "dobara", "dubara")),
    ("skip", ("skip", "skip step", "skip this", "छोड़ो", "छोड़ दो", "स्किप", "chhodo", "chhod do")),
    ("done", ("done", "finished", "i'm done", "im done", "complete", "check it", "check this",
              "all done", "ok done", "okay done", "next", "ready",
              "हो गया", "हो गई", "हो गयी", "हो चुका", "कर दिया", "कर लिया", "खत्म", "ख़त्म", "अगला", "डन",
              "चेक करो", "जाँच करो", "जांच करो", "ho gaya", "ho gayi", "kar diya", "kar liya", "agla", "check karo")),
]
_INTENTS = [(name, tuple(_nf(p) for p in phrases)) for name, phrases in _INTENTS]

_NEGATIONS = tuple(f" {_nf(w)} " for w in ("not", "नहीं", "नही", "मत", "nahi", "nahin", "mat"))
_QUESTION_HI = tuple(_nf(w) for w in ("क्या", "कैसे", "कहाँ", "कहां", "क्यों", "क्यूँ", "कौन", "कौनसा", "कब",
                                      "kya", "kaise", "kahan", "kahaan", "kyun", "kyon", "kaun"))
_HELP_HI = tuple(_nf(w) for w in ("मदद", "madad", "समझ नहीं", "samajh nahi", "नहीं हो रहा", "nahi ho raha",
                                  "फंस गया", "फँस गया", "अटक गया", "उस्ताद", "बताओ", "batao"))


_QUESTION_START = ("how", "what", "which", "where", "why", "when", "can", "could", "should", "do", "does",
                   "did", "is", "are", "will", "would", "has", "have", "who")
_HELP = ("help", "stuck", "won't", "wont", "can't", "cant", "doesn't", "doesnt", "not working",
         "not moving", "don't know", "dont know", "don't understand", "confused", "ustad", "show me how",
         "tell me", "explain")


def is_question(text: str) -> bool:
    """Is this utterance something to answer (a question or a call for help),
    rather than the worker talking to themselves or a colleague?

    Kept narrow on purpose: every answer is a model call and it talks over the
    worker. The Ask button bypasses this for anything said right after it.
    """
    t = " ".join(_nf(text).replace(",", " ").split())
    words = t.rstrip("?.!").split()
    if len(words) < 2 or len(words) > 40:
        return False
    if "?" in text or words[0].strip("'") in _QUESTION_START or any(w in _QUESTION_HI for w in words):
        return True
    bare = f" {t.rstrip('?.!')} "
    return any(f" {h} " in bare for h in _HELP) or any(f" {h} " in bare for h in _HELP_HI)


_HOWTO = re.compile(
    r"\b(what (do|should|shall) i do|what (now|next)|what'?s next|what to do|what am i (supposed to |meant to )?do"
    r"|how (do|should|can) i (do|start)( (it|this|that))?\b|how to do|how does this go|show me how"
    r"|help( me)?$|^help\b|i('?m| am) stuck|i don'?t (know|get it)|i'?m lost|tell me again)")


_HOWTO_HI = re.compile(_nf(
    r"(क्या|कैसे) (करूँ|करूं|करू|करना|करें)|अब क्या|आगे क्या|मदद|समझ नहीं|फ[ंँ]स गया|अटक गया"
    r"|(kya|kaise) kar|ab kya|aage kya|madad|samajh nahi"))


def is_howto(text: str) -> bool:
    """A generic "how do I do this?" (not a specific question): answered at
    once from the step itself (the instruction, the expert's own words,
    their photo, the live highlight), with no model call."""
    t = " ".join(_nf(text).replace(",", " ").split()).rstrip("?.!")
    return 0 < len(t.split()) <= 7 and bool(_HOWTO.search(t) or _HOWTO_HI.search(t))


def intent_of(text: str) -> str | None:
    """Map a finished utterance to a guide command, or None if it isn't one.

    Only short utterances count, so narration-like chatter ("I'm not done
    with the valve yet") does not trigger a check.
    """
    t = " " + " ".join(_nf(text).replace(",", " ").replace(".", " ").replace("!", " ")
                       .replace("?", " ").split()) + " "
    if len(t.split()) > 6:
        return None
    if any(n in t for n in _NEGATIONS) or "n't done" in t:
        return None
    for name, phrases in _INTENTS:
        for p in phrases:
            if f" {p} " in t:
                # "क्या हो गया?" ("what happened?") holds "हो गया" but isn't a "done".
                if name == "done" and any(f" {q} " in t for q in _QUESTION_HI):
                    return None
                return name
    return None
