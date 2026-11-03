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
| D-022 | 11-03 | **The expert's own keyframe is kept per step and shown to the judge** | It is few-shot recognition on the exact machine, under that light, and it costs no extra call. Measured: no added latency (6.9 vs 6.8 s). The same photo powers "Show me". |
| D-023 | 11-03 | **Ask Ustad answers by tool call, never prose: `answer`, `check_now`, `escalate`** | The answer becomes an action: point SAM at a part, show the expert's photo, run a check, call a supervisor. An answer can never mark a step done; `check_now` goes through the normal judge and the safety gate. Questions about skipping or bypassing safety must escalate. |
| D-024 | 11-03 | **Ask runs at `minimal` effort** | Measured on real photos: 3.3 s vs 5.8 s at `low`, the same tool chosen 3/3. Waiting is the worst part of asking for help, and the safety rules above don't depend on reasoning depth. |
| D-025 | 11-03 | **Questions are detected narrowly; the Ask button widens it for 12 s** | Each answer is a model call that talks over the worker. Chatter to a colleague must not trigger it. Rate limit: 8 a minute, one at a time. |
| D-026 | 11-03 | **Answers and pointers are spoken "asides"** | Found in a browser test: an answer replaced the step instruction on screen. The worker must always see what to do. |
| D-027 | 11-03 | **A capture take outlives its socket (in memory, 20 min TTL)** | Factory Wi-Fi drops. The phone keeps recording and syncs with job-clock times; the server rebuilds the timeline in place. Not persisted to disk: a server restart loses an unfinished take (stated in STATUS). |
| D-028 | 11-03 | **Procedure build runs as its own task; its result is kept for a rejoin** | A build takes 30 s or more. Inline, a drop lost the procedure, and pings went unanswered, so liveness killed the socket. |
| D-029 | 11-03 | **One socket at a time: `retire()` the old one; replies go to the socket they arrived on** | Found in a browser test: the `online` event and the backoff timer both connected, and the sync wrote to a socket still connecting, losing the offline frames. |
| D-030 | 11-03 | **Liveness by app-level ping (7 s) and silence (20 s)** | Phones leaving Wi-Fi leave sockets half-open with no close event. The server answers `pong`, including while it builds. |
| D-031 | 11-03 | **Self-improvement edits steps in place only, never adds or removes steps, and can only add to safety content** | Step numbers stay aligned with run history and insights. A model must never weaken a hazard, a reading range or a timer. Every edit needs approval, and every new phrase is SAM-checked first. |
| D-032 | 11-03 | **Metrics count only judged checks** | Questions, skips and notices are events, not checks; counting them skewed the pass rate. |
