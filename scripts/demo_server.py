#!/usr/bin/env python3
"""A second Ustad server for recording the demo, safe beside the running one.

It shares the database and media, so its sessions appear on the main dashboard.
It does not share the takes folder: every server start clears that folder, which
would delete an in-progress take on the other server.

    python -m scripts.demo_server --port 8810 --takes data/factory/replay/takes
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import uvicorn

from backend import config, db


def _tally_spend(path: Path) -> None:
    """The usage table is shared with anything else using this database, so keep
    a line per billed call from this server alone."""
    real = db.log_usage

    def counted(model: str, kind: str, units: float, cost: float, note: str | None = None) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{time.time():.1f} {model} {kind} {cost:.6f}\n")
        real(model, kind, units, cost, note)

    db.log_usage = counted  # type: ignore[assignment]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spend-log", type=Path)
    ap.add_argument("--port", type=int, default=8810)
    ap.add_argument("--takes", type=Path, required=True)
    args = ap.parse_args()
    if args.port == 8800:
        raise SystemExit("Port 8800 belongs to the running server.")
    config.TAKES_DIR = args.takes.resolve()
    if args.spend_log:
        _tally_spend(args.spend_log)
    from backend.main import app
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
