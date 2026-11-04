"""Ustad on Modal: the public demo link, with no laptop involved.

    modal deploy deploy/modal_app.py

Exactly one small always-on container, because a capture take and a guide
session live in memory and need their socket to come back to the same process.
Modal's Volumes are not meant for a live SQLite file, so the database and media
stay on the container's own disk: restored from the Volume at start and backed
up every BACKUP_S. A restart loses at most that much, and the phone's guide
reconnect resumes from the steps that were saved.

Settings and keys come from the Modal secret `ustad-env` (deploy/modal_setup.py).
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent.parent
APP_ROOT = Path("/app")
DATA = APP_ROOT / "data"
VOL = Path("/vol")
BACKUP_S = 30

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg")
    .pip_install_from_requirements(str(HERE / "requirements.txt"))
    .add_local_dir(HERE / "backend", "/app/backend", ignore=["__pycache__", "*.pyc"])
    .add_local_dir(HERE / "web", "/app/web")
)

app = modal.App("ustad", image=image)
volume = modal.Volume.from_name("ustad-data", create_if_missing=True)


def restore() -> None:
    (DATA / "media").mkdir(parents=True, exist_ok=True)
    volume.reload()
    if (VOL / "ustad.db").exists():
        shutil.copy2(VOL / "ustad.db", DATA / "ustad.db")
    media = VOL / "media"
    if media.is_dir():
        for f in media.iterdir():
            if f.is_file():
                shutil.copy2(f, DATA / "media" / f.name)


def backup() -> None:
    """A consistent copy of the live database, plus any new media files."""
    db = DATA / "ustad.db"
    if db.exists():
        tmp = VOL / "ustad.db.tmp"
        src, dst = sqlite3.connect(db), sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        tmp.replace(VOL / "ustad.db")
    media = VOL / "media"
    media.mkdir(exist_ok=True)
    for f in (DATA / "media").iterdir():
        if f.is_file() and not (media / f.name).exists():
            shutil.copy2(f, media / f.name)
    volume.commit()


def backup_forever() -> None:
    while True:
        time.sleep(BACKUP_S)
        try:
            backup()
        except Exception as exc:  # noqa: BLE001 - keep serving; the next round retries
            print(f"backup failed: {exc}", flush=True)


@app.function(
    secrets=[modal.Secret.from_name("ustad-env")],
    volumes={str(VOL): volume},
    cpu=1.0,
    memory=2048,
    min_containers=1,
    max_containers=1,
    scaledown_window=1200,
    timeout=86400,
)
@modal.concurrent(max_inputs=200)
@modal.asgi_app()
def web():
    sys.path.insert(0, str(APP_ROOT))
    restore()
    threading.Thread(target=backup_forever, daemon=True, name="backup").start()
    from backend.main import app as fastapi_app
    return fastapi_app
