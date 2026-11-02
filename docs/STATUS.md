# Status

**Last updated:** 2026-11-03 00:30 IST · **Head:** `b59d3d3` + docs · **Tests:** 74 passing
**Deadline:** Nov 4 2026, 9 PM ET (Nov 5, 7:30 AM IST) · **Spend:** $1.29 of the $50 working cap

## In one paragraph

Ustad works end to end on a real iPhone. An expert records a job once
(camera plus narration). Ustad writes a step-by-step procedure, checks every
target, hazard and tool phrase against the expert's own footage with SAM 3.1,
and saves it. A worker then runs the job hands-free. The phone streams about
one frame a second. SAM 3.1 tracks the target, hazards, tools and hands live,
and Muse Spark verifies each step with a tool call. Steps auto-advance after
two confident passes, a voice "done" triggers a check, and a hand near a
hazard is called out. Safety steps can never be skipped by inference. A
dashboard shows spend, per-step insights and every session's evidence.

## Works, and how we know

| Capability | Evidence |
|---|---|
| Capture builds a correct procedure from camera plus narration | Live API run and iPhone runs (e.g. 2026-11-02 23:33, 7 keyframes, 4 steps) |
| Self-check rewrites target, hazard and tool phrases SAM can't find; unfound ones are marked "not trackable" | Unit tests plus a real SAM probe: "circular saw blade" flagged, "gear" kept |
| Live guide: grounding about 1 fps, 2 SAM calls in flight, two-pass auto-advance | Live API e2e (`e2e_guide.py`, scratchpad); iPhone end-to-end test 2026-11-02 |
| Voice: realtime ASR, "done / repeat / skip / why / where", stable-partial trigger (~0.45 s) | Live runs; `tests/test_live.py` intents |
| Jump-ahead: skipped visual steps credited when the end state proves them | Live probe ("step 4 end state requiring prior pickup") plus tests |
| Safety gate: lockout, reading, timed and confirm steps never inferred | 3 tests; security review fix |
| Industrial step kinds: visual, reading (range checked server-side), timed, confirm | Complex-machine narration produced all four; tests |
| Multi-object live view: target (blue), hazard (red), tool (green), hands | Real SAM probe, about 2 to 3 s for 4 parallel calls |
| Hand-near-hazard alert (spoken, banner, flash) | `hand_near` True on the blade, False when clear (real SAM) |
| Left-behind tool check at job end | Unit test; real SAM finds the spanner |
| Recording UX: countdown, clock, pause and resume, mark step, 10 min limit | Browser tests; iPhone |
| Spend meter and budget guard (live watching off at 95% of cap) | `/api/usage`; test |
| iPhone camera recovery: black frames never sent, muted or ended track shows Retry | Browser fault-injection tests (2026-11-02 21:49) |
| Mic self-heals: worklet, then ScriptProcessor, then backup recording | Browser fault-injection: worklet 2.8 s fallback, double failure throws |
| Phone telemetry (`diag` messages, `/api/clientlog`, `guide close:` summary) | Tests; not yet seen from a real iPhone |

## Not verified yet (do not claim)

- **Accuracy numbers.** No factory trial yet. `scripts/evaluate.py` is ready.
  The landing page Proof rows say "in progress" until it runs.
- **The mic fix on the user's iPhone.** It was built from logs that showed
  0 bytes of audio from the phone. The next iPhone run's `diag` line will
  confirm which audio path it lands on.
- **Muse Glimmer on Modal** (open weights). The code path exists
  (`REASONING_BACKEND=open`) but has not been deployed. It needs the user's
  Modal login.

## Known issues

| Issue | Impact | Plan |
|---|---|---|
| Public URL is a Cloudflare *quick* tunnel: random name, dies on restart | Link changes; error 1033 when stale | Named tunnel or ngrok static domain (user to choose) |
| A guide session on the iPhone (2026-11-02 23:33) received zero frames | That session checked nothing | Likely a dark view; now spoken to the worker and reported via `diag`. Confirm on the next run |
| SAM misses drawn or unusual objects by their real name (drawn saw blade read as "gear") | Hazard silently unwatched | Fixed: self-check rewrites or marks "not trackable" |
| Hand-near-hazard runs at about 1 fps with 2 to 3 s latency | Advisory only, not a safety device | Stated plainly in UI copy and docs; never marketed as a guard |
| README "Decisions" table predates realtime ASR | Misleading to a reader | Corrected 2026-11-03 |

## Next up (see ROADMAP.md for the full ranked list)

1. Confirm the reliability fixes on the iPhone (read the `diag` line).
2. X-factor build, pending the user's pick: conversational "Ask Ustad",
   expert reference frames in verification, self-improving procedures,
   evidence pack.
3. Stable URL · Glimmer on Modal · factory trial and `evaluate.py` · 3 minute
   video · **make the repo public**.
