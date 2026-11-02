# Decisions

Why Ustad is built the way it is. Read before changing something that looks
odd: it may be deliberate. New non-obvious choices get a new entry at the
bottom (next id), never an edit that hides the old reasoning. When a decision
is reversed, mark it **Superseded by D-0xx**.

| Id | Date | Decision | Why |
|---|---|---|---|
| D-001 | 11-01 | **Phone PWA, not a native app** | No Xcode, no provisioning, no App Store review. The phone exposes what glasses do (POV camera, mic, speaker), so the client is a thin media source behind one interface. Meta glasses need the native Wearables Device Access Toolkit (iOS/Android, developer preview), which is a later swap of that layer. |
| D-002 | 11-01 | **Tool calls, not free-text verdicts** | `verify_step`, `flag_error` and `request_better_view` are auditable and executable. That is what makes the loop agentic, not a prompt that returns an opinion. |
| D-003 | 11-01 | **Boxes plus confidence from SAM, not masks** | Masks cost a decoder and add nothing to verification. A box and a score tell the reasoner where the part is. |
| D-004 | 11-01 | **The capture self-check grounds every target phrase on the expert's own keyframe** | A procedure ships only with phrases the perception model can find in that machine's own footage, under that light and from that angle. That is what makes later verification trustworthy. |
| D-005 | 11-01 | **Pluggable reasoning backend (hosted or Muse Glimmer)** | A factory's first question is whether footage leaves the site. Both backends speak the OpenAI protocol, so the swap is a base URL. Every step event records which backend judged it. |
| D-006 | 11-02 | **Batch ASR replaced by realtime ASR** (supersedes the README's original "batch ASR" row) | Live captions and voice commands need partials. Batch stays as the fallback for capture. |
| D-007 | 11-02 | **No Spark audio fallback** | Spark 1.3 doesn't take audio; the 1.2 path returned errors. Batch ASR is the only fallback. |
| D-008 | 11-02 | **Model calls run in `asyncio.to_thread`** | Blocking SDK calls froze the event loop and dropped WebSockets on the phone. |
| D-009 | 11-02 | **Live watching at about 1 fps, 2 SAM calls in flight, phone-side change gating** | A SAM round trip is about 2 s. Two in flight roughly double how often the box moves. Not sending unchanged views (a `still` heartbeat instead) makes a camera resting on the bench nearly free. |
| D-010 | 11-02 | **Auto-advance needs two confident passes on a newer, or held-still, view** | One pass on one frame is a guess. Two agreeing looks cut false advances without making the worker wait for a tap. |
| D-011 | 11-02 | **Reasoning effort: `low` for explicit checks, `minimal` for background looks** | Measured on fixed frames: default about 3.95 s, low about 2.4 s, minimal about 1.8 s, all 6/6 correct. Background looks are confirmed twice anyway. |
| D-012 | 11-02 | **Voice "done" fires on a stable partial (0.45 s), not on end of speech** | Waiting for end of utterance took about 3 s. Utterances over 6 words, or containing "not", are ignored ("I'm not done"). |
| D-013 | 11-02 | **Metered spend and a budget guard in the product** | Hackathon credit is finite ($150, working cap $50). Live watching is the expensive part, so it switches off at 95% and the job continues with tap and voice checks. |
| D-014 | 11-02 | **Safety gate: the model can never skip a worker past lockout, reading, timed or confirm steps** | Found in a security review. Jump-ahead is useful for visual steps whose end state proves them. For safety steps, "it looks done" is not evidence. `completed_through` is advisory and `inferable()` decides. Worker speech is quoted as data, never instructions. |
| D-015 | 11-02 | **Step kinds: visual, reading, timed, confirm** | Real machines need gauge readings in range (checked on the server, not trusted from the model), dwell times and operator confirmations that a camera can't see (e.g. "smell for solvent"). |
| D-016 | 11-02 | **Hazards and tools are watched live, but the alert is advisory** | At about 1 fps with 2 to 3 s latency, hand-near-hazard is a coaching cue, not a safety interlock. The docs and UI never present it as a guard. |
| D-017 | 11-02 | **Untrackable phrases are marked, not hidden** | SAM read a drawn saw blade as "gear". A hazard SAM can't find must show as "not trackable" on the dashboard, never be silently unwatched. It also costs no SAM call per frame. |
| D-018 | 11-02 | **Black frames are never sent** | A muted iOS camera still delivers frames, all black. They produced a procedure about "a black screen" and wasted SAM calls. |
| D-019 | 11-03 | **The mic proves itself: worklet, then ScriptProcessor, then backup recording** | On the user's iPhone the AudioWorklet path delivered nothing, silently. A running AudioContext is not proof of audio; the first chunk is. |
| D-020 | 11-03 | **Phones report telemetry to the server** | Field glitches were invisible. Two silent failures (0 audio bytes, 0 frames) were only found by reading server counters. Untrusted input is escaped, capped and rate-limited (security review). |
| D-021 | 11-03 | **Docs live in the repo and are updated in the same commit as the code** | The user's requirement: any developer or agent must be able to pick the project up cold. `AGENTS.md` is the entry point. |
