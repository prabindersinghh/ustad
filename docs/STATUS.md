# Status

**Last updated:** 2026-11-03 13:40 IST · **Head:** `12d9a9f` · **Tests:** 120 passing
**Deadline:** Nov 4 2026, 9 PM ET (Nov 5, 7:30 AM IST) · **Spend:** $2.16 of the $50 working cap

## In one paragraph

Ustad works end to end on a real iPhone.

**Capture.** An expert records a job once (camera plus narration). Ustad
writes a step-by-step procedure that quotes the expert's own words, keeps the
expert's photo of every step with the part boxed on it, and checks every
target, hazard and tool phrase against that footage with SAM 3.1.

**Guide.** A worker then runs the job hands-free:
- SAM 3.1 tracks the target, hazards, tools and hands live.
- Muse Spark verifies each step by tool call, comparing the live view with
  the expert's photo.
- The worker can ask anything ("which one?", "it won't turn"). Ustad answers
  from the expert's words, points at the part, and shows the expert's photo.
- After two failed checks it coaches unasked.
- A dropped network costs nothing. Capture keeps recording and syncs later,
  and guide resumes at the exact step.

**After the job.** Every job yields a printable evidence pack. Muse Spark reads
the runs and proposes better procedures. A supervisor approves them, and each
approval makes a new version.

## Works, and how we know

| Capability | Evidence |
|---|---|
| Capture build is fast and survives a drop: 17.2 s for the user's real take (was 87 s); browser test delivered the procedure 15 s after a mid-build drop | Real models 2026-11-03 22:30; browser test |
| Long recordings (up to 2 h): written in parts while recording, merged at stop | Real models: 3 parts, 9.2 s left at stop; test |
| iPhone mic works (worklet path) | iPhone `diag` 2026-11-03 13:49 |
| Capture builds a correct procedure from camera plus narration | Live API run and iPhone runs (e.g. 2026-11-02 23:33, 7 keyframes, 4 steps) |
| Capture keeps each step's expert photo, SAM box and the expert's quoted words | Tests; real capture in the browser 2026-11-03 13:30 saved 4 photos with boxes |
| Judge compares the live view with the expert's photo | Real photos: 4/4 right with or without the reference, no added latency (6.9 vs 6.8 s median) |
| **Ask Ustad**: spoken or typed questions answered by tool call (`answer` with `point_at` / `show_reference`, `check_now`, `escalate`) | Real models on real photos: "which cap" pointed and SAM found it, "can I skip" escalated, "I think it's off" checked. 3/3 at both efforts. Browser end to end. 7 tests |
| "Show me": the expert's photo with the part boxed, plus the live highlight | Browser screenshot 2026-11-03; tests |
| Coaching after 2 failed checks | Real model coached with the expert's quote and pointed at the cap; test |
| Capture survives a network drop: keeps recording, syncs frames, marks and narration on rejoin, delivers a procedure built while offline | Browser: 5 offline frames plus 1 mark synced, then a real build. 4 tests |
| Guide survives a network drop: unlimited retries, "done" tapped offline is checked on reconnect, resumes past skips with the attempt count | Browser: offline, tap, reconnect, correct verdict. Tests |
| Half-open sockets detected (ping every 7 s, dead after 20 s silent) | Code; pong tested |
| Evidence pack per job (`/evidence?s=`): steps, photos with SAM boxes, trail, questions, outcome, printable | Browser screenshot; tests |
| Self-improving procedures (supervisor token required; safety wording can't be dropped): Spark proposes, SAM checks phrases on the expert photo, supervisor approves, new version, history kept | Live run gave 1 evidence-backed suggestion from 1 run. 5 tests, including safety (hazards only added, readings untouchable, stale suggestions refused) |
| Self-check rewrites target, hazard and tool phrases SAM can't find; unfound ones are marked "not trackable" | Unit tests plus a real SAM probe |
| Live guide: grounding about 1 fps, 2 SAM calls in flight, two-pass auto-advance | Live API e2e; iPhone 2026-11-02 |
| Voice: realtime ASR, done / repeat / skip / why / where, plus questions | Live runs; intent and question tests |
| Safety gate: lockout, reading, timed and confirm steps never inferred | 3 tests; security review fix |
| Step kinds, multi-object live view, hand-near-hazard, left-behind tools | Earlier commits; tests and real SAM probes |
| iPhone camera recovery, self-healing mic, phone telemetry | Browser fault injection; tests |

## Not verified yet (do not claim)

- **Accuracy numbers.** No factory trial yet; `scripts/evaluate.py` is ready.
- **The new features on the user's iPhone.** All were tested in a desktop
  browser with a fake camera and with real models. Next iPhone run: Ask, Show
  me, and a drop (turn Wi-Fi off mid-job).
- **Muse Glimmer on Modal.** Needs the user's Modal login.

## Known issues

| Issue | Impact | Plan |
|---|---|---|
| Public URL is a Cloudflare *quick* tunnel: random name, dies on restart | Link changes | Named tunnel or ngrok static domain (user to choose) |
| An explicit "done" check took about 6.8 s median on 2026-11-03 (2.4 s measured on 11-02 with smaller frames) | Slower tap-to-verdict | Background auto-advance usually beats the tap. Re-measure; consider `minimal` for taps with a fresh grounding |
| Ask answers take about 3.3 s (`minimal`) | A pause before the answer | "Thinking" shown on screen. Acceptable for now |
| Server restart loses an in-progress capture take (state in memory; frames on disk are cleared at start) | Phone shows "server restarted, record again" | Persist takes to disk if it matters |
| Procedures captured before today have no expert photos | Show me and reference judging fall back gracefully | Re-capture demo jobs |
| Hand-near-hazard runs at about 1 fps with 2 to 3 s latency | Advisory only | Stated plainly; never marketed as a guard |

## Next up (see ROADMAP.md)

1. User tests on the iPhone: Ask, Show me, Wi-Fi off mid-capture and mid-job.
2. Re-capture the demo jobs so they carry expert photos and quotes.
3. Stable URL · Glimmer on Modal · Hindi voice · factory trial and
   `evaluate.py` · 3 minute video · **make the repo public**.
