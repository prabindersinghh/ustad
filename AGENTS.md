# AGENTS.md: start here

You are picking up **Ustad**, an AI copilot for factory workers built for the
Meta Global AI Developer Hackathon 2026. This file is the entry point for any
developer or coding agent. Read it fully before touching code.

## Read in this order

1. [docs/STATUS.md](docs/STATUS.md): where the project is right now. What works,
   what is verified, what is broken, what is next. **Always current.**
2. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how it works, file by file,
   including the WebSocket protocol and the model calls.
3. [docs/TIMELINE.md](docs/TIMELINE.md): everything that was done, in order,
   with timestamps and commits.
4. [docs/DECISIONS.md](docs/DECISIONS.md): why things are the way they are.
   Check here before "fixing" something that looks odd.
5. [docs/ROADMAP.md](docs/ROADMAP.md): planned work, research findings, and
   ideas ranked by value.
6. [docs/RUNBOOK.md](docs/RUNBOOK.md): set up, run, test, tunnel to a phone,
   read the phone telemetry, troubleshoot.

## Hard rules

- **Never print, cat, echo or log `.env` or `MODEL_API_KEY`.** The file is
  gitignored. Check it exists with `test -f .env`, nothing more.
- **Never state a number that was not measured.** Landing page, README and
  docs alike. Unmeasured results are written as "in progress".
- **No em dashes** in any user-facing page (`web/`). Use commas, colons or
  full stops.
- **No real client data.** The factory that inspired this (a paint plant in
  Punjab) is never named, and no footage from it is committed.
- **Safety steps are never skipped by inference.** Lockout, readings, timers
  and confirm steps must be done explicitly. See `live.inferable()` and
  DECISIONS.md D-014. Do not weaken this.
- **Budget.** Hackathon credit is $150; the working cap is `BUDGET_USD=50`.
  Every model call is metered (`/api/usage`). Probes that call real models
  cost money: say so and keep them small.
- **The repo must be made public before the deadline**
  (Nov 4 2026, 9 PM ET = Nov 5, 7:30 AM IST). It is private until then.

## Keeping the docs alive (required)

Every change that lands in a commit also updates the docs in the same commit:

| You did | Update |
|---|---|
| Anything | Add a dated entry to `docs/TIMELINE.md` (newest at the bottom of its day) |
| Changed what works or what is broken | `docs/STATUS.md` |
| Added a file, endpoint, message type, setting or model call | `docs/ARCHITECTURE.md` |
| Made a non-obvious choice | A new entry in `docs/DECISIONS.md` |
| Finished, added or dropped planned work | `docs/ROADMAP.md` |
| Changed how to run, test or deploy | `docs/RUNBOOK.md` |

Timestamps are IST (UTC+05:30), matching the git log.

## Quick commands

```bash
.venv/Scripts/python.exe -m pytest -q                                  # all tests, no network
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8800
curl -s localhost:8800/api/usage                                       # spend so far
```
