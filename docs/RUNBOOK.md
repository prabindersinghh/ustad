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
.venv/Scripts/python.exe -m pytest -q          # 75 tests, no network, no cost
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

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| "The camera was taken away" | Another tab or app has the camera, or a call or Siri interrupted it | Close other tabs, tap Retry |
| Procedure about "a black screen" | Fixed: black frames are no longer sent | Update the phone (hard refresh) |
| No live captions in capture | iOS audio path stalled | Narration is still recorded and transcribed at stop; check `diag path` |
| Guide never checks anything | Phone view dark, or `confirm` step | A dark view is now spoken; see `guide close:` |
| "Procedure generator produced no usable steps" | Take too short, or no frames | Record 1 to 5 minutes, narrate each step |
| Spend near the cap | Live watching uses SAM about every second | Budget guard turns watching off at 95%; raise `BUDGET_USD` only deliberately |

## Deadline checklist

- [ ] Repo **public** (GitHub, Settings, Danger zone) before Nov 5, 7:30 AM IST
- [ ] Stable demo URL
- [ ] 3 minute video
- [ ] Submission form
