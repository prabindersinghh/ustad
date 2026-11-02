# Architecture

How Ustad works, from the phone to the models and back. Keep this file in
step with the code: a new file, endpoint, message type, setting or model call
lands here in the same commit.

## System

```
 iPhone (Safari PWA, web/)                       FastAPI (backend/)                      Meta Model API
 ─────────────────────────                       ──────────────────                      ──────────────
 camera ──1 fps JPEG (change-gated)──┐
 mic ─────16 kHz PCM, 100 ms chunks──┤  /ws/guide    GuideSession (live.py)
 taps / voice intents ───────────────┘──────────────▶  watcher: ground ~1 fps, 2 in flight ──▶ SAM 3.1 (grounding)
                                                       background judge, two passes        ──▶ Muse Spark 1.3 (tool call)
 speaker (speechSynthesis) ◀── speak / verdict ───────  voice relay                         ──▶ Muse Voice realtime ASR
 AR overlay (LiveBox) ◀─────── track / proximity ──────
                                                     /ws/capture   CaptureSession (live.py)
 camera ──JPEG every 2 s──────────────────────────────▶  live captions + draft step cards
 mic ─────PCM live + backup recording ─────────────────▶  build(): procedures.py           ──▶ Spark (write procedure)
                                                          self-check every phrase           ──▶ SAM (on expert footage)
                                                     SQLite (data/ustad.db) + evidence JPEGs (data/media/)
 /dashboard (web/dashboard.html) ◀── REST /api/* ─────
```

## Files

| File | Role |
|---|---|
| `backend/config.py` | Every setting, read from env with defaults (table below). |
| `backend/meta_client.py` | Meta Model API wrapper (OpenAI SDK compatible): `reason`, `reason_json`, `segment` (SAM), `transcribe` (batch ASR with ffmpeg to 16 kHz WAV). Meters every call into `usage`. Switches the reasoning backend between hosted and open weights. |
| `backend/sam_parse.py` | Parses SAM 3.1's token grammar into typed boxes with confidence. |
| `backend/realtime.py` | `VoiceRelay`: realtime ASR over WebSocket with reconnect and metering. `intent_of(text)` maps an utterance to done, repeat, skip, why or where. It ignores anything longer than 6 words or containing "not". |
| `backend/procedures.py` | Capture: narration plus keyframes become procedure JSON. `select_keyframes`, `_normalise` (step kinds, readings, timers, `watch_for`), `_validate_and_repair` (the self-check), `_check_grounding`, `_check_watch`. |
| `backend/guide.py` | Perception and decision. `ground`, `ground_scene` (target, hazards, tools and hands in parallel), `hand_near`, `left_behind`, `judge_step` (Spark tool call), `audit_skipped`, `parse_reading`, the TOOLS and SYSTEM prompt. |
| `backend/live.py` | Session logic. `GuideSession` (watcher, background judge, checks, jump-ahead with the safety gate, skip audit, end-of-job checks, dark notice, telemetry) and `CaptureSession` (pause, marks, live drafts, build). Also `inferable`, `log_diag`, `clean_for_log`, `_RateGate`. |
| `backend/db.py` | SQLite: procedures, sessions, step_events, usage. `step_insights`, `usage_summary`. |
| `backend/main.py` | FastAPI app: REST endpoints, the two WebSockets (thin `_pump` dispatch), static files, dashboard. |
| `web/index.html` | Landing page plus the app screens (home, capture, library, guide, report). |
| `web/app.js` | The PWA. Modules: `Voice` (speech queue), `MediaSource_` (camera, dark detection, track-loss events), `WakeLock`, `Mic` (16 kHz PCM, self-healing), `Diag` (telemetry), `Capture`, `Library`, `Guide`, `LiveBox` (AR overlay), `Report`. |
| `web/app.css` | All styles. The cockpit (camera screens) is dark; the rest is blue and white. |
| `web/dashboard.html` | Spend meter, procedures with per-step insights, sessions with evidence. |
| `web/figures/` | The verification-loop figure (anatomy style), built by `build.mjs`. |
| `scripts/` | `smoke_test.py` (live API, 5 checks), `evaluate.py` (accuracy on labelled runs), `seed_demo.py`, `deploy_open_model.sh` (Muse Glimmer on Modal). |
| `tests/` | 75 tests, no network: models are monkeypatched. |

## Capture flow

1. Tap **Start recording**. `Mic.prime()` runs inside the tap (iOS rule),
   then a 3-2-1 countdown, then the socket opens and sends `start`.
2. A frame is sent every 2 s (never a black one). PCM streams live to the
   realtime ASR, giving live captions, and `draft` step cards appear as the
   expert talks. A backup audio recording runs alongside.
3. **Pause** stops frames, audio and the clock. **Mark step**, or saying
   "next step", inserts a `[step marker]`.
4. **Stop**: the backup recording is uploaded only if the live stream carried
   under 3 s of audio, then `stop` is sent.
5. `build()`: the narration comes from live utterances, else batch ASR of the
   PCM or the backup blob. Up to 12 keyframes are selected. Spark writes the
   procedure. `_normalise` validates it. The self-check runs: every
   `target_phrase` and `watch_for` phrase is grounded by SAM on the step's own
   keyframe. Misses go back to Spark once for a rewrite. Anything still
   missing is marked `trackable: false`.

### Procedure step schema

```json
{ "index": 0, "title": "...", "instruction": "...", "target_phrase": "red emergency stop button",
  "success_criteria": "...", "common_mistake": "...", "hazard": "...", "keyframe_index": 3,
  "check": "visual | reading | timed | confirm",
  "reading": { "what": "pressure", "unit": "bar", "min": 6, "max": 8 },
  "duration_s": 30,
  "watch_for": [ { "phrase": "saw blade", "role": "hazard | tool", "trackable": true } ] }
```

## Guide flow (the live loop)

- **Watcher** (`GuideSession._watch`, every 100 ms):
  - Grounds the newest frame about every 1 s, with up to 2 SAM calls in
    flight.
  - A generation counter drops stale results after a step change.
  - Confirm steps, and steps without a target, cost nothing.
  - The budget guard turns watching off at 95% of `BUDGET_USD`.
- **Scene grounding** (`ground_scene`): the target, each trackable `watch_for`
  item, and "human hand" when a hazard is watched, all in parallel. Sends
  `track` with the objects. `hand_near` triggers `proximity` and a spoken
  warning (throttled to 6 s).
- **Background judge:** at `WATCH_EFFORT` (minimal) on the grounded frame. It
  auto-advances after **two** confident passes on a newer view, or on the same
  view held still. Timed steps can't finish early.
- **Explicit check:** triggered by the "done" voice intent or a tap. Runs
  `judge_step` at `JUDGE_EFFORT` (low), reusing a fresh grounding (under
  2.5 s old) when there is one. Spark must call exactly one tool:
  `verify_step`, `flag_error` or `request_better_view`. A reading out of
  range is overridden to `flag_error` on the server.
- **Jump-ahead:** `completed_through` lets one check close several steps.
  This is the safety gate: only `inferable` steps (visual, not hazard) are
  credited, and the jump stops at the first one that isn't.
- **End of job:** skipped steps are audited against the final frame, and the
  bench is checked for left-behind tools.
- **Evidence:** every judged step stores a photo and its grounding in
  `step_events`.

## WebSocket protocol

Text frames are JSON. Binary frames are 16 kHz mono s16le PCM in 100 ms
chunks.

### `/ws/guide`

| Phone to server | Meaning |
|---|---|
| `start {procedure_id}` · `resume {session_id}` | Begin, or rejoin after a drop. The server decides the resume step. |
| `watch {data}` | Live view, JPEG data URL. Sent only when the view changed (or every 8 s). |
| `still` | The view didn't change: keep the current grounding fresh, no SAM call. |
| `dark` | The view is black; the server tells the worker. |
| `check {said, data?}` | The worker asked for a check (tap or voice). |
| `repeat` · `skip` · `abort` | Controls. |
| `diag {...}` | Telemetry: track states, audio path, counters, notes (every 15 s). |

| Server to phone | Meaning |
|---|---|
| `session` | Session id, title, step count, flags. |
| `speak {text, step, of, title, target, kind, reading, duration}` | Announce a step (also sets the on-screen instruction). |
| `track {detections, target, objects}` | Live AR boxes: target, hazards, tools. |
| `proximity {hazard}` · `hazard {message}` | Hand near a hazard; safety notes. |
| `checking` · `verdict {...}` · `auto {state}` | Check in progress, its result, background state. |
| `heard {text, final}` | Live caption of the worker's speech. |
| `notice {text}` | Spoken toast (e.g. dark view). |
| `budget` · `complete` · `error` | Watching off at budget; job done; error. |

### `/ws/capture`

| Phone to server | Server to phone |
|---|---|
| `start`, `frame {data}`, `pause`, `resume`, `mark`, `audio {data, mime}`, `stop`, `diag`, binary PCM | `status`, `frame_ack {count}`, `heard`, `draft`, `draft_title`, `marker`, `paused`, `resumed`, `procedure`, `error` |

## REST

| Endpoint | Returns |
|---|---|
| `GET /api/health` | Which models and backend are live. |
| `GET /api/procedures` · `/api/procedures/{id}` | The library, one procedure. |
| `GET /api/procedures/{id}/insights` | Per-step first-time-right rate, caught, inferred, skipped, median time. |
| `GET /api/sessions` · `/api/sessions/{id}` | Session list, one session with events. |
| `GET /api/metrics` | Totals, including checks by backend. |
| `GET /api/usage` | Metered spend by model against `BUDGET_USD`. |
| `POST /api/clientlog` | Phone script errors. Unauthenticated: escaped, capped, rate-limited. |
| `GET /dashboard` · `GET /media/{name}` | Dashboard page; evidence photos. |

## Data (SQLite, `data/ustad.db`)

- `procedures(id, title, machine, language, steps_json, source_transcript, created_at)`
- `sessions(id, procedure_id, worker_name, mode, started_at, finished_at, outcome)`
- `step_events(id, session_id, step_index, verdict, confidence, reason, grounding, evidence_path, attempt, latency_ms, backend, created_at)`
  - Verdicts: pass (`verify_step`), fail (`flag_error`), retry
    (`request_better_view`), inferred (credited by jump-ahead), confirmed
    (confirm step, by voice), skipped, notice (end-of-job finding). `wait`
    (timer still running) goes to the phone only. A step counts as done for
    `pass`, `inferred`, `confirmed` and `skipped` (`db.DONE_VERDICTS`).
- `usage(id, at, model, kind, units, cost, note)`: every billed call.

## Models and what each call costs

| Call | Model | Setting | List price |
|---|---|---|---|
| Ground a phrase | `sam-3.1` | `SAM_MODEL` | $2.50 per 1k images |
| Judge a step, write a procedure, audit | `muse-spark-1.3` (Responses API, tools) | `JUDGE_EFFORT=low`, `WATCH_EFFORT=minimal`, `JUDGE_MAX_OUTPUT_TOKENS=1600`, `prompt_cache_key` | $1.25 per M input, $0.15 cached, $4.25 per M output (reasoning counts as output) |
| Speech to text | `muse-voice-transcribe-1.0` (realtime WS, batch fallback) | `VOICE=1` | $0.18 per hour |
| Open weights option | `meta-models/Muse-Glimmer-30B` on Modal | `REASONING_BACKEND=open` | Our GPU time |

Measured latency (fixed frames): Spark judge minimal about 1.8 s, low about
2.4 s, default about 3.95 s. Four parallel SAM calls take about 2 to 3 s.

## Settings (`backend/config.py`)

| Setting | Default | Meaning |
|---|---|---|
| `MODEL_API_KEY` | (secret, `.env`) | Never print it. |
| `SPARK_MODEL` · `SAM_MODEL` · `ASR_MODEL` | muse-spark-1.3 · sam-3.1 · muse-voice-transcribe-1.0 | Models. |
| `REASONING_BACKEND` | hosted | `open` routes reasoning to Glimmer (`OPEN_BASE_URL`, `OPEN_MODEL`, `OPEN_FALLBACK`). |
| `WATCH_GROUND_INTERVAL_S` · `WATCH_GROUND_PARALLEL` | 1.0 · 2 | Live grounding rate. |
| `WATCH_JUDGE_INTERVAL_S` · `WATCH_JUDGE_AFTER_S` | 5.0 · 4.0 | Background judge rate, and delay after a step starts. |
| `AUTO_ADVANCE` · `AUTO_CONFIDENCE` | 1 · 0.8 | Auto-advance on two confident passes. |
| `GROUNDING_FRESH_S` | 2.5 | Reuse a grounding this recent for a check. |
| `JUDGE_EFFORT` · `WATCH_EFFORT` · `JUDGE_MAX_OUTPUT_TOKENS` | low · minimal · 1600 | Reasoning effort and output cap. |
| `BUDGET_USD` | 50 | Live watching switches off at 95%. |
| `VALIDATE_PROCEDURE` | 1 | Run the capture self-check. |
| `VOICE` | 1 | Realtime voice. |
| `FFMPEG` | auto | Path for transcoding audio. |

## Telemetry

The phone reports what the server can't see. `Diag.snapshot()` sends video and
audio track state (`live`, `muted`, `ended`), AudioContext state and sample
rate, the audio path (`worklet`, `script` or `stalled`), chunk and frame
counters, and recent notes. It goes out over the session socket every 15 s and
at the end of a capture. Script errors go to `/api/clientlog` by beacon. The
server writes one `diag ...` line per report and a `guide close:` summary per
session. See RUNBOOK.md, "Reading phone telemetry".
