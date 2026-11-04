# Roadmap

Planned work, ranked by value to the problem and to the judging criteria
(Impact, Use of Meta AI, Technical depth, Demo; bonus prizes for Best
Agentic, Open Weights and Most Viral). Move items to **Done** with the
commit when they land.

## Honest capability today

Where Ustad stands on real machines, so the plan aims at the real gaps.

| Works well | Struggles |
|---|---|
| **Discrete, visible state changes:** a lever moved, a guard closed, a part placed, a valve turned, a can flipped. | **Small or look-alike parts:** one bolt of twelve, a connector seated or not. The live view is 720 px, so SAM finds "a bolt", not "the third bolt". |
| **Gauge readings** with a range check. | **Things a camera can't see:** torque, a click, a seal, a smell. These must be `confirm` steps. |
| **Named, everyday objects** (e-stop, spanner, valve, panel). | **Unusual or drawn objects** SAM names differently (handled by the self-check). |
| **Linear procedures of 4 to 15 steps.** | **Branches** ("if the light is red, do X") and **troubleshooting**: the procedure is linear. |
| **Timed dwells, operator confirmations.** | **Occlusion and one viewpoint:** hands and the machine hide the part. |
| | **Fast hazards:** about 1 fps with 2 to 3 s latency is a coaching cue, not a guard. |
| | **Accuracy is unmeasured** until a factory trial. |

**Verdict:** ready for simple to moderately complex machine procedures
(changeovers, start-up and shut-down, lockout, filter and blade changes,
inspection rounds). Not yet for fine assembly verification or diagnosis of
unknown faults. The ranked items below close the gaps that matter most for a
demo and for a factory.

## Ranked next work

| # | Item | What it adds | Gap closed | Est. | Status |
|---|---|---|---|---|---|
| 1 | **Ask Ustad: conversational guide** | Ask by voice or typing; answers from the expert's words and the live view; points with SAM; "Show me" the expert's photo; coaching after 2 fails; escalate to a supervisor. | Help when stuck; the "expert in your ear" | | **Done** (11-03) |
| 2 | **Verify against the expert's photo** | `judge_step` sees the expert's keyframe first. | Recognition of the right part on this machine | | **Done** (11-03) |
| 3 | **Self-improving procedures (agent loop)** | Spark proposes edits from run evidence, SAM checks phrases, supervisor approves, versions. | Best Agentic | | **Done** (11-03). Later: proposals that split or add steps (needs step-id remapping) |
| 4 | **Evidence pack per job** | Printable page per job. | Compliance value | | **Done** (11-03). Later: signed or hashed record |
| 4b | **Offline resilience** (user request) | Capture keeps recording through a drop and syncs; guide resumes exactly. | Real factory Wi-Fi | | **Done** (11-03). Later: persist takes to disk |
| 5 | **Hindi voice** | Spark writes and speaks steps in Hindi (`hi-IN` TTS on iPhone); intents in Hindi. | Real Indian shop floors | | **Done** (11-04), guide side. Not yet on a real phone voice or factory noise |
| 6 | **Smooth AR box between SAM results** | Track the box on the phone between 1 fps results (optical flow or interpolation). | The live view feels instant | 2 h | Optional |
| 7 | **Muse Glimmer on Modal** | Verification on open weights, on infrastructure the plant controls. | Open Weights prize; data stays on site | 1 to 2 h | Blocked: Modal needs a payment method for any GPU (login done 11-04) |
| 8 | **Stable public URL** | Named Cloudflare tunnel or ngrok static domain. | Demo link that never changes | 30 min | User wants a working link for judges: host to be agreed |
| 9 | **Factory trial, then `evaluate.py`** | Real accuracy numbers to replace "in progress" on the landing page. | Proof; Impact | Half a day on site | User |
| 10 | **3 minute demo video** | Capture once, guide hands-free, catch a mistake, ask a question, evidence pack. | Demo; Most Viral | 3 h | After 1 to 4 |
| 11 | **Make the repo public** | Required for judging. | Submission | 1 min | **Before Nov 5, 6:30 AM IST** |

## Research notes (2026-11-03)

What funded startups and incumbents do, and where Ustad stands apart.

- **NavigateAI** ($25M seed, led by Elad Gil, 2026): an AI copilot for field
  workers in construction, infrastructure and industrial operations. It runs
  on Meta smart glasses, sees what the worker sees, gives instructions in the
  line of sight, and has a phone mode. It **validates the category and the
  Meta-glasses direction**. Ustad's differentiators: procedures captured from
  one expert take, and every step *proven* by perception plus a tool call,
  with an evidence trail.
- **Evos** ($9M pre-seed, Uncork Capital, 2026): agents watch human workers
  do manual industrial tasks, then automate them. Same "learn by watching"
  insight, aimed at replacing the worker. Ustad keeps the worker and makes
  them expert-level.
- **Retrocausal** (Assembly Copilot, Siemens partner): camera-based step
  verification on assembly lines, with vendor-reported error capture around
  80%. Fixed-camera, per-station, trained per line. Ustad is zero-training
  (one expert take) and goes wherever the worker goes.
- **Augmentir** (Frost & Sullivan #1 innovator, connected worker): AI work
  instructions that **adapt to each worker's skill level**, skills matrix,
  agents. That suggests a later step for Ustad: guidance that shortens for
  workers who keep passing first time.
- **Aquant** (service AI): "retrieval-augmented *conversation*". The
  assistant asks clarifying questions instead of dumping steps. This informs
  Ask Ustad (#1).
- **Handbuch2AR** (research, smart glasses): answers cite the exact manual
  page. Ask Ustad's equivalent is citing the expert's timestamped words.
- **SAM 3.1** adds Object Multiplex: many objects tracked in one pass (about
  7x at 128 objects on an H100, per secondary sources). Hosted SAM here is
  per-image, so multi-object live grounding stays parallel calls. Streaming
  video tracking would need self-hosting (later).
- **Meta Wearables Device Access Toolkit:** camera streaming from Ray-Ban
  Meta glasses to a phone app, in developer preview (native iOS and Android;
  publishing still restricted). This is the path from phone to glasses after
  the hackathon.
- **Workforce data** worth citing (primary source only): Deloitte and the
  Manufacturing Institute 2024 (already on the landing page). Vendor
  percentages such as "60% faster training" or "45% of knowledge lost" have no
  primary source: **do not use**.

## Done

| Item | Commit |
|---|---|
| Smoke test, capture self-check, dashboard, `evaluate.py`, polish | `8a21278` |
| Realtime voice, live AR tracking, auto-verify, spend meter | `dc75f3e` |
| Step kinds, jump-ahead, skip audit, recording UX, insights | `5022300` |
| Safety gate | `d024299` |
| Multi-object detection, hand-near-hazard, left-behind tools, phrase self-check | `c020f1f` |
| iPhone camera recovery | `39c7562` |
| Self-healing mic, backup narration, dark notice, telemetry | `b59d3d3` |
| Project docs | `d52721e` |
| Expert reference, Ask Ustad, Show me, coaching, offline resilience, evidence pack, self-improving procedures | `12d9a9f` |
