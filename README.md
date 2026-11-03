# Ustad

**An AI copilot for industrial technicians that learns a procedure by watching an expert do it once, then guides and verifies every worker after that.**

> **Developers and coding agents:** start with [AGENTS.md](AGENTS.md). Current state, timeline, architecture and decisions are in [docs/](docs/).

Built for the Meta Global AI Developer Hackathon 2026 — Indie Developer track, *Push the Frontier*.

---

## The problem

In a factory, the procedures that matter most — machine changeovers, maintenance, quality checks — are rarely written down. They live in the heads of a few experienced people. When those people leave, the knowledge goes with them, and new workers learn by trial and error: mistakes, downtime, scrap.

Written SOPs don't fix this. They're slow to produce, go stale, and fundamentally cannot tell you whether a step was actually *done right*.

## What Ustad does

**Capture mode.** An expert does the job once, wearing the camera, narrating as they go. Ustad transcribes the narration, samples keyframes, and writes a step-by-step procedure where every step carries a *visual success criterion* and a *target phrase* — a short noun phrase naming the one object the worker must act on. Then it **checks its own work**: SAM 3.1 tries to ground every target phrase in the keyframe that step came from, and any phrase it cannot find is fed back to the model and regenerated. A procedure ships only with target phrases the perception model can actually locate — which is what makes the downstream verification trustworthy.

**Guide mode.** A new worker gets hands-free, spoken guidance, one step at a time. When they say they're done, Ustad doesn't take their word for it:

1. **Perception** — SAM 3.1 grounds the step's target phrase in the live frame and returns boxes with confidence.
2. **Decision** — Muse Spark sees the frame, the grounding report and the success criteria, and must call exactly one tool: `verify_step`, `flag_error`, or `request_better_view`.
3. **Action** — the server executes it: advance and bank a photo, or speak a correction and hold the worker on the step.

The visual evidence changes what the system does next. That is the difference between this and a chatbot describing a photo.

Every judged step leaves a timestamped photo behind, so a completed job produces an audit trail nobody had to write.

**Ask Ustad.** Stuck? The worker just asks: "which one?", "it won't turn", "can I skip this?". Muse Spark answers from the expert's own recorded words and the live view, and answers with a tool call:
- `answer` can **point**: SAM 3.1 lights up the part in the worker's view, and "Show me" puts the expert's own photo of the step beside it, with the part boxed.
- `check_now` runs the step check.
- `escalate` stops the worker and logs it for a supervisor. That is what happens to "can I skip the lockout?".

After two failed checks, Ustad coaches without being asked. The judge itself also sees the expert's photo, so it knows what "right" looks like on this exact machine.

**Works through dropouts.** If factory Wi-Fi drops mid-recording, the phone keeps recording and syncs the frames, step marks and narration when it reconnects. Mid-job, the worker carries on and the job resumes at the exact step.

**Evidence pack.** Every job has a printable page with each step's photo, the SAM box, the verdict, the trail of errors caught, and the questions asked.

**Procedures that improve themselves.** Muse Spark reads where workers failed, asked and skipped, and proposes concrete edits. Every new phrase is checked by SAM on the expert's photo. A supervisor approves each change, and each approval creates a new version.

---

## Architecture

```
 POV camera + mic + speaker
 (iPhone PWA today -- Ray-Ban Meta glasses via the Wearables
  Device Access Toolkit behind the same interface)
          |  WebSocket, JSON frames
          v
 FastAPI backend  (backend/)
   |
   |-- meta_client.py   Meta Model API wrapper (OpenAI-SDK compatible)
   |      * muse-spark-1.3        reasoning over frame + grounding
   |      * sam-3.1               phrase-grounded detection
   |      * muse-voice-transcribe speech to text
   |
   |-- sam_parse.py     SAM token grammar -> typed boxes + confidence
   |-- procedures.py    capture mode: narration + keyframes -> procedure JSON
   |-- guide.py         guide mode: perception -> decision -> action loop
   |-- db.py            procedures, sessions, step events, evidence photos
   |
   v
 SQLite + evidence photos on disk
```

### Decisions worth defending

| Decision | Why |
|---|---|
| **Phone PWA, not a native app** | No Xcode, no provisioning, no App Store review. The phone exposes exactly what glasses do — POV camera, mic, speaker — so the client is a thin media source behind one interface. When the glasses land, only that layer changes. |
| **Boxes + confidence from SAM, not decoded masks** | Masks cost a decoder and buy nothing for verification. A box plus a confidence score is enough to tell the reasoning model *where* the part is, which is the entire point of grounding. |
| **Realtime ASR, batch as fallback** | Live captions while the expert records, and hands-free voice commands while a worker is guided, need partial transcripts. Batch transcription stays as the fallback for a capture whose live audio path fails. |
| **Live view at about 1 fps, sent only when it changes** | Capture samples a keyframe every 2 s. Guide sends a small frame about once a second, and only when the view changed; otherwise a tiny heartbeat. Grounding runs continuously, so a step can verify itself without the worker saying *done*. |
| **Tool calling, not free-text verdicts** | A constrained tool call is auditable and executable. It is also what makes this a genuine agentic loop rather than a prompt that returns an opinion. |
| **No model audio fallback** | Spark 1.3 does not take audio, so the only fallback is batch ASR. A failure is reported, never papered over. |
| **Pluggable reasoning backend** | The verification loop runs on the hosted Meta Model API *or* on Muse Glimmer deployed on our own infrastructure, switched by one env var. Both speak the OpenAI protocol, so the swap is a base URL. |

### Running on open weights

A factory's first question is not about accuracy. It is whether footage of their line leaves the site.

Ustad answers it by running the verification reasoning on **Muse Glimmer 30B**, a Meta open-weight model, deployed on infrastructure the operator controls — Modal for this build, equally a box in the plant room. Muse Glimmer is the right target: it is built for agentic tool calling, which is exactly what the verification loop does, and it fits a modest GPU budget where Muse Spark Open (816B MoE) does not.

```bash
bash scripts/deploy_open_model.sh     # creates the endpoint, prints the env vars
```

Then set `REASONING_BACKEND=open` and restart. `/api/health` reports which engine is live, and **every step event is tagged with the backend that judged it**, so `/api/metrics` returns a `checks_by_backend` breakdown — hard evidence of which model actually did the work rather than a claim in a slide.

SAM 3.1 and Muse Voice Transcribe stay on the hosted API; neither has an open-weights release. `OPEN_FALLBACK=1` means a cold-starting endpoint degrades to the hosted model rather than stranding a worker mid-procedure, and the log records honestly which backend handled each step.

---

## Running it

```bash
# 1. deps
pip install -r requirements.txt

# 2. key
cp .env.example .env     # then paste your Meta Model API key into MODEL_API_KEY

# 3. verify every model works before you build on it
python -m scripts.smoke_test

# 4. serve
uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

```bash
# run the unit + websocket tests (no API key needed)
python -m pytest tests/ -q

# seed one good procedure so the demo never opens on an empty screen
python -m scripts.seed_demo
```

- **Phone PWA** — `http://<host>:8000/` (home, capture, library, guide, report).
- **Supervisor dashboard** — `http://<host>:8000/dashboard`: the procedure library, a session log with the **evidence photo for every judged step**, and the headline metrics from `/api/metrics` (pass rate, errors caught, mean latency, and a `checks_by_backend` breakdown).

### Getting it onto the iPhone

`getUserMedia` requires HTTPS, so the phone cannot hit `http://<laptop-ip>:8000` directly. Tunnel it:

```bash
cloudflared tunnel --url http://localhost:8000
```

Open the printed `https://….trycloudflare.com` URL in **Safari** on the iPhone, then *Share → Add to Home Screen* to run it fullscreen without browser chrome.

**iOS notes already handled in the client:** speech synthesis is unlocked inside the first user gesture; `MediaRecorder` prefers `audio/mp4`; a screen wake lock is taken while the camera is live; the box overlay replicates `object-fit: cover` cropping so the boxes land on the right pixels.

For the demo, mount the phone on a cheap chest strap or head strap — that reproduces the glasses' point of view closely enough that the footage reads correctly on camera.

---

## Repo layout

```
backend/
  config.py        env + tunables
  meta_client.py   Meta Model API: reason / segment / transcribe
  sam_parse.py     SAM 3.1 token grammar parser
  procedures.py    capture mode
  guide.py         guide mode agent loop + tool definitions
  db.py            SQLite persistence and metrics
  main.py          FastAPI app, two WebSockets, REST, static hosting
web/
  index.html       landing (3D Ray-Ban Meta hero) + capture, library, guide, report
  dashboard.html   supervisor view: procedures, session log + evidence, metrics
  app.js           media capture, WebSocket protocol, speech, box overlay, reconnect
  app.css          Meta-blue design system (light launch-stage + dark camera cockpit)
  models/          glasses.glb, iphone.glb — rendered with <model-viewer>
scripts/
  smoke_test.py    one call per capability -- run this first
  evaluate.py      replay labelled frames -> accuracy table for the submission
  seed_demo.py     load one good procedure so the demo never starts empty
  deploy_open_model.sh   deploy Muse Glimmer 30B on Modal
tests/             unit tests (SAM parser, capture self-check, eval scoring)
                   + websocket tests (guide start/resume handshake)
```

## Evaluation harness

The number that matters is not "the demo worked." It is step-verification
accuracy against ground truth. `scripts/evaluate.py` replays a folder of
labelled frames through the **same decision code the live loop runs**
(`backend.guide.judge_step`) and prints a paste-ready table.

Name each frame with the ground truth encoded in the filename:

```
step_03_correct.jpg              step 3 done right     -> expect PASS
step_03_wrong_valve_open.jpg     step 3 planted error  -> expect FAIL
step_03_unclear_blurry.jpg       step 3 bad view       -> expect RETRY
```

```bash
python -m scripts.evaluate --procedure <id> --frames ./eval_frames --out report.md
```

| Metric | Result |
|---|---|
| Correct steps confirmed | % of PASS-labelled frames confirmed |
| Planted errors caught | % of FAIL-labelled frames caught |
| **Missed errors (false pass)** | the safety-critical number |
| False alarms (good flagged wrong) | over-strictness |
| Bad views correctly deferred | `request_better_view` instead of guessing |
| Mean / median / p95 latency | per check, from real calls |

Latency is also logged on every live event and surfaced in `/api/metrics` and
the dashboard, so a real trial produces its own numbers. Complementary field
measures — novice task time vs the paper method, expert minutes-to-procedure —
come from the factory pilot.

## Status

Built during the hackathon window (Oct 27 – Nov 4, 2026). Everything in this repo is new work for this event.

## Licence

MIT
