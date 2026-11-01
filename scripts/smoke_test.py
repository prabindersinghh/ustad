#!/usr/bin/env python3
"""Verify the Meta Model API key and every model Ustad depends on.

Run this FIRST, before touching the app:

    python -m scripts.smoke_test

It makes one small call per capability and prints exactly which ones work,
so a wrong model id or a missing credit shows up in seconds instead of
halfway through a demo.
"""
from __future__ import annotations

import base64
import io
import struct
import sys
import zlib

sys.path.insert(0, ".")

from backend import config, meta_client, sam_parse  # noqa: E402

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"


def _png(width: int = 320, height: int = 240) -> bytes:
    """A tiny valid PNG: grey field with a solid red rectangle in the middle."""
    rows = []
    for y in range(height):
        row = bytearray([0])  # filter byte
        for x in range(width):
            inside = (width * 0.35 < x < width * 0.65) and (height * 0.3 < y < height * 0.7)
            row += bytes((200, 40, 40) if inside else (130, 130, 130))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", raw)
        + chunk(b"IEND", b"")
    )


def check(label: str, fn) -> bool:
    print(f"  {label:<34}", end="", flush=True)
    try:
        detail = fn()
        print(f"{GREEN}ok{RESET}  {detail}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"{RED}FAIL{RESET}  {type(exc).__name__}: {str(exc)[:160]}")
        return False


def main() -> int:
    print("\nUstad smoke test")
    print(f"  base url  {config.META_BASE_URL}")
    print(f"  key       {'set (' + config.MODEL_API_KEY[:6] + '…)' if config.MODEL_API_KEY else RED + 'MISSING' + RESET}\n")

    if not config.MODEL_API_KEY:
        print(f"{RED}No MODEL_API_KEY. Copy .env.example to .env and add your key.{RESET}\n")
        return 1

    img = _png()
    results = []

    results.append(check("text reasoning (Spark)", lambda: _text()))
    results.append(check("image reasoning (Spark vision)", lambda: _vision(img)))
    results.append(check("segmentation (SAM 3.1)", lambda: _sam(img)))
    results.append(check("json mode", lambda: _json(img)))
    results.append(check("tool calling", lambda: _tools(img)))

    ok = sum(results)
    print(f"\n  {ok}/{len(results)} capabilities working")
    if ok < len(results):
        print(f"  {YELLOW}Fix the failures above before building further.{RESET}\n")
        return 1
    print(f"  {GREEN}All green. Start the server: uvicorn backend.main:app --reload{RESET}\n")
    return 0


def _text() -> str:
    r = meta_client.reason("Answer in one word.", [{"type": "input_text", "text": "Capital of France?"}])
    return repr((r.output_text or "").strip()[:40])


def _vision(img: bytes) -> str:
    r = meta_client.reason(
        "Answer in under 8 words.",
        [
            {"type": "input_text", "text": "What colour is the rectangle in the middle?"},
            meta_client.as_image_part(img),
        ],
    )
    return repr((r.output_text or "").strip()[:60])


def _sam(img: bytes) -> str:
    dets = meta_client.segment("red rectangle", img)
    if not dets:
        return "0 detections (parser ok, concept not matched)"
    d = dets[0]
    return f"{len(dets)} detection(s); first at {d.describe()}"


def _json(img: bytes) -> str:
    data = meta_client.reason_json(
        'Return {"colour": "<colour of the central rectangle>"}',
        [meta_client.as_image_part(img)],
    )
    return repr(data)[:70]


def _tools(img: bytes) -> str:
    from backend.guide import TOOLS, SYSTEM

    r = meta_client.reason(
        SYSTEM,
        [
            {
                "type": "input_text",
                "text": (
                    "Step 1: Check indicator\n"
                    "Instruction given to the worker: Confirm the indicator panel is red.\n"
                    "Success criteria: a solid red rectangle is visible in the centre.\n"
                    "Attempt number: 1\n\n"
                    "Grounding: searched the frame for \"red rectangle\" and found 1 match.\n"
                    "  1. centre of frame, covering 13.6% of the frame, confidence 0.94\n\n"
                    "The image is the worker's current view."
                ),
            },
            meta_client.as_image_part(img),
        ],
        tools=TOOLS,
    )
    calls = meta_client.tool_calls(r)
    if not calls:
        raise RuntimeError("model returned prose instead of a tool call")
    return f"{calls[0]['name']}({', '.join(list(calls[0]['args'])[:3])})"


if __name__ == "__main__":
    raise SystemExit(main())
