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
| 00:30 | `d52721e` | **Project docs** (`AGENTS.md`, `docs/`). Security review fix: untrusted telemetry is flattened to escaped single lines, size-capped and rate-limited (log forging and flooding). README decisions table corrected (realtime ASR; no Spark audio fallback). |
| 00:45 | (research) | Market and X-factor research written to ROADMAP. User approved building all four X-factors, and added: "if the network cuts off, recording and guiding must continue where they stopped". |
| 13:40 | `12d9a9f` | **X-factors and offline resilience.** (1) **Expert reference:** capture keeps each step's keyframe, SAM box and the expert's quoted words; the judge sees the expert's photo next to the live view. (2) **Ask Ustad:** voice or typed questions answered by a Spark tool call (`answer` with `point_at` / `show_reference`, `check_now`, `escalate`); "Show me" button; coaching after 2 fails; questions logged. (3) **Offline:** capture takes outlive the socket (`CaptureSocket`, `rejoin`), the phone buffers frames, marks and narration offline and syncs them; the procedure build is its own task and is re-delivered on rejoin; guide reconnects forever, queues a "done", resumes past skips with the attempt count; ping and pong liveness. (4) **Evidence pack** page `/evidence?s=`. (5) **Self-improving procedures** (`backend/improve.py`): Spark proposes edits from run evidence, SAM checks phrases on the expert photo, supervisor approves on the dashboard, versions and history. Measured: Ask `minimal` 3.3 s vs `low` 5.8 s (both 3/3), so `minimal` is the default; reference adds no latency. Bugs found in browser tests and fixed: a reconnect sent "still" with no frame on the server; two sockets raced on `online` and lost synced frames. 109 tests. |
| 13:55 | `e80bd7d` | **Security review fixes.** Improve and apply need a supervisor token (header; `.env` or generated `data/supervisor_token.txt`); Improve rate-limited. Worker questions reach the improve prompt quoted, and edits that drop safety wording are refused in code. Capture memory bounded (12 takes, 400 keyframes, 10 min offline audio per take, checked before decoding). 112 tests. |
| 14:05 | `b2dc61b` | **Security review follow-up.** Waiting capture takes are never evicted for another client: per-client cap of 4 (by `CF-Connecting-IP`), only a client's own detached take can give way, and a full server refuses new takes. |
| 14:15 | `f47b890` | **Security review follow-up 2.** `CF-Connecting-IP` trusted only from loopback (the tunnel); new capture takes rate-limited per client; near-empty abandoned takes expire in 2 min. 115 tests. |
| 13:49 | (field run) | iPhone capture: **mic fix confirmed** (`path: worklet`, 113 chunks). But the build took 87 s for an 11 s take (writer 22 s at default effort, full-regeneration repair 60 s), the socket dropped 24 s in, and the phone never reconnected: the screen froze on "Building" although the procedure was saved. |
| 22:40 | `3d36ecd` | **Fast, unfreezable capture build, and long recordings.** Freeze fixed (close handler gave up after Stop; now rejoins, also on screen wake). Build progress every 5 s. Writer at `low`, targeted phrase repair at `minimal` (alternatives for just the missed phrases, SAM-tested in parallel). Measured on the real take: **17.2 s instead of 87 s**; browser drop mid-build: procedure delivered 15 s after the drop. Long recordings: written in parts of about 4 min while recording, merged by one text-only call (3 parts: 9.2 s left at stop); frames on disk; change-gated keyframes; recording limit 2 h; fallback audio capped. Voice relay errors now surface. 119 tests. |
| 22:50 | `cba14cd` | Security review: the generated supervisor token file is created owner-only (0600) atomically with `O_EXCL`, and an existing one is tightened on POSIX. |
| 23:00 | `f44bb08` | Security review: frames on disk are bounded. A frame is size-checked before decoding (700 KB encoded) and must be a JPEG; 300 MB per take, 1.5 GB across takes; written off the event loop. 120 tests. |

## 2026-11-04

| Time | Commit | What |
|---|---|---|
| 00:30 | (field run) | iPhone: capture and guide work. A 4-step guided job (laptop open and close) took 11 s, 49 s, then an 11 s "Done" check, per step. User: "Ustad is not quick in telling how to do it, and scanning to check the task is done should be fast." Also: the watcher crashed after the last step (index past the end), and a mic start raced (`this.ctx` null). |
| 01:40 | (this commit) | **Fast guiding.** Pipelined background judges (3 in flight, every 1 s, only while SAM sees the part, from 2 s into a step); one look at ≥ 0.9 advances an ordinary visual step (safety steps still two); "Done" tap at `minimal`; a pass is spoken as "Done." then the next instruction; "what do I do / help / what now" answered instantly from the step (no model call). Measured on the user's real job, same photos: **about 7 s per step (5.8 to 12.3) vs 11.5 to 30 s**; about $0.10 per 4-step job. Dashboard: "Run this procedure" (link, QR code, copy link) and "Run this procedure again" on the report. Fixes: watcher crash after the last step; mic start race (and the unlocked AudioContext is no longer closed on failure; the mic retries on the next tap). 135 tests. |
