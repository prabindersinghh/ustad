# Status

**Last updated:** 2026-11-04 IST · **Tests:** 283 passing
**Deadline worked to:** Nov 5, 6:30 AM IST (9 PM ET is 7:30, one listing says 6:30) · **Spend:** $10.61 of the $50 working cap (`/api/usage`, 20:41)

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
| Muse Glimmer second opinion on a finished job: same judge, model id recorded, can't affect live guiding | 8 cases 2026-11-04: Glimmer 8/8 at 63 s median, Muse Spark 8/8 at 4.6 s, no false passes. Real run through the public link. 11 tests |
| Landing page shows real MCPL photos, in the Proof section | Checked at phone and desktop width |
| Demo film, 2:59, outside git at `data/factory/film/renders/ustad_demo.mp4`. Rebuild: `tools/build.py`, then `npx hyperframes render` in `data/factory/film` | Frames viewed and length checked. **Audio not heard by anyone yet** |
| Replay and overlay: recorded footage through the real guide code | Sessions `9570097bec90`, `f359cde1ded5`. SAM found 9 of 25 object and frame pairs at 480 px; 2x did not help |
| Edge-case review: Glimmer re-checks only a job's doubtful steps | One real run on a viewed photo: agreed, confidence 0.78, 84 s |
| Public link protected: demo sign-in, spending limits ($25 total, $4 an hour on the cloud counter), session limits | 19 tests, real browser at phone size, checked on the public link |
| Permanent public link, no laptop needed: https://prabindersinghh--ustad-web.us-east.modal.run | Public address 2026-11-04: real job, browser job, data survives a redeploy. Not yet tried on an iPhone |
| Hindi guide: spoken and on-screen steps, verdicts and answers; Hindi voice commands; numbers checked in code | Real models on the user's job 2026-11-04; browser at phone size; 40 tests |
| Guiding is fast: about 7 s per step from the finished state to the next instruction (was 11.5 to 30 s), on the user's real job; instant answer to "what do I do" | Real models 2026-11-04 01:30, same photos old vs new; tests |
| Run any procedure again from the dashboard (link, QR, copy) or from the report | Browser 2026-11-04 |
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
| `scripts/replay.py`: a video file through the real `guide.verify_step`, saved as a session named `replay: <file>` with video times and boxes | Real run on the factory clip (session `9570097bec90`, 4 of 5 verified, 1 better view); 10 tests |
| `scripts/capture_video.py --narration-file`: a procedure from a video, with a `.txt` or audio narration in place of its own sound | Real run on the factory clip (`218406178c38`); 7 tests |
| `scripts/render_overlay.py`: a replay's real boxes and verdicts over the video, the app's best box only, nothing over a title card, no box across a cut | Rendered session `9570097bec90`; checked every title card frame against the source; 14 tests |
| The real app runs end to end in headless Chromium with footage as the camera and speech as the mic: real ASR, writer, self-check, guide | Capture `284e455eec7b`, guide session `f359cde1ded5` (4 of 4 passed), Hindi session `7c86ee8fe687` |

## Not verified yet (do not claim)

- **Accuracy numbers.** No factory trial yet; `scripts/evaluate.py` is ready.
- **Hindi on a real phone with a real voice.** Speech was tested on synthetic
  Hindi audio, on-device speech in a desktop browser. Not yet: an iPhone's
  Hindi voice, a real worker's accent, factory noise.
- **Muse Glimmer on hardware we control.** It has only run through NVIDIA's API.
  Modal refuses any GPU without a card, despite the user's $100.99 credit. The
  on-premise tier is labelled "planned" on the landing page.
- **SAM on the factory clip's draft phrases.** Measured at 480 px: 2 of 5 phrases
  ground. "red ink pad", "teal silicone pad" and "rotary fixture" were found on
  0 of 5 frames, and 2x did not change that. Do not claim SAM grounded every
  step of that replay. The procedure is a draft for the user to correct.
- **An "error caught" on the factory clip.** The real guide run flagged nothing: 4 passes, 0 errors, 2 live hand
  warnings. The demo's error moment is from our own test run (session `af3b0ea0a00f`, a bottle on a desk), and is
  labelled so. Don't describe it as a factory result.
- **Whether a partner API qualifies for the open-weight prize.** Official rules
  not found online; the user should check.
- **The edge-case review on real Glimmer.** Built and tested with mocked models,
  page checked on a synthetic job. Not yet run against the model.
- **A fine-tuned Glimmer does not exist.** What runs on NVIDIA today is base
  Glimmer. Built and run: the episode set (`scripts/make_episodes.py`). Not run:
  SFT, GRPO, any eval. No accuracy, loss or reward has been measured. Nothing may
  be called fine-tuned or RL-trained until a training run has really happened
  and been measured.

## Known issues

| Issue | Impact | Plan |
|---|---|---|
| Three target phrases in the draft factory procedure (`f01d7d9cc120`) are never found by SAM: "red ink pad", "teal silicone pad", "rotary fixture", 0 of 199 frames | A guided run of that draft gets "no match" on steps 3, 4 and 5, and the judge is told to ask for a better view when the frame doesn't clearly show the criteria. Not tried end to end | Rewrite the phrases before demoing that procedure. Of 18 alternatives tried on 5 frames, only "red pad" (2 of 5) and "red rubber pad" (2 of 5) found the part; "machine" also matched but is too broad |
| SAM's "human hand" misses fingertips at the frame edge: 19 of 153 hand frames in the factory clip got no detection at 0.5 confidence or above | Hand-near-hazard can miss a hand there. Advisory only, as already stated | Stated plainly. The judge also sees the image |
| A restart of the Modal container loses up to 30 s of data and any capture take in progress | Rare. The guide resumes from saved steps; a take must be recorded again | Acceptable for the demo |
| The laptop and the Modal link keep separate databases and separate spend counts | The budget guard sees each one's own spend. Check Meta's billing for the true total | Use the Modal link from now on |
| An explicit "done" check took about 6.8 s median on 2026-11-03 (2.4 s measured on 11-02 with smaller frames) | Slower tap-to-verdict | Background auto-advance usually beats the tap. Re-measure; consider `minimal` for taps with a fresh grounding |
| Ask answers take about 3.3 s (`minimal`) | A pause before the answer | "Thinking" shown on screen. Acceptable for now |
| Server restart loses an in-progress capture take (state in memory; frames on disk are cleared at start) | Phone shows "server restarted, record again" | Persist takes to disk if it matters |
| Procedures captured before today have no expert photos | Show me and reference judging fall back gracefully | Re-capture demo jobs |
| Hand-near-hazard runs at about 1 fps with 2 to 3 s latency | Advisory only | Stated plainly; never marketed as a guard |

## Next up (see ROADMAP.md)

1. User: factory demo and video; share the factory media for the landing page.
2. iPhone run on the Modal link.
3. Show the second opinion in the demo video; only on footage without faces.
4. Remove test procedures just before submitting · **make the repo public**.
