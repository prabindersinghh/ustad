"""Thin wrapper around the Meta Model API.

The API is OpenAI-SDK compatible, so we use the OpenAI client pointed at
Meta's base URL for the Responses calls, and raw httpx for the ASR endpoint
(which is not part of the OpenAI surface).
"""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Iterable

import httpx
from openai import OpenAI

from . import config, sam_parse

log = logging.getLogger("ustad.meta")

_client: OpenAI | None = None
_open_client: OpenAI | None = None


def client() -> OpenAI:
    """Hosted Meta Model API. Always used for SAM 3.1 and ASR."""
    global _client
    if _client is None:
        if not config.MODEL_API_KEY:
            raise RuntimeError(
                "MODEL_API_KEY is not set. Copy .env.example to .env and add your key."
            )
        _client = OpenAI(
            base_url=config.META_BASE_URL,
            api_key=config.MODEL_API_KEY,
            timeout=120.0,
        )
    return _client


def open_client() -> OpenAI:
    """Self-deployed open-weights endpoint (Modal / Baseten / on-prem box).

    Modal proxy tokens are used as `wk-<id>.ws-<secret>`; Baseten uses its own
    key. Either way it is an OpenAI-compatible base URL, so nothing else in
    Ustad needs to know the difference.
    """
    global _open_client
    if _open_client is None:
        if not config.open_backend_ready():
            raise RuntimeError(
                "Open-weights backend not configured. Set OPEN_BASE_URL and OPEN_MODEL."
            )
        _open_client = OpenAI(
            base_url=config.OPEN_BASE_URL,
            api_key=config.OPEN_API_KEY or "not-required",
            timeout=180.0,
        )
    return _open_client


def active_backend() -> str:
    """Which engine will answer the next reasoning call."""
    if config.REASONING_BACKEND == "open" and config.open_backend_ready():
        return "open"
    return "hosted"


def _data_url(image_bytes: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(image_bytes).decode()}"


def as_image_part(image: str | bytes) -> dict[str, Any]:
    """Accept raw bytes, a data URL, or an http URL and return an input_image part."""
    if isinstance(image, bytes):
        url = _data_url(image)
    elif image.startswith("data:") or image.startswith("http"):
        url = image
    else:  # bare base64
        url = f"data:image/jpeg;base64,{image}"
    return {"type": "input_image", "image_url": url}


# --------------------------------------------------------------------------
# Segmentation / grounding
# --------------------------------------------------------------------------

def segment(phrase: str, image: str | bytes) -> list[sam_parse.Detection]:
    """Locate every instance of `phrase` in the image using SAM 3.1.

    `phrase` must be a short noun phrase ("red valve handle"), not a question.
    Always runs on the hosted API -- SAM 3.1 is not an open-weights release.
    """
    resp = client().responses.create(
        model=config.SAM_MODEL,
        input=[
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": phrase},
                    as_image_part(image),
                ],
            }
        ],
        metadata={"include_confidence": "true"},
    )
    raw = getattr(resp, "output_text", "") or ""
    detections = sam_parse.parse(raw)
    _meter(config.SAM_MODEL, "image", 1, config.PRICE_SAM_IMAGE)
    log.info("SAM %r -> %d detections", phrase, len(detections))
    return detections


def _meter(model: str, kind: str, units: float, cost: float, note: str | None = None) -> None:
    """Record a billed call. Metering must never break a request."""
    try:
        from . import db
        db.log_usage(model, kind, units, cost, note)
    except Exception as exc:  # noqa: BLE001
        log.debug("usage meter failed: %s", exc)


def _meter_tokens(model: str, usage: Any) -> None:
    """Exact Spark cost from the token counts the API returns, reasoning included."""
    if usage is None:
        return
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    inp = (getattr(usage, "input_tokens", 0) or 0) - cached
    out = getattr(usage, "output_tokens", 0) or 0
    cost = inp * config.PRICE_SPARK_IN + cached * config.PRICE_SPARK_CACHED + out * config.PRICE_SPARK_OUT
    _meter(model, "tokens", inp + cached + out, cost, f"in={inp} cached={cached} out={out}")


# --------------------------------------------------------------------------
# Reasoning
# --------------------------------------------------------------------------

class _FunctionCall:
    """Mimics a Responses API function_call item."""

    type = "function_call"

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _Result:
    """Uniform result shape across the hosted and open-weights backends."""

    def __init__(self, output_text: str, output: list[Any], backend: str) -> None:
        self.output_text = output_text
        self.output = output
        self.backend = backend


def reason(
    instructions: str,
    content: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    model: str | None = None,
    force_hosted: bool = False,
    effort: str | None = None,
    max_output_tokens: int | None = None,
    cache_key: str | None = None,
) -> Any:
    """One reasoning turn, on whichever backend is active.

    `force_hosted` pins a call to the Meta Model API regardless of config --
    used where a capability only the hosted models have is required.

    `effort` sets Muse Spark's hidden reasoning (minimal/low/medium/high). It
    is the main lever on latency: a step check at the default effort spent
    up to 4,300 reasoning tokens (23 s); at `low` it judged the same frames
    as accurately in about 2.4 s. `max_output_tokens` caps visible plus
    reasoning tokens so one check can never run away. `cache_key` groups calls
    that share a long fixed prefix (system prompt + tools) for prompt caching.
    """
    if not force_hosted and active_backend() == "open":
        try:
            return _reason_open(instructions, content, tools=tools)
        except Exception as exc:  # noqa: BLE001
            if not config.OPEN_FALLBACK:
                raise
            log.warning("Open-weights backend failed (%s); falling back to hosted", exc)

    kwargs: dict[str, Any] = {
        "model": model or config.SPARK_MODEL,
        "instructions": instructions,
        "input": [{"type": "message", "role": "user", "content": content}],
    }
    if tools:
        kwargs["tools"] = tools
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    if max_output_tokens:
        kwargs["max_output_tokens"] = max_output_tokens
    if cache_key:
        kwargs["prompt_cache_key"] = cache_key
    resp = client().responses.create(**kwargs)
    _meter_tokens(kwargs["model"], getattr(resp, "usage", None))
    try:
        resp.backend = "hosted"  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - pydantic models may be frozen
        pass
    return resp


# --- open-weights adapter -------------------------------------------------
# Self-hosted servers (vLLM behind Modal Endpoints, Baseten, an on-prem box)
# speak Chat Completions rather than the Responses API, so we translate in
# both directions and hand the rest of Ustad an identical object.

def _to_chat_content(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for part in content:
        kind = part.get("type")
        if kind == "input_text":
            out.append({"type": "text", "text": part.get("text", "")})
        elif kind == "input_image":
            out.append({"type": "image_url", "image_url": {"url": part["image_url"]}})
        elif kind == "input_audio":
            out.append({"type": "input_audio", "input_audio": part["input_audio"]})
    return out


def _to_chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    converted = []
    for t in tools:
        if t.get("type") == "function" and "function" not in t:
            converted.append(
                {
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {}),
                    },
                }
            )
        else:
            converted.append(t)
    return converted


def _reason_open(
    instructions: str,
    content: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
) -> _Result:
    kwargs: dict[str, Any] = {
        "model": config.OPEN_MODEL,
        "messages": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": _to_chat_content(content)},
        ],
    }
    if tools:
        kwargs["tools"] = _to_chat_tools(tools)
        kwargs["tool_choice"] = "required"

    completion = open_client().chat.completions.create(**kwargs)
    u = getattr(completion, "usage", None)
    if u is not None:   # self-hosted: tracked for volume, billed by the GPU host not per token
        _meter(config.OPEN_MODEL, "tokens", (u.prompt_tokens or 0) + (u.completion_tokens or 0), 0.0, "open weights")
    message = completion.choices[0].message

    calls: list[Any] = []
    for call in getattr(message, "tool_calls", None) or []:
        fn = getattr(call, "function", None)
        if fn is not None:
            calls.append(_FunctionCall(fn.name, fn.arguments or "{}"))

    return _Result(message.content or "", calls, backend="open")


def tool_calls(resp: Any) -> list[dict[str, Any]]:
    """Pull function calls out of a Responses API result, tolerant of shape."""
    calls: list[dict[str, Any]] = []
    for item in getattr(resp, "output", []) or []:
        if getattr(item, "type", None) == "function_call":
            raw_args = getattr(item, "arguments", "") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {"_unparsed": raw_args}
            calls.append({"name": getattr(item, "name", ""), "args": args})
    return calls


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def reason_json(
    instructions: str,
    content: list[dict[str, Any]],
    *,
    model: str | None = None,
    effort: str | None = None,
) -> Any:
    """Muse Spark turn that must return JSON. Tolerant of fenced output."""
    resp = reason(
        instructions + "\n\nRespond with JSON only. No prose, no code fences.",
        content,
        model=model,
        effort=effort,
    )
    text = (getattr(resp, "output_text", "") or "").strip()

    for candidate in _json_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise ValueError(f"Model did not return parseable JSON. Got: {text[:400]}")


def _json_candidates(text: str) -> Iterable[str]:
    yield text
    fenced = _JSON_BLOCK.search(text)
    if fenced:
        yield fenced.group(1).strip()
    # Fall back to the outermost braces/brackets.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            yield text[start : end + 1]


# --------------------------------------------------------------------------
# Speech to text
# --------------------------------------------------------------------------

def transcribe(
    audio_bytes: bytes,
    *,
    filename: str = "audio.m4a",
    keywords: list[str] | None = None,
    language: str | None = None,
) -> str:
    """Batch transcription via Muse Voice Transcribe.

    Per dev.meta.ai/docs/api-reference/voice/transcribe: multipart/form-data
    with a `request` JSON part and an `audio` part, and the audio must be
    mono 16-bit PCM WAV at 16 or 24 kHz. Phones record AAC in an mp4/webm
    container, so we transcode first. Raises on failure; the caller decides
    whether a capture can go on without narration.
    """
    wav = _to_wav16k(audio_bytes, filename)
    req: dict[str, Any] = {
        "model": config.ASR_MODEL,
        "audioEncoding": "WAV",
        "mode": "ENDPOINTING",
    }
    if keywords:
        req["keywords"] = keywords
    if language:
        req["languageBias"] = [language]

    r = httpx.post(
        f"{config.META_BASE_URL.rstrip('/')}/asr/transcribe",
        headers={"Authorization": f"Bearer {config.MODEL_API_KEY}", "Accept": "application/json"},
        files={
            "request": (None, json.dumps(req), "application/json"),
            "audio": ("recording.wav", wav, "audio/wav"),
        },
        timeout=180.0,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"ASR {r.status_code}: {r.text[:300]}")
    payload = r.json()
    secs = int((payload.get("audioDurationMs") or 0) / 1000)   # billed in whole seconds
    _meter(config.ASR_MODEL, "audio_s", secs, secs * config.PRICE_ASR_SECOND, "batch")
    log.info("ASR %d ms of audio -> %d turns", payload.get("audioDurationMs", 0), len(payload.get("turns") or []))
    return (payload.get("transcript") or "").strip()


def _to_wav16k(audio_bytes: bytes, filename: str) -> bytes:
    """Any phone recording -> mono 16-bit PCM WAV at 16 kHz, via ffmpeg.

    Goes through temp files rather than pipes: mp4 from iOS puts its index at
    the end, which ffmpeg cannot read from a non-seekable stdin.
    """
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    ffmpeg = config.FFMPEG or shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found: install it or set FFMPEG in .env")
    ext = (filename.rsplit(".", 1)[-1] or "m4a").lower()
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / f"in.{ext}", Path(tmp) / "out.wav"
        src.write_bytes(audio_bytes)
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
             "-vn", "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", "-f", "wav", str(dst)],
            capture_output=True, timeout=120,
        )
        if proc.returncode != 0 or not dst.exists():
            raise RuntimeError(f"ffmpeg could not decode the recording: {proc.stderr.decode(errors='ignore')[:300]}")
        return dst.read_bytes()
