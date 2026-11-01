"""Configuration loaded from environment."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

MODEL_API_KEY = os.getenv("MODEL_API_KEY", "")
META_BASE_URL = os.getenv("META_BASE_URL", "https://api.meta.ai/v1")

SPARK_MODEL = os.getenv("SPARK_MODEL", "muse-spark-1.3")
SPARK_AUDIO_MODEL = os.getenv("SPARK_AUDIO_MODEL", "muse-spark-1.2")
SAM_MODEL = os.getenv("SAM_MODEL", "sam-3.1")
ASR_MODEL = os.getenv("ASR_MODEL", "muse-voice-transcribe-1.0")
# ASR only accepts 16 kHz mono WAV; phones record AAC, so we transcode with
# ffmpeg. Leave empty to find it on PATH.
FFMPEG = os.getenv("FFMPEG", "")

# --------------------------------------------------------------------------
# Open-weights reasoning backend.
#
# Ustad can run its verification reasoning either on the hosted Meta Model API
# or on an open-weights Meta model we deploy ourselves (Muse Glimmer on Modal,
# Baseten, or a box inside the plant). Both speak the OpenAI protocol, so the
# only difference is a base URL, a key and a model name.
#
# This is not a gimmick: it is the answer to the first question every factory
# asks, which is whether their line footage leaves the site.
# --------------------------------------------------------------------------
REASONING_BACKEND = os.getenv("REASONING_BACKEND", "hosted").strip().lower()

OPEN_BASE_URL = os.getenv("OPEN_BASE_URL", "")
OPEN_API_KEY = os.getenv("OPEN_API_KEY", "")
OPEN_MODEL = os.getenv("OPEN_MODEL", "meta-models/Muse-Glimmer-30B")

# If the open backend errors, fall back to hosted rather than stranding a
# worker mid-procedure. Set to "0" to make open-weights failures loud.
OPEN_FALLBACK = os.getenv("OPEN_FALLBACK", "1") != "0"


def open_backend_ready() -> bool:
    return bool(OPEN_BASE_URL and OPEN_MODEL)

DB_PATH = ROOT / os.getenv("DB_PATH", "data/ustad.db")
MEDIA_DIR = ROOT / "data" / "media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# Cost control: how often we sample frames from the live stream.
CAPTURE_FRAME_INTERVAL_S = 2.0
# How many keyframes we hand to Spark when generating a procedure.
MAX_KEYFRAMES_FOR_PROCEDURE = 12

# --------------------------------------------------------------------------
# Live guide mode. While a step is active the phone streams a low-res view and
# the server "watches": SAM grounds the target continuously (the live box on
# screen, and a ready-made grounding when the worker says done), and Spark
# judges in the background so a finished step is noticed without being asked.
# Every call costs credits, so each rate is capped here.
# --------------------------------------------------------------------------
WATCH_GROUND_INTERVAL_S = float(os.getenv("WATCH_GROUND_INTERVAL_S", "1.0"))
WATCH_GROUND_PARALLEL = int(os.getenv("WATCH_GROUND_PARALLEL", "2"))
WATCH_JUDGE_INTERVAL_S = float(os.getenv("WATCH_JUDGE_INTERVAL_S", "5.0"))
# Don't judge in the first seconds of a step: the worker hasn't started yet.
WATCH_JUDGE_AFTER_S = float(os.getenv("WATCH_JUDGE_AFTER_S", "4.0"))
# Auto-advance needs two background passes in a row at this confidence.
AUTO_ADVANCE = os.getenv("AUTO_ADVANCE", "1") != "0"
AUTO_CONFIDENCE = float(os.getenv("AUTO_CONFIDENCE", "0.8"))
# A grounding is reused for "done" only if its frame is at most this old.
GROUNDING_FRESH_S = float(os.getenv("GROUNDING_FRESH_S", "2.5"))
VOICE = os.getenv("VOICE", "1") != "0"

# --------------------------------------------------------------------------
# Spend. Every call is metered at list price (dev.meta.ai/docs/pricing-rate-
# limits) into the usage table. Near the budget, live watching switches off
# and guide mode falls back to tap/voice checks rather than overspending.
# --------------------------------------------------------------------------
BUDGET_USD = float(os.getenv("BUDGET_USD", "50"))
PRICE_SAM_IMAGE = 2.50 / 1000
PRICE_SPARK_IN = 1.25 / 1_000_000
PRICE_SPARK_CACHED = 0.15 / 1_000_000
PRICE_SPARK_OUT = 4.25 / 1_000_000          # includes hidden reasoning tokens
PRICE_ASR_SECOND = 0.18 / 3600

# After generating a procedure, ground every target_phrase with SAM and
# regenerate the ones it can't find. One-time cost at authoring, and it is what
# stops a vague target_phrase from making the guide loop useless later.
VALIDATE_PROCEDURE = os.getenv("VALIDATE_PROCEDURE", "1") != "0"
