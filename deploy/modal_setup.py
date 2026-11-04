"""Prepare the Modal side of the demo link. Prints no secret values.

    python deploy/modal_setup.py            # store .env settings as a Modal secret
    python deploy/modal_setup.py --seed     # also copy a clean set of demo jobs

Seeding works on a copy: the laptop's database is never changed. Only the
procedures listed in KEEP go up, with their runs and photos. Spend history stays,
so the budget guard still counts what has been spent.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import modal

ROOT = Path(__file__).resolve().parent.parent
KEEP = ["Open and close laptop", "Balance Bottle Upside Down", "Air compressor daily check"]


def env_values() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
        if m and m.group(2).strip():
            out[m.group(1)] = m.group(2).strip().strip("\"'")
    return out


def store_secret() -> None:
    values = env_values()
    # Local paths mean nothing in the container. The open backend stays off.
    # Keys for other routes aren't needed up there: only what the app uses goes.
    for local_only in ("FFMPEG", "DB_PATH", "REASONING_BACKEND", "AI_GATEWAY_API_KEY", "MODAL_PROXY_TOKEN"):
        values.pop(local_only, None)
    # MODAL_X in .env becomes X on the public link only: the laptop stays open for work.
    for name in [k for k in values if k.startswith("MODAL_") and k != "MODAL_PROXY_TOKEN"]:
        values[name[len("MODAL_"):]] = values.pop(name)
    if "SUPERVISOR_TOKEN" not in values:
        token = (ROOT / "data" / "supervisor_token.txt")
        if token.exists():
            values["SUPERVISOR_TOKEN"] = token.read_text(encoding="utf-8").strip()
    # Creating over an existing secret keeps the old values, so replace it.
    # A running app keeps its copy until the next `modal deploy`.
    modal.Secret.objects.delete("ustad-env", allow_missing=True)
    modal.Secret.objects.create("ustad-env", values)
    print("secret ustad-env stored with", sorted(values))


def db_session_tables(conn: sqlite3.Connection) -> list[str]:
    """Every table with a session_id column, including ones added later."""
    names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    return [n for n in names if any(c[1] == "session_id" for c in conn.execute(f"PRAGMA table_info({n})"))]


def seed() -> None:
    src = sqlite3.connect(ROOT / "data" / "ustad.db")
    tmp = Path(tempfile.mkdtemp()) / "ustad.db"
    dst = sqlite3.connect(tmp)
    src.backup(dst)
    src.close()
    marks = ",".join("?" * len(KEEP))
    keep_ids = [r[0] for r in dst.execute(f"SELECT id FROM procedures WHERE title IN ({marks})", KEEP)]
    id_marks = ",".join("?" * len(keep_ids))
    dst.execute(f"DELETE FROM procedures WHERE id NOT IN ({id_marks})", keep_ids)
    for table in ("procedure_history", "proposals", "translations"):
        dst.execute(f"DELETE FROM {table} WHERE procedure_id NOT IN ({id_marks})", keep_ids)
    # Only real runs of jobs that have the expert's photos: nothing synthetic.
    photo_ids = [r[0] for r in dst.execute("SELECT id, steps_json FROM procedures") if "ref_image" in r[1]]
    ph = ",".join("?" * len(photo_ids))
    dst.execute(f"DELETE FROM sessions WHERE procedure_id NOT IN ({ph})", photo_ids)
    dst.execute("DELETE FROM step_events WHERE session_id NOT IN (SELECT id FROM sessions)")
    # Everything else keyed by session goes with it, so no removed job travels up.
    for table in db_session_tables(dst):
        dst.execute(f"DELETE FROM {table} WHERE session_id NOT IN (SELECT id FROM sessions)")
    dst.commit()
    text = " ".join(r[0] for r in dst.execute("SELECT steps_json FROM procedures"))
    text += " " + " ".join(str(r[0]) for r in dst.execute("SELECT evidence_path FROM step_events WHERE evidence_path IS NOT NULL"))
    counts = {t: dst.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("procedures", "sessions", "step_events")}
    dst.execute("VACUUM")
    dst.close()

    media = sorted(f for f in (ROOT / "data" / "media").iterdir() if f.is_file() and f.name in text)
    volume = modal.Volume.from_name("ustad-data", create_if_missing=True)
    with volume.batch_upload(force=True) as batch:
        batch.put_file(str(tmp), "/ustad.db")
        for f in media:
            batch.put_file(str(f), f"/media/{f.name}")
    shutil.rmtree(tmp.parent, ignore_errors=True)
    print("seeded", counts, "| media files", len(media))


if __name__ == "__main__":
    store_secret()
    if "--seed" in sys.argv:
        seed()
