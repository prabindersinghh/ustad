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
# How many keyframes we hand to Spark when generating a procedure (or one
# part of a long recording).
MAX_KEYFRAMES_FOR_PROCEDURE = 12
# Writing a procedure: measured on a real take, default effort 19.9 s, low
# 10.2 s with the same steps, minimal 11.2 s (output-bound). Low it is.
WRITER_EFFORT = os.getenv("WRITER_EFFORT", "low")
# Renaming parts SAM couldn't find: minimal 2.5 s vs low 6 s, same top pick.
REPAIR_EFFORT = os.getenv("REPAIR_EFFORT", "minimal")
# Long recordings are written in parts while recording: a part is cut at an
# utterance boundary once it is this long (or twice this, with no speech).
SEGMENT_S = float(os.getenv("SEGMENT_S", "240"))
SEGMENT_KEYFRAMES = 8
# A take's frames live on disk, not in memory: one long job can be hours.
TAKES_DIR = ROOT / "data" / "takes"

# --------------------------------------------------------------------------
# Live guide mode. While a step is active the phone streams a low-res view and
# the server "watches": SAM grounds the target continuously (the live box on
# screen, and a ready-made grounding when the worker says done), and Spark
# judges in the background so a finished step is noticed without being asked.
# Every call costs credits, so each rate is capped here.
# --------------------------------------------------------------------------
WATCH_GROUND_INTERVAL_S = float(os.getenv("WATCH_GROUND_INTERVAL_S", "1.0"))
WATCH_GROUND_PARALLEL = int(os.getenv("WATCH_GROUND_PARALLEL", "2"))
# A judge call takes ~4.5 s (measured on a real job, any effort or image
# size), so judges are pipelined: up to WATCH_JUDGE_PARALLEL in flight,
# started this far apart, each on a view not yet judged. Done to advance:
# ~6 s instead of the ~15 to 20 s of one judge every 5 s.
WATCH_JUDGE_INTERVAL_S = float(os.getenv("WATCH_JUDGE_INTERVAL_S", "1.0"))
WATCH_JUDGE_PARALLEL = int(os.getenv("WATCH_JUDGE_PARALLEL", "3"))
# Don't judge in the first seconds of a step: the worker hasn't started yet.
WATCH_JUDGE_AFTER_S = float(os.getenv("WATCH_JUDGE_AFTER_S", "2.0"))
# Auto-advance needs two background passes in a row at this confidence.
AUTO_ADVANCE = os.getenv("AUTO_ADVANCE", "1") != "0"
AUTO_CONFIDENCE = float(os.getenv("AUTO_CONFIDENCE", "0.8"))
# ...or one pass at this confidence, with SAM seeing the target, for an
# ordinary visual step (never a hazard, reading, timed or confirm step).
AUTO_SINGLE_CONFIDENCE = float(os.getenv("AUTO_SINGLE_CONFIDENCE", "0.9"))
# A grounding is reused for "done" only if its frame is at most this old.
GROUNDING_FRESH_S = float(os.getenv("GROUNDING_FRESH_S", "2.5"))
VOICE = os.getenv("VOICE", "1") != "0"

# Muse Spark reasoning effort per call type (measured on fixed frames: default
# ~4 s and up to 23 s live, low ~2.4 s, minimal ~1.8 s, all 6/6 correct).
# Operator said "done". 2026-11-04, the user's real job: minimal 4.5 s median
# 6/6 right; low 5.5 s 6/6, with an 11 s worst case live. Minimal it is.
JUDGE_EFFORT = os.getenv("JUDGE_EFFORT", "minimal")
WATCH_EFFORT = os.getenv("WATCH_EFFORT", "minimal")      # background look; two passes must agree
JUDGE_MAX_OUTPUT_TOKENS = int(os.getenv("JUDGE_MAX_OUTPUT_TOKENS", "1600"))
# Show the judge the expert's own keyframe for the step next to the worker's
# view (one extra image per check; steps captured before this have none).
JUDGE_REFERENCE = os.getenv("JUDGE_REFERENCE", "1") != "0"
# Ask Ustad: questions answered by Spark from the procedure, the expert's
# words and the live view. Coaching starts after this many failed checks.
# Measured on real photos: minimal ~3.3 s, low ~5.8 s, both 3/3 right tool.
# An answer can never mark a step done (check_now runs the normal judge).
ASK_EFFORT = os.getenv("ASK_EFFORT", "minimal")
COACH_AFTER_FAILS = int(os.getenv("COACH_AFTER_FAILS", "2"))
# Self-improving procedures: one considered Spark call per request, offline.
IMPROVE_EFFORT = os.getenv("IMPROVE_EFFORT", "medium")


def supervisor_token() -> str:
    """The secret a supervisor enters to change procedures (Improve / apply).

    From SUPERVISOR_TOKEN in .env, else generated once into
    data/supervisor_token.txt (gitignored). The demo link is public, so
    changing a procedure must need more than the URL. Never logged.
    """
    tok = os.getenv("SUPERVISOR_TOKEN", "").strip()
    if tok:
        return tok
    path = ROOT / "data" / "supervisor_token.txt"
    try:
        tok = path.read_text(encoding="utf-8").strip()
        if tok and os.name == "posix" and path.stat().st_mode & 0o077:
            os.chmod(path, 0o600)                 # an older or copied file: owner-only
    except OSError:
        tok = ""
    if not tok:
        import secrets
        tok = secrets.token_urlsafe(18)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Created owner-only (0600) in one step: never readable by others,
        # not even for a moment. Two starts racing: the second reads the first.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return path.read_text(encoding="utf-8").strip() or tok
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok)
    return tok

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
