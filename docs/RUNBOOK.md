# Runbook

How to set up, run, test, put on a phone, and troubleshoot Ustad. The
commands are for Windows with Git Bash (the dev machine). On macOS or Linux,
use `.venv/bin/python` instead.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env          # then put MODEL_API_KEY in it. Never print or commit .env.
```

ffmpeg must be on PATH, or set `FFMPEG` (used to transcode audio for batch ASR).

## Run

```bash
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8800
```

- App: http://localhost:8800 · Dashboard: http://localhost:8800/dashboard
- The camera needs HTTPS on a phone, so use a tunnel (below).
- Static files (`web/`) are served live. Backend changes need a restart.
- **Cache busting:** after changing `web/app.js` or `web/app.css`, bump
  `?v=` in `web/index.html`. Otherwise phones keep the old file.

## Put it on a phone

Quick tunnel (random URL, dies on restart, fine for testing):

```bash
"C:/Program Files (x86)/cloudflared/cloudflared.exe" tunnel --url http://localhost:8800
```

The URL is printed as `https://<words>.trycloudflare.com`. If the phone
shows error 1033 or 1016, the tunnel died: restart it and share the new URL.
For a link that never changes, use a named Cloudflare tunnel (needs a domain
on Cloudflare) or an ngrok static domain (needs an ngrok authtoken).

On the iPhone:
- Open the link **in Safari**, not inside WhatsApp or Gmail.
- Allow camera and microphone.
- **Close other Ustad tabs:** iOS gives the camera to one tab at a time.
- If permission was denied once: tap **aA** in the address bar, then Website
  Settings, and set Camera and Microphone to Allow.

## Test

```bash
.venv/Scripts/python.exe -m pytest -q          # 120 tests, no network, no cost
```

These hit the real APIs and **cost money** (cents):

```bash
.venv/Scripts/python.exe scripts/smoke_test.py      # 5 live checks
.venv/Scripts/python.exe scripts/seed_demo.py       # demo procedure
.venv/Scripts/python.exe scripts/evaluate.py        # accuracy on labelled runs
```

Spend so far: `curl -s localhost:8800/api/usage`.

## Reading phone telemetry

Every guide session ends with one line:

```
guide close: session 72633c51f3fe, step 1, 0 frames, 0 dark, 0 audio bytes
```

- **0 frames:** the phone sent no live view. Check the `diag` line's
  `video` and `dark` fields.
- **0 audio bytes:** no mic audio. Check `path` and `ctx`.

The phone reports every 15 s:

```
diag guide <session> {'video': 'live', 'audio': 'live', 'ctx': 'running', 'rate': '48000',
                      'path': 'script', 'mic_chunks': '412', 'dark': 'False', 'frames': '37', ...}
```

| Field | Healthy | Trouble |
|---|---|---|
| `video`, `audio` | `live` | `live/muted` (another tab or app, or a call); `ended` (track lost: Retry) |
| `ctx` | `running` | `suspended` or `interrupted` (iOS audio session) |
| `path` | `worklet` or `script` | `stalled`: no audio path works, so capture uses the backup recording |
| `mic_chunks` | grows about 10 a second | stuck at 0 |
| `dark` | `False` | `True`: lens covered or camera muted |

Script errors from phones appear as `client error: <message> | <user agent>`.

## Testing the new features on a phone

- **Ask Ustad:** in a job, tap **Ask Ustad** and say "which one?", or just ask
  out loud ("how tight should it be?"). Server log: `ask (voice) '...' -> answer in N ms point_at=...`.
- **Show me:** tap **Show me**, or say "where is it". You should see the
  expert's photo with the part boxed. Only procedures captured since
  2026-11-03 have expert photos; re-capture older ones.
- **Network drop:** turn Wi-Fi off mid-capture for 10 s, then on. The clock
  shows "offline", the badge counts frames kept, and the log shows
  `capture <id> rejoined`. Mid-job: tap Done while offline; it checks on
  reconnect. Log: `guide resume: session ... at step N`.
- **Evidence pack:** the report screen's button, or the dashboard session's
  "evidence pack" link.
- **Improve:** dashboard, pick a procedure with runs, "Suggest
  improvements" (one Spark call plus SAM checks, about a cent). The
  dashboard asks once for the **supervisor token**: `SUPERVISOR_TOKEN` in
  `.env`, or else the contents of `data/supervisor_token.txt` (created on
  first start, gitignored). Don't paste it anywhere public.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "The camera was taken away" | Another tab or app has the camera, or a call or Siri interrupted it | Close other tabs, tap Retry |
| Procedure about "a black screen" | Fixed: black frames are no longer sent | Update the phone (hard refresh) |
| No live captions in capture | iOS audio path stalled | Narration is still recorded and transcribed at stop; check `diag path` |
| Guide never checks anything | Phone view dark, or `confirm` step | A dark view is now spoken; see `guide close:` |
| "Procedure generator produced no usable steps" | Take too short, or no frames | Record 1 to 5 minutes, narrate each step |
| "The server restarted and lost this recording" | Server restarted mid-take (takes are kept in memory) | Record again |
| Show me says "I can't see it" with no photo | Procedure captured before expert photos existed | Re-capture it |
| Spend near the cap | Live watching uses SAM about every second | Budget guard turns watching off at 95%; raise `BUDGET_USD` only deliberately |

## The public link (Modal)

https://prabindersinghh--ustad-web.us-east.modal.run. Runs without this laptop. Uses Modal's CLI (`pip install modal`, in its
own environment, not the project's):

    modal deploy deploy/modal_app.py          # ship code changes (about 1 min)
    python deploy/modal_setup.py              # after changing .env settings
    modal app logs ustad                      # what the server is doing
    modal app stop ustad                      # stop paying: do this after judging

Costs about $0.06 an hour while up. `python deploy/modal_setup.py --seed`
**overwrites the cloud's data** with the chosen jobs from the laptop: don't run
it once real runs exist there.

## Muse Glimmer second opinion

Open a job's evidence pack (`/evidence?s=<session>`) and press **Get a second
opinion**. It needs the supervisor token. Photos go to NVIDIA's API: don't use it
on footage showing faces or anything confidential. Settings: `OPEN_BASE_URL`,
`OPEN_API_KEY`, `OPEN_MODEL`, with `REASONING_BACKEND` left on hosted. Live
guiding on Glimmer (`REASONING_BACKEND=open`) would mean about a minute per check.

## Deadline checklist

- [x] Test procedures left out of the cloud copy (the laptop's list is unchanged)
- [ ] Repo **public** (GitHub, Settings, Danger zone) before Nov 5, 6:30 AM IST
- [x] Stable demo URL that doesn't need this laptop on: https://prabindersinghh--ustad-web.us-east.modal.run
- [ ] 3 minute video
- [ ] Submission form
