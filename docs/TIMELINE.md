# Timeline

Everything done on Ustad, in order. Times are IST (UTC+05:30), matching
`git log`. Each entry names its commit. Add to the bottom of the current day
with every change (see AGENTS.md).

---

## Before 2026-11-01: brief and first build

- Brief: an AI copilot for factory workers with two modes. **Capture**: an
  expert records a job once and it becomes a checkable procedure.
  **Guide**: a worker is guided hands-free and every step is verified with
  SAM 3.1 grounding plus a Muse Spark tool call. Built for the Meta Global AI
  Developer Hackathon. Judged on Impact, Use of Meta AI, Technical depth and
  Demo. Bonus targets: Best Agentic, Open Weights, Most Viral.
- Original task list: (1) smoke test 5/5, (2) capture quality plus SAM
  self-check, (3) `/dashboard`, (4) `scripts/evaluate.py`, (5) Muse Glimmer
  on Modal, (6) polish (reconnect, camera-denied state, seed data).
  Tasks 1 to 4 and 6 done. Task 5 waits on the user's Modal login.
- UI: premium blue and white landing page in the Meta style, 3D glasses
  (model-viewer) and an iPhone model. Rebuilt after user feedback, using the
  mindcloud, Voiceflow and wallet-site references. The user's logo goes on a
  white background with no box; the nav mark uses the blue of the logo.
- Landing copy from the user's `LANDING_COPY.md`. All em dashes removed.

## 2026-11-01

| Time | Commit | What |
|---|---|---|
| 19:51 | `8a21278` | **First commit.** Backend (FastAPI, Meta Model API client, SAM parser, capture, guide, SQLite), PWA, dashboard, evaluate and seed scripts, tests. Private GitHub repo created (user's choice; Vercel skipped). |

## 2026-11-02

| Time | Commit | What |
|---|---|---|
| 00:37 | `540316c` | **Capture fixed for the iPhone.** Real ASR contract (multipart `request` plus 16 kHz WAV via ffmpeg). Unsupported Spark audio fallback removed. Start-tap guard, immediate and final frames. Model calls moved off the event loop (`asyncio.to_thread`) so sockets stop dropping. Audio-only MediaRecorder. |
| 01:12 | `dc75f3e` | **Live mode.** Realtime voice relay (`backend/realtime.py`), live grounding about 1 fps with AR box, background verification with two-pass auto-advance, stable-partial "done" (0.45 s), phone-side frame-change gating with "still" heartbeats, spend meter and budget guard, `backend/live.py` session classes. Reasoning effort benchmarked: low ~2.4 s, minimal ~1.8 s, both 6/6 correct. |
| 01:19 | `66246b3` | Landing copy rewritten for factory workers, with sourced numbers only (1.9M jobs, Deloitte and MI 2024; 11% of revenue lost to downtime, Siemens 2024; 23% of service visits fail first time, Vanson Bourne and ServiceMax 2017). |
| 01:26 | `90ec837` | Hero: rotating glasses with labels pinned to real 3D points (camera, mic, speaker). Visibility computed in JS because model-viewer never set `data-visible`. |
| 01:49 | `5022300` | **Smarter guide for real machines.** Step kinds: visual, reading (range-checked on the server), timed, confirm. Jump-ahead: when the end state proves skipped steps, they are credited. Skipped-step audit at job end. Recording UX: countdown, clock, pause and resume, mark step, 10 minute limit. Per-step insights on the dashboard. |
| 01:52 | `d024299` | **Safety gate** (from a security review). The model's `completed_through` is advisory only. A jump stops at any lockout, reading, timed or confirm step, and those are never credited by inference. Worker speech is quoted, not trusted. |
| 02:05 | `c020f1f` | **Multi-object detection.** Capture records up to 2 hazards or tools per step (`watch_for`). Live: target, hazards, tools and hands grounded in parallel. Hand-near-hazard alert (spoken, banner, flash). Left-behind tool check at job end. The self-check grounds every hazard and tool phrase on the expert's footage, rewrites misses, and marks the rest "not trackable". Real SAM probe: drawn saw blade read as "gear", which motivated the check. |
| 21:49 | `39c7562` | **iPhone camera and mic.** The AudioContext is unlocked inside the tap. Black frames are never sent. A muted or ended camera track shows a clear overlay with a Retry that keeps the recording. In-app browsers are told to open Safari. Cause found in logs: 4 black keyframes and 0 bytes of audio produced a procedure about "a black screen". |
| 21:5x | (no commit) | Cloudflare quick tunnel expired ("Tunnel not found"). Restarted with a new URL. |
| 23:33 | (field run) | iPhone capture works (camera OK, 7 keyframes) but still **0 bytes of audio**. A guide session received **0 frames**. Both failed silently. |

## 2026-11-03

| Time | Commit | What |
|---|---|---|
| 00:13 | `b59d3d3` | **Field reliability.** The mic self-heals: AudioWorklet, then ScriptProcessor if no chunk arrives within 2.5 s, then error. Capture always keeps a backup narration recording and uploads it only if under 3 s streamed live. A dark view sends `dark` and the server says "I can't see anything" (at most every 12 s). Phone telemetry: `diag` messages, `/api/clientlog` beacons, a `guide close:` summary line. |
| 00:30 | (this commit) | **Project docs** (`AGENTS.md`, `docs/`). Security review fix: untrusted telemetry is flattened to escaped single lines, size-capped and rate-limited (log forging and flooding). README decisions table corrected (realtime ASR; no Spark audio fallback). |
