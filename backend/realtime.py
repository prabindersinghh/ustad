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
import time
from typing import Awaitable, Callable

from websockets.asyncio.client import connect

from . import config

log = logging.getLogger("ustad.voice")

REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
OnText = Callable[[str, bool], Awaitable[None]]   # (text, final)


class VoiceRelay:
    """One phone microphone -> one Muse Voice realtime session (with reconnects)."""

    def __init__(self, on_text: OnText, *, keywords: list[str] | None = None) -> None:
        self.on_text = on_text
        self.keywords = keywords or []
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
                    chunk = await self._queue.get()
                    if chunk is None:
                        await ws.send(json.dumps({"type": "endStream"}))
                        await asyncio.wait_for(reader, timeout=5)
                        return
                    await ws.send(chunk)
                    sent += len(chunk)
                    if reader.done():
                        reader.result()   # surface upstream errors -> reconnect
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

_INTENTS: list[tuple[str, tuple[str, ...]]] = [
    # order matters: more specific phrases first
    ("why", ("what's wrong", "what is wrong", "why", "what did i do wrong", "explain")),
    ("where", ("where is", "where's", "show me", "which one", "what should i touch")),
    ("repeat", ("repeat", "say again", "say that again", "come again", "pardon", "what was that")),
    ("skip", ("skip", "skip step", "skip this")),
    ("done", ("done", "finished", "i'm done", "im done", "complete", "check it", "check this",
              "all done", "ok done", "okay done", "next", "ready")),
]


def intent_of(text: str) -> str | None:
    """Map a finished utterance to a guide command, or None if it isn't one.

    Only short utterances count, so narration-like chatter ("I'm not done
    with the valve yet") does not trigger a check.
    """
    t = " " + " ".join(text.lower().replace(",", " ").replace(".", " ").replace("!", " ")
                       .replace("?", " ").split()) + " "
    if len(t.split()) > 6:
        return None
    if " not " in t or "n't done" in t:
        return None
    for name, phrases in _INTENTS:
        for p in phrases:
            if f" {p} " in t:
                return name
    return None
