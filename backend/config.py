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

# After generating a procedure, ground every target_phrase with SAM and
# regenerate the ones it can't find. One-time cost at authoring, and it is what
# stops a vague target_phrase from making the guide loop useless later.
VALIDATE_PROCEDURE = os.getenv("VALIDATE_PROCEDURE", "1") != "0"
