/* Ustad PWA client.
 *
 * Runs in iPhone Safari. The phone is a stand-in for AI glasses: it supplies
 * a point-of-view camera, a microphone and a speaker, which is exactly the
 * surface the Meta Wearables Device Access Toolkit exposes. When the glasses
 * arrive, only `MediaSource` and `Mic` below change -- the protocol and the
 * backend stay identical.
 *
 * Both modes are live: the camera view and the microphone stream to the
 * server continuously, and the server answers with captions, a tracked target
 * box, and decisions -- the phone never has to be poked to find out.
 */
'use strict';

const WS_BASE = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;
const CAPTURE_FRAME_MS = 2000;   // keyframe rate while an expert records
const WATCH_FRAME_MS = 1000;     // live view rate while a worker is guided
const JPEG_QUALITY = 0.7;
const FRAME_WIDTH = 960;         // capture keyframes
const WATCH_WIDTH = 720;         // live view: smaller, it's sent every second

const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------ screens */

function show(name) {
  document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
  $('screen-' + name).classList.add('active');
}

document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-go]');
  if (!btn) return;
  const dest = btn.dataset.go;
  Voice.unlock();
  if (dest === 'home') { Capture.teardown(); Guide.teardown(); show('home'); }
  else if (dest === 'capture') Access.then(() => Capture.open());
  else if (dest === 'library') Access.then(() => Library.open());
});

/* -------------------------------------------------------- demo sign-in */

/* The landing page is public. The app asks for the demo code once per device. */
const Access = {
  state: null,
  async status() {
    if (this.state && this.state.signed_in) return this.state;
    try { this.state = await fetch('/api/me').then((r) => r.json()); }
    catch (_) { this.state = { required: false, signed_in: true }; }   // offline: the app's own recovery takes over
    return this.state;
  },
  async then(next) {
    const s = await this.status();
    if (!s.required || s.signed_in) { next(); return; }
    this.prompt(next);
  },
  prompt(next) {
    const box = $('signin');
    const form = $('signin-form');
    const err = $('signin-error');
    err.textContent = '';
    box.hidden = false;
    $('signin-code').focus();
    $('signin-close').onclick = () => { box.hidden = true; };
    form.onsubmit = async (e) => {
      e.preventDefault();
      err.textContent = '';
      const r = await fetch('/api/login', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: $('signin-code').value }),
      }).catch(() => null);
      if (r && r.ok) {
        this.state = { required: true, signed_in: true };
        box.hidden = true;
        next();
        return;
      }
      err.textContent = !r ? 'No connection. Try again.'
        : r.status === 429 ? 'Too many attempts. Try again in a few minutes.' : "That code isn't right.";
    };
  },
};

/* ------------------------------------------------------------- language */

/* The worker's language. Only words change: the server still checks every step
 * the same way. Kept per device, and a dashboard link can set it (&lang=hi). */
const HI = {
  'Starting…': 'शुरू हो रहा है…',
  'Preparing Hindi for this job…': 'इस काम के लिए हिन्दी तैयार हो रही है…',
  'Connecting': 'जुड़ रहा है',
  'Reconnecting': 'फिर से जुड़ रहा है',
  'Offline': 'ऑफ़लाइन',
  'Watching': 'देख रहा है',
  'Live': 'लाइव',
  'Thinking': 'सोच रहा है',
  'Checking': 'जाँच रहा है',
  'Looks done': 'हो गया लगता है',
  'Done. Check it': 'हो गया, जाँचें',
  'Looking…': 'देख रहा है…',
  'Show me': 'दिखाओ',
  'Ask Ustad': 'उस्ताद से पूछें',
  'Ask': 'पूछें',
  'Repeat': 'फिर से',
  'Skip': 'छोड़ें',
  'Or type a question': 'या सवाल लिखें',
  'No connection. Keep working. Your place is saved and I will check your work when the connection is back.':
    'कनेक्शन नहीं है। काम जारी रखें। आपकी जगह सेव है, कनेक्शन आने पर मैं आपका काम जाँच लूँगा।',
  'Connection lost. Keep working, your place is saved.': 'कनेक्शन चला गया। काम जारी रखें, आपकी जगह सेव है।',
  'Reconnected. Resuming step {n}.': 'फिर से जुड़ गया। स्टेप {n} से जारी है।',
  'Back online': 'वापस ऑनलाइन',
  "Can't see the {label} in your view": '{label} आपके व्यू में नहीं दिख रहा',
  'step {n} / {of}': 'स्टेप {n} / {of}',
  'Confirmed by voice': 'आवाज़ से पुष्टि हुई',
  'Verified automatically': 'अपने आप जाँच हो गई',
  'Verified': 'सही है',
  'Caught': 'गलती पकड़ी',
  'Need a better view': 'और साफ़ दिखाएँ',
  'Read: {reading}': 'पढ़ें: {reading}',
  'Timer {t}': 'टाइमर {t}',
  'Time is up. Say done.': 'समय पूरा हुआ। बोलें: हो गया।',
  'Time is up.': 'समय पूरा हुआ।',
  'Not visible to the camera. Say done once you have checked it.': 'यह कैमरे में नहीं दिखता। जाँच लें, फिर बोलें: हो गया।',
  'Unsafe. Stop.': 'सुरक्षित नहीं है। रुकिए।',
  'Offline. I will check this when the connection is back.': 'ऑफ़लाइन। कनेक्शन आने पर मैं इसे जाँच लूँगा।',
  'No picture from the camera. Check the lens is clear.': 'कैमरे से तस्वीर नहीं आ रही। देख लें कि लेंस साफ़ है।',
  'Listening. Ask anything about this step.': 'सुन रहा हूँ। इस स्टेप के बारे में कुछ भी पूछें।',
  'That is twice now. Here is a tip.': 'यह दूसरी बार है। एक सुझाव सुनिए।',
  'Thinking…': 'सोच रहा हूँ…',
  'Get a supervisor.': 'सुपरवाइज़र को बुलाएँ।',
  'Checking your work…': 'आपका काम जाँच रहा हूँ…',
  "Expert's view · step {n}": 'एक्सपर्ट का व्यू · स्टेप {n}',
  'Tap the screen once to turn on voice commands.': 'आवाज़ से चलाने के लिए स्क्रीन पर एक बार टैप करें।',
  'Voice commands on': 'आवाज़ कमांड चालू',
  'This phone has no Hindi voice. Read the screen, or add one in Settings.': 'इस फ़ोन में हिन्दी आवाज़ नहीं है। स्क्रीन पर पढ़ें, या सेटिंग्स में जोड़ें।',
};

function tr(text, vars) {
  let out = (Lang.code === 'hi' && HI[text]) || text;
  if (vars) for (const k of Object.keys(vars)) out = out.split(`{${k}}`).join(vars[k]);
  return out;
}

const Lang = {
  code: 'en',
  init() {
    const asked = new URLSearchParams(location.search).get('lang');
    let saved = null;
    try { saved = localStorage.getItem('ustad.lang'); } catch (_) {}
    if (asked === 'hi' || asked === 'en') this.set(asked);
    else this.set(saved === 'hi' ? 'hi' : 'en');
    document.addEventListener('click', (e) => {
      const b = e.target.closest('.lang-toggle button');
      if (b) this.set(b.dataset.lang);
    });
  },
  set(code) {
    this.code = code === 'hi' ? 'hi' : 'en';
    try { localStorage.setItem('ustad.lang', this.code); } catch (_) {}
    document.querySelectorAll('.lang-toggle button').forEach((b) => {
      const on = b.dataset.lang === this.code;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    this.paint();
  },
  /* The guide screen's fixed labels. */
  paint() {
    const label = { 'guide-show': 'Show me', 'guide-ask': 'Ask Ustad', 'guide-repeat': 'Repeat', 'guide-skip': 'Skip', 'guide-done': 'Done. Check it' };
    for (const [id, text] of Object.entries(label)) { const el = $(id); if (el) el.textContent = tr(text); }
    const send = document.querySelector('#ask-form button');
    if (send) send.textContent = tr('Ask');
    const input = $('ask-input');
    if (input) input.placeholder = tr('Or type a question');
    const hints = $('guide-hints');
    if (hints) hints.innerHTML = this.code === 'hi'
      ? 'बोलें <b>हो गया</b> <b>कहाँ है</b> <b>फिर से</b> या कुछ भी पूछें'
      : 'Say <b>done</b> <b>where is it</b> <b>repeat</b> or just ask';
  },
};

/* -------------------------------------------------------------- voice out */

const Voice = {
  unlocked: false,
  quietUntil: 0,
  /* iOS will not speak unless synthesis was first touched inside a real
   * user gesture. We burn one silent utterance on the first tap. */
  unlock() {
    if (this.unlocked || !window.speechSynthesis) return;
    const u = new SpeechSynthesisUtterance('');
    u.volume = 0;
    window.speechSynthesis.speak(u);
    this.unlocked = true;
  },
  say(text) {
    if (!text || !window.speechSynthesis) return;
    // Queue rather than cancel: "Good, cap's tight." then the next step's
    // instruction should both be heard, in order.
    const u = new SpeechSynthesisUtterance(text);
    if (Lang.code === 'hi') {
      u.lang = 'hi-IN';
      const v = this.hindiVoice();
      if (v) u.voice = v;
      u.rate = 1;                           // Hindi voices are already quick
    } else {
      u.rate = 1.08;                        // a little brisk: instructions are short
    }
    u.onend = u.onerror = () => { this.quietUntil = Date.now() + 450; };
    this.quietUntil = Date.now() + 60000;   // until onend says otherwise
    window.speechSynthesis.speak(u);
  },
  hindiVoice() {
    if (!window.speechSynthesis) return null;
    return window.speechSynthesis.getVoices().find((v) => /^hi([-_]|$)/i.test(v.lang)) || null;
  },
  /* True while Ustad is talking, so the mic doesn't transcribe its own voice. */
  busy() {
    return (window.speechSynthesis && window.speechSynthesis.speaking) || Date.now() < this.quietUntil;
  },
  stop() { window.speechSynthesis && window.speechSynthesis.cancel(); this.quietUntil = 0; },
};

/* ------------------------------------------------------- media source */

const MediaSource_ = {
  stream: null,
  async start(videoEl, { audio = false } = {}) {
    // iOS gives the camera to one capture at a time; a leftover stream from a
    // previous screen would mute the new one.
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const err = new Error('This browser does not allow camera access. Open the link in Safari.');
      err.name = 'InAppBrowser';
      throw err;
    }
    this.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 } },
      audio: audio ? { echoCancellation: true, noiseSuppression: true, autoGainControl: true } : false,
    });
    videoEl.srcObject = this.stream;
    await videoEl.play().catch(() => {});
    await WakeLock.acquire();
    // iOS takes the camera away without an error: another tab grabbing it, a
    // call, Siri, the screen locking. The track says so; the screen should too.
    const track = this.stream.getVideoTracks()[0];
    if (track) {
      track.onended = () => this.onlost && this.onlost('ended');
      track.onmute = () => this.onlost && this.onlost('muted');
      track.onunmute = () => this.onlost && this.onlost('back');
    }
    return this.stream;
  },
  onlost: null,
  /* True when the picture is effectively black: a muted iOS camera still
   * hands the page frames, they are just empty. */
  isDark(videoEl) {
    const c = this._probe || (this._probe = document.createElement('canvas'));
    c.width = 16; c.height = 12;
    const g = c.getContext('2d', { willReadFrequently: true });
    g.drawImage(videoEl, 0, 0, 16, 12);
    const px = g.getImageData(0, 0, 16, 12).data;
    let sum = 0;
    for (let i = 0; i < px.length; i += 4) sum += px[i] + px[i + 1] + px[i + 2];
    return sum / (px.length / 4) / 3 < 8;
  },
  stop() {
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    this.stream = null;
    WakeLock.release();
  },
  /* Grab the current video frame as a downscaled JPEG data URL. */
  grab(videoEl, width = FRAME_WIDTH, quality = JPEG_QUALITY) {
    const vw = videoEl.videoWidth, vh = videoEl.videoHeight;
    if (!vw || !vh) return null;
    // Never send a black frame: it builds a procedure about "a black screen"
    // and costs a SAM call that can't find anything.
    this.dark = this.isDark(videoEl);
    if (this.dark) return null;
    const w = Math.min(width, vw);
    const h = Math.round((vh / vw) * w);
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.getContext('2d').drawImage(videoEl, 0, 0, w, h);
    return c.toDataURL('image/jpeg', quality);
  },
};

const WakeLock = {
  lock: null,
  async acquire() {
    try { this.lock = await navigator.wakeLock.request('screen'); } catch (_) {}
  },
  release() {
    if (this.lock) { this.lock.release().catch(() => {}); this.lock = null; }
  },
};

/* ---------------------------------------------------------------- mic in */

/* Streams the microphone as 16 kHz mono 16-bit PCM in 100 ms chunks, the
 * format Muse Voice's realtime endpoint takes. Resampling happens on the
 * phone so the upload is a third of the raw 48 kHz stream. While Ustad is
 * speaking we send silence instead of the mic: the upstream session needs
 * audio paced at real time, and it must not transcribe Ustad's own voice. */
const Mic = {
  ctx: null, src: null, node: null, sink: null, onchunk: null,
  CHUNK: 1600,

  /* iOS only lets an AudioContext run if it was created or resumed inside the
   * tap itself. By the time the countdown and socket are done that tap is long
   * gone, and the context sits suspended streaming nothing. So the tap handlers
   * call this first, synchronously, and start() reuses the unlocked context. */
  prime() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!this.ctx || this.ctx.state === 'closed') this.ctx = new AC();
    this.ctx.resume().catch(() => {});
  },

  async start(stream, onchunk, onlevel) {
    this.disconnect();
    const track = stream && stream.getAudioTracks()[0];
    if (!track) throw new Error('no microphone track');
    this.onchunk = onchunk;
    this.onlevel = onlevel || null;
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!this.ctx || this.ctx.state === 'closed') this.ctx = new AC();
    // A newer start() or a stop() may run while this one waits: the iPhone
    // log showed "this.ctx is null" from exactly that. Work on a local
    // context and give way to whoever came later.
    const ctx = this.ctx, my = (this.startGen = (this.startGen || 0) + 1);
    // resume() outside a gesture can hang on iOS rather than reject.
    await Promise.race([ctx.resume(), new Promise((r) => setTimeout(r, 800))]);
    if (my !== this.startGen || this.ctx !== ctx) throw new Error('mic restarted');
    if (ctx.state !== 'running') {
      // Keep the context: it was unlocked by a tap and a later resume (the
      // next tap) can still bring it up. Closing it threw that away.
      this.disconnect();
      throw new Error('audio suspended');     // caller falls back, and retries on the next tap
    }
    // iOS drops the context to "interrupted" for calls, Siri and its own
    // speech; pick it back up rather than going quiet for the rest of the job.
    this.ctx.onstatechange = () => {
      if (this.ctx && this.ctx.state !== 'running' && this.ctx.state !== 'closed') this.ctx.resume().catch(() => {});
    };

    // A running context is not proof of audio: on some iPhones the worklet
    // path delivers nothing, silently. Watch for the first chunk, retry on the
    // older ScriptProcessor path, and only then give up so the caller can fall
    // back to recording.
    for (const legacy of this.ctx.audioWorklet ? [false, true] : [true]) {
      this.chunks = 0;
      await this.wire(track, legacy);
      for (let w = 0; w < 25 && !this.chunks; w++) await new Promise((r) => setTimeout(r, 100));
      if (this.chunks) { this.path = legacy ? 'script' : 'worklet'; return; }
      this.unwire();
    }
    this.path = 'stalled';
    throw new Error('mic stalled');
  },

  async wire(track, legacy) {
    this.src = this.ctx.createMediaStreamSource(new MediaStream([track]));
    this.sink = this.ctx.createGain();
    this.sink.gain.value = 0;                 // keep the graph pulling, play nothing
    this.sink.connect(this.ctx.destination);

    const ratio = this.ctx.sampleRate / 16000;
    let acc = 0, cnt = 0, t = 0;
    let out = new Int16Array(this.CHUNK), n = 0;
    const push = (samples) => {
      for (let i = 0; i < samples.length; i++) {
        acc += samples[i]; cnt++; t += 1;
        if (t >= ratio) {
          t -= ratio;
          const v = Math.max(-1, Math.min(1, acc / cnt));
          out[n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
          acc = 0; cnt = 0;
          if (n === this.CHUNK) {
            const chunk = Voice.busy() ? new Int16Array(this.CHUNK) : out;
            if (this.onlevel) {             // RMS of the 100 ms chunk, 0..1, for the meter
              let s = 0;
              for (let k = 0; k < out.length; k++) s += (out[k] / 32768) ** 2;
              this.onlevel(Math.min(1, Math.sqrt(s / out.length) * 4));
            }
            this.chunks++;
            this.onchunk && this.onchunk(chunk.buffer);
            out = new Int16Array(this.CHUNK); n = 0;
          }
        }
      }
    };

    if (!legacy) {
      if (this.workletFor !== this.ctx) {     // a processor name registers once per context
        const code = `class M extends AudioWorkletProcessor{process(i){const c=i[0]&&i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor('ustad-mic',M)`;
        const url = URL.createObjectURL(new Blob([code], { type: 'application/javascript' }));
        await this.ctx.audioWorklet.addModule(url);
        this.workletFor = this.ctx;
      }
      this.node = new AudioWorkletNode(this.ctx, 'ustad-mic');
      this.node.port.onmessage = (e) => push(e.data);
    } else {                                  // older Safari, and the fallback
      this.node = this.ctx.createScriptProcessor(4096, 1, 1);
      this.node.onaudioprocess = (e) => push(e.inputBuffer.getChannelData(0));
    }
    this.src.connect(this.node);
    this.node.connect(this.sink);
  },

  unwire() {
    try { this.src && this.src.disconnect(); } catch (_) {}
    try { this.node && this.node.disconnect(); } catch (_) {}
    try { this.sink && this.sink.disconnect(); } catch (_) {}
    if (this.node && this.node.port) this.node.port.onmessage = null;
    if (this.node) this.node.onaudioprocess = null;
    this.src = this.node = this.sink = null;
  },

  disconnect() {
    this.unwire();
    this.onchunk = null;
    this.onlevel = null;
  },

  stop() {
    this.disconnect();
    if (this.ctx) this.ctx.close().catch(() => {});
    this.ctx = null;
  },
};

/* ------------------------------------------------------------ telemetry */

/* A glitch on a worker's phone is invisible from the server. So the phone
 * reports what it saw: camera and mic track state, which audio path is live,
 * how many frames and chunks actually went out, and any script error. It
 * rides the session socket, and errors go by beacon so they survive a crash. */
const Diag = {
  notes: [],
  note(what, detail) {
    this.notes.push(`${what}: ${detail}`.slice(0, 200));
    if (this.notes.length > 20) this.notes.shift();
  },
  snapshot(extra = {}) {
    const s = MediaSource_.stream;
    const tr = (t) => (t ? t.readyState + (t.muted ? '/muted' : '') + (t.enabled ? '' : '/off') : 'none');
    return {
      type: 'diag', ua: navigator.userAgent.slice(0, 140),
      video: tr(s && s.getVideoTracks()[0]), audio: tr(s && s.getAudioTracks()[0]),
      ctx: Mic.ctx ? Mic.ctx.state : 'none', rate: Mic.ctx ? Mic.ctx.sampleRate : 0,
      path: Mic.path || 'none', mic_chunks: Mic.chunks || 0, dark: !!MediaSource_.dark,
      hidden: document.hidden, notes: this.notes.slice(-8), ...extra,
    };
  },
  flush(ws, extra) {
    if (ws && ws.readyState === 1) ws.send(JSON.stringify(this.snapshot(extra)));
  },
  report(kind, message) {
    this.note(kind, message);
    const body = JSON.stringify({ kind, message: String(message).slice(0, 500), ua: navigator.userAgent.slice(0, 140) });
    try { navigator.sendBeacon('/api/clientlog', new Blob([body], { type: 'application/json' })); } catch (_) {}
  },
};
window.addEventListener('error', (e) => Diag.report('error', `${e.message} @${(e.filename || '').split('/').pop()}:${e.lineno}`));
window.addEventListener('unhandledrejection', (e) => Diag.report('rejection', (e.reason && (e.reason.stack || e.reason.message)) || e.reason));

/* ----------------------------------------------------------- liveness */

/* A phone that walks out of Wi-Fi range often leaves its socket half-open:
 * no error, no close, just silence. So every socket pings every 7 s, and one
 * that hasn't heard anything for 20 s is treated as closed, which starts the
 * reconnect right away instead of minutes later. */
const Liveness = {
  timers: new Map(),
  watch(ws, lastHeard) {
    this.stop(ws);
    this.timers.set(ws, setInterval(() => {
      if (ws.readyState !== 1) { this.stop(ws); return; }
      if (Date.now() - lastHeard() > 20000) {
        Diag.note('liveness', 'no reply in 20 s');
        this.stop(ws);
        const onclose = ws.onclose;
        ws.onclose = ws.onmessage = null;
        try { ws.close(); } catch (_) {}
        if (onclose) onclose();
        return;
      }
      try { ws.send(JSON.stringify({ type: 'ping' })); } catch (_) {}
    }, 7000));
  },
  stop(ws) { clearInterval(this.timers.get(ws)); this.timers.delete(ws); },
};

/* Drop a socket we are replacing, silently: its late close or messages must
 * not schedule another reconnect or act on stale state. */
function retire(ws) {
  if (!ws) return;
  Liveness.stop(ws);
  ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
  if (ws.readyState <= 1) { try { ws.close(); } catch (_) {} }
}

/* The browser says the network is back, or the screen came back on: don't
 * wait out the backoff. */
window.addEventListener('online', () => { Capture.kick(); Guide.kick(); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') { Capture.kick(); Guide.kick(); }
});

/* A 32x24 grayscale thumbnail of the camera view, and how different two of
 * them are (mean absolute difference, 0 to 255). Used to send a frame only
 * when the view changed: a camera resting on the bench sends almost nothing. */
function thumbGray(video) {
  const t = thumbGray.c || (thumbGray.c = Object.assign(document.createElement('canvas'), { width: 32, height: 24 }));
  const g = t.getContext('2d', { willReadFrequently: true });
  g.drawImage(video, 0, 0, 32, 24);
  const px = g.getImageData(0, 0, 32, 24).data;
  const gray = new Uint8Array(32 * 24);
  for (let i = 0; i < gray.length; i++) gray[i] = (px[i * 4] * 3 + px[i * 4 + 1] * 6 + px[i * 4 + 2]) / 10;
  return gray;
}
function grayDiff(a, b) {
  if (!a || !b) return 255;
  let s = 0;
  for (let i = 0; i < a.length; i++) s += Math.abs(a[i] - b[i]);
  return s / a.length;
}

/* ------------------------------------------------------------ capture */

/* Recording a job is the expert's one shot, so the screen tells them exactly
 * where they are: a countdown before it starts, a job clock with a guide
 * length and a hard limit, a live mic level, pause and resume, and a "Mark
 * step" button (or saying "next step") to set step boundaries themselves. */
const CAPTURE_LIMIT_S = 7200;        // 2 hours, then it stops and builds (long jobs are written in parts as they go)

const Capture = {
  ws: null, timer: null, clock: null, recorder: null, chunks: [], recording: false,
  connecting: false, processing: false, streamingMic: false, frames: 0,
  paused: false, startedAt: 0, pausedAt: 0, pausedTotal: 0, warned: false, steps: 1,

  async open() {
    show('capture');
    const wrap = $('cap-video').closest('.camera-wrap');
    try {
      await MediaSource_.start($('cap-video'), { audio: true });
      clearCameraError(wrap);
    } catch (err) {
      showCameraError(wrap, err, () => this.open());
      return;
    }
    MediaSource_.onlost = (why) => cameraLost(wrap, why, () => (this.recording ? this.restartCamera() : this.open()));
    this.resetButton();
    this.clearDrafts();
    this.setState('ready', 'ready');
    $('cap-time').textContent = '0:00';
    $('cap-limit').style.width = '0%';
    $('cap-status').textContent =
      'Record the whole job, any length up to 2 hours. Say each step out loud as you do it, or tap Mark step between steps.';
    $('cap-btn').onclick = () => (this.recording ? this.stop() : this.start());
    $('cap-pause').onclick = () => (this.paused ? this.resume() : this.pause());
    $('cap-mark').onclick = () => this.mark();
  },

  startRecorder() {
    this.recorder = null;
    if (!window.MediaRecorder) return;
    const audioOnly = new MediaStream(MediaSource_.stream.getAudioTracks());
    const mime = ['audio/mp4', 'audio/webm'].find((m) => MediaRecorder.isTypeSupported(m)) || '';
    try {
      this.recorder = new MediaRecorder(audioOnly, mime ? { mimeType: mime } : undefined);
      this.recorder.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
      this.recorder.start(1000);              // timeslices: a crash mid-take keeps what was said
      this.mime = this.recorder.mimeType || mime;
    } catch (err) {
      this.recorder = null;
      Diag.note('recorder', err.message);
    }
  },

  /* The camera dropped mid-take: get it back without losing the recording. */
  async restartCamera() {
    const wrap = $('cap-video').closest('.camera-wrap');
    Mic.prime();
    try {
      await MediaSource_.start($('cap-video'), { audio: true });
      clearCameraError(wrap);
      if (this.streamingMic && this.micArgs) await Mic.start(MediaSource_.stream, ...this.micArgs);
      $('cap-status').textContent = 'Camera back. Still recording.';
    } catch (err) {
      showCameraError(wrap, err, () => this.restartCamera());
    }
  },

  /* ---- clock ---------------------------------------------------------- */
  elapsed() {
    if (!this.startedAt) return 0;
    const now = this.paused ? this.pausedAt : Date.now();
    return Math.max(0, (now - this.startedAt - this.pausedTotal) / 1000);
  },

  tick() {
    const s = this.elapsed();
    $('cap-time').textContent = mmss(s);
    $('cap-limit').style.width = Math.min(100, (s / CAPTURE_LIMIT_S) * 100) + '%';
    $('cap-limit').classList.toggle('warn', s >= CAPTURE_LIMIT_S - 60);
    if (!this.warned && s >= CAPTURE_LIMIT_S - 60) {
      this.warned = true;
      $('cap-status').textContent = 'One minute left. Finish the step you are on.';
      Voice.say('One minute left.');
    }
    if (s >= CAPTURE_LIMIT_S && this.recording) {
      $('cap-status').textContent = 'Two hour limit reached. Building your procedure.';
      this.stop();
    }
  },

  setState(state, label) {
    $('cap-clock').className = 'rec-clock ' + state;
    $('cap-state').textContent = label;
  },

  /* ---- start ---------------------------------------------------------- */
  async start() {
    // One tap, one socket: a second tap while connecting used to open a second
    // capture socket and split the frames from the stop message.
    if (this.connecting || this.recording) return;
    Mic.prime();                                // must run inside the tap, before any await
    this.connecting = true;
    $('cap-btn').disabled = true;

    // 3-2-1 so the expert's hands are on the job before the first frame.
    const count = $('cap-count');
    for (const n of ['3', '2', '1']) {
      count.textContent = n;
      count.classList.add('on');
      await new Promise((r) => setTimeout(r, 750));
    }
    count.classList.remove('on');

    this.frames = 0;
    this.steps = 1;
    this.warned = false;
    this.paused = false;
    this.pausedTotal = 0;
    this.clearDrafts();
    $('cap-btn').textContent = 'Connecting…';
    this.chunks = [];
    this.captureId = null;
    this.offline = false;
    this.pendingFrames = [];
    this.pendingAudio = [];
    this.stopQueued = null;
    this.retries = 0;
    this.sentChunks = 0;
    this.connect(true);
  },

  /* One socket per connection, many connections per take. The first one
   * starts the take; after a drop, the next one rejoins it by id and the
   * frames and narration the phone kept while offline are synced. */
  connect(first) {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    retire(this.ws);                            // exactly one socket per take at a time
    const ws = new WebSocket(WS_BASE + '/ws/capture');
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      this.retries = 0;
      this.lastHeard = Date.now();
      Liveness.watch(ws, () => this.lastHeard);
      if (first) this.begin();
      else ws.send(JSON.stringify({ type: 'rejoin', capture_id: this.captureId, at: this.elapsed(), paused: this.paused }));
    };
    ws.onmessage = (evt) => { this.lastHeard = Date.now(); this.handle(JSON.parse(evt.data), ws); };
    ws.onerror = () => {};
    ws.onclose = () => {
      if (ws !== this.ws) return;               // an old socket closing late
      Liveness.stop(ws);
      // Never got going: the server never gave this take an id. (Checking
      // "not recording" here was the bug that froze the screen: after Stop
      // the take is building, not recording, and must be rejoined.)
      if (!this.captureId) {
        if (this.connecting) {
          this.connecting = false;
          $('cap-status').textContent = 'Could not reach the server. Check the laptop is running, then try again.';
          this.resetButton();
        }
        return;
      }
      if (!(this.recording || this.processing || this.stopQueued)) return;
      // The phone keeps recording: camera frames and narration are held
      // here and synced when the connection comes back.
      this.offline = true;
      this.setOffline(true);
      const delay = Math.min(1000 * 2 ** this.retries, 5000);
      this.retries += 1;
      this.reconnectTimer = setTimeout(() => this.connect(false), delay);
    };
  },

  /* The network is back, or the phone woke up: reconnect now, unless a
   * socket is already open or on its way. iOS freezes timers and may close
   * the socket while the screen is off, without telling the page. */
  kick() {
    const busy = this.recording || this.processing || this.stopQueued;
    if (busy && this.captureId && !(this.ws && this.ws.readyState <= 1)) this.connect(false);
  },

  setOffline(on) {
    $('cap-clock').classList.toggle('offline', on);
    if (on) {
      $('cap-status').textContent = this.processing
        ? 'Connection lost while building. Reconnecting to collect your procedure…'
        : 'No connection. Still recording on the phone. It will sync when the connection is back.';
    }
  },

  handle(msg, ws) {
    if (msg.type === 'capture') this.captureId = msg.capture_id;
    if (msg.type === 'rejoined') this.synced(msg, ws);
    if (msg.type === 'rejoin_failed') {
      this.offline = false;
      this.setOffline(false);
      $('cap-status').textContent = 'The server restarted and lost this recording. Stop, then record again.';
      this.stopQueued = null;
      if (this.processing) this.resetButton();
    }
    if (msg.type === 'frame_ack') { this.frames = msg.count; this.setBadge(); }
    if (msg.type === 'heard') this.caption(msg.text, msg.final);
    if (msg.type === 'draft') this.addDraft(msg);
    if (msg.type === 'draft_title') this.titleDraft(msg);
    if (msg.type === 'marker') this.addMarker(msg);
    if (msg.type === 'status') $('cap-status').textContent = msg.message;
    if (msg.type === 'error') {
      $('cap-status').textContent = msg.message;
      this.resetButton();
    }
    if (msg.type === 'procedure') {
      this.processing = false;
      this.setState('saved', 'saved');
      $('cap-status').textContent =
        `Saved "${msg.procedure.title}". ${msg.procedure.steps.length} steps. Opening your procedures…`;
      Voice.say(`Procedure saved. ${msg.procedure.steps.length} steps.`);
      this.send({ type: 'close' });
      setTimeout(() => Library.open(msg.procedure_id), 1600);
    }
  },

  /* Back online: send what was kept while offline, oldest first, on the
   * socket that rejoined. Anything that can't go now stays for the next one. */
  synced(msg, ws) {
    if (ws !== this.ws || ws.readyState !== 1) return;
    const frames = this.pendingFrames.splice(0);
    frames.forEach((f) => ws.send(JSON.stringify(
      f.mark ? { type: 'mark', at: f.at, quiet: true } : { type: 'frame', data: f.data, at: f.at })));
    const gaps = this.pendingAudio.splice(0);
    gaps.forEach((g) => this.sendGap(g, ws));
    this.offline = false;
    this.setOffline(false);
    Diag.note('rejoin', `${msg.state}, ${frames.length} frames, ${gaps.length} audio gaps`);
    if (msg.state === 'recording') {
      const n = frames.filter((f) => !f.mark).length;
      $('cap-status').textContent = n || gaps.length
        ? `Back online. Synced ${n} frame${n === 1 ? '' : 's'}${gaps.length ? ' and the narration' : ''} from while you were offline.`
        : 'Back online. Still recording.';
    }
    if (this.stopQueued) { const fin = this.stopQueued; this.stopQueued = null; this.deliverStop(fin); }
  },

  /* Offline narration goes up as 16 kHz PCM, a minute per message. */
  sendGap(g, ws) {
    const per = 16000 * 60 / Mic.CHUNK;            // chunks in a minute
    for (let i = 0; i < g.chunks.length; i += per) {
      const part = g.chunks.slice(i, i + per);
      const pcm = new Uint8Array(part.reduce((n, c) => n + c.byteLength, 0));
      let o = 0;
      part.forEach((c) => { pcm.set(new Uint8Array(c), o); o += c.byteLength; });
      ws.send(JSON.stringify({ type: 'audio_gap', at: g.at + (i * Mic.CHUNK) / 16000, data: bytesToBase64(pcm) }));
    }
  },

  /* The socket is open and the server has a fresh take: start recording. */
  async begin() {
    this.connecting = false;
    this.ws.send(JSON.stringify({ type: 'start', lang: Lang.code }));
    this.recording = true;
    this.startedAt = Date.now();
    clearInterval(this.clock);
    this.clock = setInterval(() => this.tick(), 250);
    this.setState('recording', 'recording');
    $('cap-btn').disabled = false;
    $('cap-btn').textContent = 'Stop & build';
    $('cap-btn').classList.add('stop');
    $('cap-pause').hidden = false;
    $('cap-mark').hidden = false;
    $('cap-pause').textContent = 'Pause';
    this.setBadge();
    $('cap-status').textContent = 'Recording. Say each step as you do it. Say "next step" to start a new one.';

    // First frame now, then sample on an interval. Even a short take has frames.
    this.sendFrame(true);
    this.timer = setInterval(() => this.sendFrame(), CAPTURE_FRAME_MS);

    // Narration streams live: captions and step cards appear as you talk.
    // A backup recording runs alongside, so a phone whose live audio path
    // stalls still has its narration transcribed when the take ends.
    this.startRecorder();
    try {
      this.micArgs = [(buf) => this.onMic(buf),
        (level) => { $('cap-level').style.width = (this.paused ? 0 : Math.round(level * 100)) + '%'; }];
      await Mic.start(MediaSource_.stream, ...this.micArgs);
      this.streamingMic = true;
    } catch (err) {
      this.streamingMic = false;
      Diag.note('mic', err.message);
      if (this.recorder) {
        $('cap-status').textContent =
          'Live captions are off on this phone. Your narration is still recorded and read when you stop.';
      }
    }
  },

  /* A 100 ms chunk of narration: live to the server, or kept while offline
   * (up to 10 minutes) with the job-clock time its gap started at. */
  onMic(buf) {
    if (this.paused || !this.recording) return;
    if (!this.offline && this.ws && this.ws.readyState === 1) { this.ws.send(buf); this.sentChunks++; return; }
    let gap = this.pendingAudio[this.pendingAudio.length - 1];
    if (!gap || gap.closed) { gap = { at: this.elapsed(), chunks: [] }; this.pendingAudio.push(gap); }
    if (this.pendingAudio.reduce((n, g) => n + g.chunks.length, 0) < 6000) gap.chunks.push(buf.slice(0));
  },

  /* ---- pause / resume / mark -------------------------------------------- */
  pause() {
    if (!this.recording || this.paused) return;
    this.paused = true;
    this.pausedAt = Date.now();
    clearInterval(this.timer);
    this.send({ type: 'pause' });
    const gap = this.pendingAudio[this.pendingAudio.length - 1];
    if (gap) gap.closed = true;                 // offline narration after the pause starts a new gap
    if (this.recorder && this.recorder.state === 'recording') this.recorder.pause();
    this.setState('paused', 'paused');
    $('cap-pause').textContent = 'Resume';
    $('cap-mark').disabled = true;
    $('cap-level').style.width = '0%';
    $('cap-status').textContent = 'Paused. Nothing is being recorded. Tap Resume to carry on.';
  },

  resume() {
    if (!this.paused) return;
    this.pausedTotal += Date.now() - this.pausedAt;
    this.paused = false;
    this.send({ type: 'resume' });
    if (this.recorder && this.recorder.state === 'paused') this.recorder.resume();
    this.sendFrame(true);
    this.timer = setInterval(() => this.sendFrame(), CAPTURE_FRAME_MS);
    this.setState('recording', 'recording');
    $('cap-pause').textContent = 'Pause';
    $('cap-mark').disabled = false;
    $('cap-status').textContent = 'Recording again.';
  },

  mark() {
    if (!this.recording || this.paused) return;
    this.sendFrame(true);              // the frame at the boundary is worth keeping
    const msg = { type: 'mark', at: this.elapsed() };
    if (this.online()) this.send(msg);
    else { this.pendingFrames.push({ mark: true, at: msg.at }); this.addMarker({ n: ++this.steps, at: mmss(msg.at) }); }
  },

  online() { return !this.offline && this.ws && this.ws.readyState === 1; },

  send(obj) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj)); },

  /* A keyframe every 2 s, but only when the view changed (or 10 s passed,
   * or at a step mark and at the end). The expert's hands still for a
   * minute should not cost 30 identical frames, on a 2 hour job above all. */
  sendFrame(force) {
    if (this.paused) return;
    const video = $('cap-video');
    if (!video.videoWidth) return;
    const gray = thumbGray(video);
    if (!force && grayDiff(gray, this.lastGray) < 4 && Date.now() - (this.lastFrameAt || 0) < 10000) return;
    const data = MediaSource_.grab(video);
    if (data) { this.lastGray = gray; this.lastFrameAt = Date.now(); }
    if (data && this.online()) {
      this.ws.send(JSON.stringify({ type: 'frame', data }));
    } else if (data) {
      // Offline: keep it with its job-clock time. Past 150 frames (5 min),
      // thin the oldest half rather than lose the end of the job.
      this.pendingFrames.push({ data, at: this.elapsed() });
      if (this.pendingFrames.length > 150) {
        this.pendingFrames = this.pendingFrames.filter((f, i) => f.mark || i % 2 === 0 || i > 75);
      }
      $('cap-badge').textContent = `${this.frames || 0} + ${this.pendingFrames.filter((f) => !f.mark).length} offline`;
    }
    this.darkStreak = MediaSource_.dark ? (this.darkStreak || 0) + 1 : 0;
    if (this.darkStreak === 2) {
      $('cap-status').textContent =
        'No picture from the camera. Check nothing covers the lens and close other tabs using the camera.';
    }
  },

  setBadge() { $('cap-badge').textContent = `${this.frames || 0} frames`; },

  caption(text, final) {
    const el = $('cap-heard');
    el.textContent = text;
    el.classList.toggle('final', !!final);
    el.classList.add('on');
    clearTimeout(this.capTimer);
    this.capTimer = setTimeout(() => el.classList.remove('on'), final ? 2500 : 4000);
  },

  clearDrafts() { const d = $('cap-drafts'); if (d) d.innerHTML = ''; },

  addDraft({ n, text, at }) {
    const d = $('cap-drafts');
    const card = document.createElement('div');
    card.className = 'draft';
    card.dataset.n = n;
    card.innerHTML = `<span class="draft-n"></span><div><b class="draft-title">Step ${n}</b><p></p></div><span class="draft-at"></span>`;
    card.querySelector('.draft-n').textContent = n;
    card.querySelector('p').textContent = text;
    card.querySelector('.draft-at').textContent = at;
    d.appendChild(card);
    d.scrollTop = d.scrollHeight;
  },

  addMarker({ n, at }) {
    const d = $('cap-drafts');
    const el = document.createElement('div');
    el.className = 'marker';
    el.textContent = `Step ${n} starts · ${at}`;
    d.appendChild(el);
    d.scrollTop = d.scrollHeight;
  },

  titleDraft({ n, title }) {
    const card = document.querySelector(`#cap-drafts .draft[data-n="${n}"] .draft-title`);
    if (card) card.textContent = title;
  },

  resetButton() {
    this.processing = false;
    clearInterval(this.clock);
    $('cap-btn').disabled = false;
    $('cap-btn').textContent = 'Start recording';
    $('cap-btn').classList.remove('stop');
    $('cap-pause').hidden = true;
    $('cap-mark').hidden = true;
    $('cap-mark').disabled = false;
    $('cap-level').style.width = '0%';
    this.setState('ready', 'ready');
  },

  async stop() {
    if (!this.recording) return;
    if (this.paused) this.resume();  // fold the paused time back before stopping
    this.recording = false;
    this.processing = true;
    clearInterval(this.timer);
    clearInterval(this.clock);
    this.sendFrame(true);   // the end state of the job is the most useful frame
    this.setState('building', 'building');
    $('cap-btn').textContent = 'Building your procedure…';
    $('cap-btn').disabled = true;
    $('cap-pause').hidden = true;
    $('cap-mark').hidden = true;

    if (this.streamingMic) {
      // Give the last words a moment to reach the server, then stop the mic.
      await new Promise((r) => setTimeout(r, 400));
      Mic.stop();
    }
    const fin = { audio: null, streamed: this.sentChunks };
    if (this.recorder && this.recorder.state !== 'inactive') {
      await new Promise((resolve) => {
        this.recorder.onstop = resolve;
        this.recorder.stop();
      });
      // The backup only travels when the live stream (online or synced
      // later) carried under 3 s of audio.
      const kept = this.pendingAudio.reduce((n, g) => n + g.chunks.length, 0);
      if (this.sentChunks + kept < 30) {
        const blob = new Blob(this.chunks, { type: this.mime });
        if (blob.size) fin.audio = { type: 'audio', data: await blobToBase64(blob), mime: this.mime };
      }
    }
    this.deliverStop(fin);
  },

  /* Send the end of the take, now or (offline) as soon as the phone rejoins. */
  deliverStop(fin) {
    if (!this.online()) {
      this.stopQueued = fin;
      $('cap-status').textContent = 'Stopped. Waiting for the connection to build your procedure. Keep this screen open.';
      return;
    }
    if (fin.audio) this.ws.send(JSON.stringify(fin.audio));
    Diag.flush(this.ws, { streamed: fin.streamed, recorded: this.chunks.length });
    this.ws.send(JSON.stringify({ type: 'stop' }));
  },

  teardown() {
    clearInterval(this.timer);
    clearInterval(this.clock);
    clearTimeout(this.reconnectTimer);
    Mic.stop();
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    this.recording = this.connecting = this.processing = this.paused = false;
    this.stopQueued = null;
    this.offline = false;
    this.startedAt = 0;
    if (this.ws) {
      const ws = this.ws; this.ws = null; ws.onclose = null; Liveness.stop(ws);
      try { ws.close(); } catch (_) {}
    }
    MediaSource_.stop();
    Voice.stop();
  },
};

function mmss(s) {
  s = Math.floor(s);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/* ------------------------------------------------------------ library */

const Library = {
  /* `highlightId` marks one procedure: just captured, or opened from a
   * dashboard "Run" link. Starting it still takes a tap, which iOS needs
   * before it allows the microphone. */
  async open(highlightId, label = 'Just captured') {
    Capture.teardown();
    show('library');
    const list = $('library-list');
    list.innerHTML = '<div class="empty">Loading…</div>';
    try {
      const procs = await fetch('/api/procedures').then((r) => r.json());
      if (!procs.length) {
        list.innerHTML =
          '<div class="empty">No procedures yet.<br>Record one and this fills itself.</div>';
        return;
      }
      list.innerHTML = '';
      procs.forEach((p) => {
        const el = document.createElement('button');
        el.className = 'card';
        el.innerHTML =
          `<h3></h3><div class="meta">${p.step_count} steps` +
          `${p.machine ? ' · ' + escapeHtml(p.machine) : ''}</div>`;
        el.querySelector('h3').textContent = p.title;
        if (p.id === highlightId) {
          el.classList.add('fresh');
          const tag = document.createElement('span');
          tag.className = 'fresh-tag';
          tag.textContent = label;
          el.prepend(tag);
        }
        el.onclick = () => Guide.open(p.id);
        list.appendChild(el);
      });
      const fresh = list.querySelector('.card.fresh');
      if (fresh) fresh.scrollIntoView({ block: 'center' });
    } catch (err) {
      list.innerHTML = '<div class="empty">Could not load procedures.</div>';
    }
  },
};

/* -------------------------------------------------------------- guide */

const Guide = {
  ws: null, sessionId: null, procedureId: null, busy: false,
  closing: false, retries: 0, reconnectTimer: null, watchTimer: null, step: 0,
  offline: false, pendingCheck: false,

  async open(procedureId) {
    Mic.prime();                                // inside the tap that opened the job
    this.sent = { frames: 0, stills: 0, dark: 0 };
    show('guide');
    this.procedureId = procedureId;
    this.sessionId = null;
    this.closing = false;
    this.retries = 0;
    this.step = 0;
    Lang.paint();
    $('guide-instruction').textContent = tr('Starting…');
    $('guide-step-title').textContent = '';
    this.setLive('connecting', 'Connecting');
    if (Lang.code === 'hi') {
      // The voice list can load late on iPhone: look after a moment, and say so
      // rather than stay silent, so the worker knows to read the screen.
      setTimeout(() => {
        if (Voice.hindiVoice()) return;
        Diag.note('tts', 'no Hindi voice on this device');
        this.toast(tr('This phone has no Hindi voice. Read the screen, or add one in Settings.'));
      }, 1800);
    }
    const wrap = $('guide-video').closest('.camera-wrap');
    try {
      await MediaSource_.start($('guide-video'), { audio: true });
      clearCameraError(wrap);
    } catch (err) {
      showCameraError(wrap, err, () => this.open(this.procedureId));
      return;
    }
    MediaSource_.onlost = (why) => cameraLost(wrap, why, () => this.restartCamera());

    $('guide-done').onclick = () => this.check();
    $('guide-repeat').onclick = () => this.send({ type: 'repeat' });
    $('guide-skip').onclick = () => this.send({ type: 'skip' });
    $('guide-show').onclick = () => { Voice.stop(); this.send({ type: 'show' }); };
    $('guide-ask').onclick = () => Ask.open();
    LiveBox.start();
    this.connect();
  },

  kick() {
    if (this.offline && !this.closing && this.reconnectTimer && !(this.ws && this.ws.readyState <= 1)) this.connect();
  },

  /* Open the socket and (re)join the session. On a first connect we `start`;
   * on a reconnect we `resume`, and the server decides which step to resume at
   * from what already passed -- a dropped connection never restarts the job. */
  connect() {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    retire(this.ws);
    const ws = new WebSocket(WS_BASE + '/ws/guide');
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    this.ws.onopen = async () => {
      this.retries = 0;
      this.lastHeard = Date.now();
      Liveness.watch(ws, () => this.lastHeard);
      this.setBanner('');
      if (this.sessionId) this.send({ type: 'resume', session_id: this.sessionId, lang: Lang.code });
      else this.send({ type: 'start', procedure_id: this.procedureId, lang: Lang.code });
      // A new socket is a new server session with no view yet: the change
      // detector must send a full frame first, not a "still" heartbeat, and
      // a check before that frame lands carries its own.
      this.lastGray = null;
      this.haveView = false;

      // The live view: one small frame a second for the watcher.
      clearInterval(this.watchTimer);
      this.watchTimer = setInterval(() => this.sendWatch(), WATCH_FRAME_MS);
      this.sendWatch();

      // Hands-free: the mic streams to the server, which listens for commands.
      try {
        await Mic.start(MediaSource_.stream, this.micSend);
        $('guide-hints').classList.add('on');
      } catch (err) {
        Diag.note('mic', err.message);
        if (err.message !== 'mic restarted') {
          $('guide-hints').classList.remove('on');
          this.toast(tr('Tap the screen once to turn on voice commands.'));
          // iOS lets audio start inside a tap: try again on the next one.
          const retry = async () => {
            Mic.prime();
            if (!this.ws || this.ws.readyState !== 1) return;
            try { await Mic.start(MediaSource_.stream, this.micSend); $('guide-hints').classList.add('on'); this.toast(tr('Voice commands on')); }
            catch (e) { Diag.note('mic retry', e.message); }
          };
          document.addEventListener('touchend', retry, { once: true });
          document.addEventListener('click', retry, { once: true });
        }
      }
      const report = () => Diag.flush(this.ws, { frames: this.sent.frames, stills: this.sent.stills, dark: this.sent.dark });
      report();
      clearInterval(this.diagTimer);
      this.diagTimer = setInterval(report, 15000);
    };
    this.ws.onmessage = (evt) => { this.lastHeard = Date.now(); this.handle(JSON.parse(evt.data)); };
    this.ws.onerror = () => {};  // onclose drives reconnect
    this.ws.onclose = () => {
      if (ws !== this.ws) return;
      Liveness.stop(ws);
      clearInterval(this.watchTimer);
      clearInterval(this.diagTimer);
      // Keep the unlocked audio context: a reconnect has no tap to unlock a new one.
      Mic.disconnect();
      if (!this.closing) this.scheduleReconnect();
    };
  },

  micSend(buf) {
    if (Guide.ws && Guide.ws.readyState === 1) Guide.ws.send(buf);
  },

  /* The camera dropped mid-job: get it back and carry on from the same step. */
  async restartCamera() {
    const wrap = $('guide-video').closest('.camera-wrap');
    Mic.prime();
    try {
      await MediaSource_.start($('guide-video'), { audio: true });
      clearCameraError(wrap);
      if (this.ws && this.ws.readyState === 1) await Mic.start(MediaSource_.stream, this.micSend).catch(() => {});
    } catch (err) {
      showCameraError(wrap, err, () => this.restartCamera());
    }
  },

  /* Send the live view only when it changed. Grounding a frame costs a SAM
   * call, and a camera resting on the work shows the same thing second after
   * second -- so compare a 32x24 grayscale thumbnail with the last one sent
   * and, if nothing moved, send a tiny "still" heartbeat instead. */
  sendWatch() {
    if (!this.ws || this.ws.readyState !== 1) return;
    const video = $('guide-video');
    if (!video.videoWidth) return;
    const gray = thumbGray(video);
    const diff = grayDiff(gray, this.lastGray);
    const stale = Date.now() - (this.lastSentAt || 0) > 8000;   // refresh now and then anyway
    if (diff < 7 && !stale) { this.send({ type: 'still' }); this.sent.stills++; return; }
    const data = MediaSource_.grab(video, WATCH_WIDTH, 0.62);
    if (!data) {
      // A black view used to mean silence on both ends. Now the server hears
      // about it and tells the worker, instead of quietly checking nothing.
      if (MediaSource_.dark) {
        this.sent.dark++;
        if (Date.now() - (this.darkSentAt || 0) > 4000) { this.send({ type: 'dark' }); this.darkSentAt = Date.now(); }
      }
      return;
    }
    this.sent.frames++;
    this.send({ type: 'watch', data });
    this.lastGray = gray;
    this.lastSentAt = Date.now();
    this.haveView = true;
  },

  /* Keep trying for as long as the job is on screen: the server holds the
   * worker's place, so whenever the network comes back the job carries on. */
  scheduleReconnect() {
    const delay = Math.min(1000 * 2 ** this.retries, 5000);
    this.retries += 1;
    if (!this.offline) {
      this.offline = true;
      $('guide-instruction').textContent = tr('No connection. Keep working. Your place is saved and I will check your work when the connection is back.');
      Voice.say(tr('Connection lost. Keep working, your place is saved.'));
    }
    this.setBanner(tr('Offline'), true);
    this.setLive('connecting', 'Reconnecting');
    LiveBox.tracks = {};                 // stale boxes would point at the wrong place
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  },

  setBanner(text, warn) {
    const el = $('guide-progress');
    if (!el) return;
    if (!text) { el.classList.remove('warn'); return; }
    el.textContent = text;
    el.classList.toggle('warn', !!warn);
  },

  /* The live-state chip: what Ustad is doing right now. */
  setLive(state, label) {
    const el = $('guide-live');
    el.className = 'live-chip ' + state;
    el.querySelector('b').textContent = tr(label);
  },

  send(obj) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj)); },

  resetDone() {
    this.busy = false;
    $('guide-done').disabled = false;
    $('guide-done').textContent = tr('Done. Check it');
  },

  handle(msg) {
    switch (msg.type) {
      case 'session':
        this.sessionId = msg.session_id;
        if (msg.resumed_at) {
          $('guide-instruction').textContent = tr('Reconnected. Resuming step {n}.', { n: msg.resumed_at });
          this.toast(tr('Back online'));
        }
        this.offline = false;
        this.setLive('watching', msg.auto ? 'Watching' : 'Live');
        if (this.pendingCheck) { this.pendingCheck = false; setTimeout(() => this.check(), 600); }
        break;

      case 'preparing':
        $('guide-instruction').textContent = tr('Preparing Hindi for this job…');
        break;

      case 'thinking':
        Ask.thinking(msg);
        this.setLive('checking', 'Thinking');
        break;

      case 'answer':
        Ask.answered(msg);
        if (!this.busy) this.setLive('watching', 'Watching');
        break;

      case 'point':
        LiveBox.point(msg.label, msg.detections || []);
        if (!(msg.detections || []).length) this.toast(tr("Can't see the {label} in your view", { label: msg.label }));
        break;

      case 'reference':
        RefCard.show(msg);
        break;

      case 'speak':
        if (this.offline) break;             // a late line from the old socket
        if (msg.aside) {                     // an answer or a pointer: the step stays on screen
          Voice.say(msg.text);
          if (!$('ask-panel').className.includes('answered')) Ask.answered({ say: msg.text, tool: 'answer' });
          break;
        }
        if (msg.step) {
          $('guide-progress').textContent = tr('step {n} / {of}', { n: msg.step, of: msg.of });
          if (msg.step !== this.step && msg.target !== undefined) LiveBox.reset(msg.target);
          this.step = msg.step;
        }
        if (msg.kind !== undefined) this.setSpec(msg);
        $('guide-progress').classList.remove('warn');
        if (msg.title !== undefined) $('guide-step-title').textContent = msg.title || '';
        $('guide-instruction').textContent = msg.text;
        Voice.say(msg.text);
        break;

      case 'track':
        LiveBox.update(msg.detections || [], msg.target, msg.objects);
        break;

      case 'heard':
        this.caption(msg.text, msg.final);
        break;

      case 'checking':
        this.busy = true;
        $('guide-done').disabled = true;
        $('guide-done').textContent = tr('Looking…');
        this.setLive('checking', 'Checking');
        Voice.stop();
        break;

      case 'auto':
        if (!this.busy) this.setLive(msg.state === 'looks_done' ? 'likely' : 'watching',
          msg.state === 'looks_done' ? 'Looks done' : 'Watching');
        break;

      case 'hazard':
        this.hazard(msg.message);
        break;

      case 'notice':                       // e.g. the view went dark
        this.toast(msg.text);
        Voice.say(msg.text);
        break;

      case 'proximity':
        LiveBox.danger(msg.hazard);
        this.hazard(msg.text || `Hand near the ${msg.hazard}. Keep clear.`);
        break;

      case 'pulse':
        LiveBox.pulse();
        break;

      case 'budget':
        this.setLive('connecting', 'Budget saver');
        this.toast(`Spend $${msg.spent} of $${msg.budget}: live watching paused, say done to check`);
        break;

      case 'verdict':
        this.resetDone();
        this.setLive('watching', 'Watching');
        if (msg.verdict === 'wait') break;          // timed step: the spoken countdown says it
        flash(msg.verdict === 'confirmed' ? 'pass' : msg.verdict);
        LiveBox.flash(msg.verdict === 'confirmed' ? 'pass' : msg.verdict);
        if (msg.verdict === 'confirmed') this.toast(tr('Confirmed by voice'));
        else if (msg.source === 'auto' && msg.advance) this.toast(tr('Verified automatically'));
        else if (msg.latency_ms) this.toast(`${tr(msg.verdict === 'pass' ? 'Verified' : msg.verdict === 'fail' ? 'Caught' : 'Need a better view')} · ${(msg.latency_ms / 1000).toFixed(1)} s`);
        break;

      case 'complete':
        Report.open(msg.session_id);
        break;

      case 'error':
        this.resetDone();
        this.setLive('watching', 'Watching');
        $('guide-instruction').textContent = msg.message;
        break;
    }
  },

  /* What kind of check this step is: a gauge reading to hit, a timer to
   * wait out, or a check only the operator can confirm. */
  setSpec({ kind, reading, duration }) {
    const el = $('guide-spec');
    clearInterval(this.specTimer);
    el.className = 'step-spec ' + (kind || 'visual');
    if (kind === 'reading' && reading) {
      el.textContent = tr('Read: {reading}', { reading });
      el.hidden = false;
    } else if (kind === 'timed' && duration) {
      const end = Date.now() + duration * 1000;
      const draw = () => {
        const left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
        el.textContent = left > 0 ? tr('Timer {t}', { t: mmss(left) }) : tr('Time is up. Say done.');
        el.classList.toggle('up', left === 0);
        if (left === 0) { clearInterval(this.specTimer); Voice.say(tr('Time is up.')); }
      };
      draw();
      this.specTimer = setInterval(draw, 500);
      el.hidden = false;
    } else if (kind === 'confirm') {
      el.textContent = tr('Not visible to the camera. Say done once you have checked it.');
      el.hidden = false;
    } else {
      el.hidden = true;
    }
  },

  caption(text, final) {
    const el = $('guide-heard');
    el.textContent = final ? `“${text}”` : text;
    el.classList.toggle('final', !!final);
    el.classList.add('on');
    clearTimeout(this.capTimer);
    this.capTimer = setTimeout(() => el.classList.remove('on'), final ? 2200 : 3500);
  },

  toast(text) {
    const el = $('guide-toast');
    el.textContent = text;
    el.classList.add('on');
    clearTimeout(this.toastTimer);
    this.toastTimer = setTimeout(() => el.classList.remove('on'), 2200);
  },

  hazard(text) {
    const el = $('guide-hazard');
    el.textContent = text || tr('Unsafe. Stop.');
    el.classList.add('on');
    clearTimeout(this.hazTimer);
    this.hazTimer = setTimeout(() => el.classList.remove('on'), 6000);
  },

  check() {
    if (this.busy) return;
    if (!this.ws || this.ws.readyState !== 1) {
      // Offline: remember the tap and check as soon as the job reconnects.
      this.pendingCheck = true;
      this.toast(tr('Offline. I will check this when the connection is back.'));
      return;
    }
    // The server judges its own live view (already grounded, so it's fast).
    // Before the first view arrives, send one with the request.
    const msg = { type: 'check', said: 'done' };
    if (!this.haveView) {
      const data = MediaSource_.grab($('guide-video'));
      if (!data) {
        if (MediaSource_.dark) this.toast(tr('No picture from the camera. Check the lens is clear.'));
        return;
      }
      msg.data = data;
    }
    this.send(msg);
  },

  teardown() {
    this.closing = true;                 // stop the reconnect loop from firing
    this.offline = false;
    this.pendingCheck = false;
    clearInterval(this.diagTimer);
    clearTimeout(this.reconnectTimer);
    clearInterval(this.watchTimer);
    Mic.stop();
    Ask.close();
    RefCard.hide();
    if (this.ws) {
      Liveness.stop(this.ws);
      this.send({ type: 'abort' });
      try { this.ws.close(); } catch (_) {}
      this.ws = null;
    }
    this.haveView = false;
    this.lastGray = null;
    this.lastSentAt = 0;
    MediaSource_.stop();
    Voice.stop();
    LiveBox.stop();
    clearInterval(this.specTimer);
    $("guide-spec").hidden = true;
  },
};

/* ------------------------------------------------------------ ask ustad */

/* The worker can ask anything, any time, by voice ("which one?", "it won't
 * turn") and the server answers out loud. The Ask button makes sure the next
 * thing said is taken as a question, and has a box for typing one. */
const Ask = {
  open() {
    Voice.stop();                         // the mic is muted while Ustad talks
    Guide.send({ type: 'listen' });
    const p = $('ask-panel');
    p.hidden = false;
    p.className = 'ask-panel listening';
    $('ask-q').textContent = tr('Listening. Ask anything about this step.');
    $('ask-a').textContent = '';
    this.autoHide(14000);
  },
  thinking({ question, coach }) {
    const p = $('ask-panel');
    p.hidden = false;
    p.className = 'ask-panel thinking';
    $('ask-q').textContent = coach ? tr('That is twice now. Here is a tip.') : `“${question}”`;
    $('ask-a').textContent = tr('Thinking…');
    this.autoHide(20000);
  },
  answered({ say, tool, reason, failed }) {
    const p = $('ask-panel');
    if (failed) { this.close(); return; }
    p.hidden = false;
    p.className = 'ask-panel ' + (tool === 'escalate' ? 'escalate' : 'answered');
    $('ask-a').textContent = tool === 'escalate' ? `${tr('Get a supervisor.')} ${reason || ''}`.trim() : (say || '');
    if (tool === 'check_now') $('ask-a').textContent = tr('Checking your work…');
    this.autoHide(tool === 'escalate' ? 15000 : 9000);
  },
  autoHide(ms) {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.close(), ms);
  },
  close() {
    clearTimeout(this.timer);
    const p = $('ask-panel');
    if (p) p.hidden = true;
  },
};

document.addEventListener('submit', (e) => {
  if (e.target.id !== 'ask-form') return;
  e.preventDefault();
  const input = $('ask-input');
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  input.blur();
  Guide.send({ type: 'ask', text });
});

/* "Show me": the expert's own photo of this step, the part boxed on it, so
 * the worker can match what they see to what the expert saw. */
const RefCard = {
  show({ image, box, step, said, target }) {
    const card = $('ref-card');
    $('ref-img').src = image;
    const b = $('ref-box');
    if (box && box.img_w && box.img_h) {
      Object.assign(b.style, {
        left: (box.x1 / box.img_w) * 100 + '%', top: (box.y1 / box.img_h) * 100 + '%',
        width: ((box.x2 - box.x1) / box.img_w) * 100 + '%', height: ((box.y2 - box.y1) / box.img_h) * 100 + '%',
      });
      b.hidden = false;
      b.dataset.label = target || '';
    } else {
      b.hidden = true;
    }
    $('ref-title').textContent = tr("Expert's view · step {n}", { n: step });
    $('ref-said').textContent = said ? `“${said.length > 160 ? said.slice(0, 157) + '…' : said}”` : '';
    card.hidden = false;
    card.classList.remove('big');
    clearTimeout(this.timer);
    this.timer = setTimeout(() => { if (!card.classList.contains('big')) this.hide(); }, 15000);
  },
  hide() { clearTimeout(this.timer); const c = $('ref-card'); if (c) c.hidden = true; },
};

document.addEventListener('click', (e) => {
  const card = e.target.closest('#ref-card');
  if (!card) return;
  if (e.target.closest('.ref-close')) { RefCard.hide(); return; }
  card.classList.toggle('big');          // tap to enlarge, tap again to shrink
});

/* ------------------------------------------------------------ live box */

/* Draws what matters in the operator's view like AR markers, each eased
 * toward its latest SAM grounding so it reads as tracking, not flicker:
 *   target  blue corner brackets, the part to work on
 *   hazard  red dashed zone, "Keep clear", flashing when a hand gets close
 *   tool    green brackets, the tool this step needs */
const STYLE = {
  target: { color: '#3b82ff', prefix: '' },
  hazard: { color: '#ff4d52', prefix: 'Keep clear: ' },
  tool: { color: '#12d98a', prefix: 'Tool: ' },
  point: { color: '#ffc53d', prefix: 'Here: ' },     // Ask Ustad pointing at something
};

const LiveBox = {
  tracks: {}, target: '', raf: 0,
  flashColor: null, flashUntil: 0, pulseAt: 0, dangerUntil: 0, dangerLabel: '',

  start() { if (!this.raf) this.loop(); },
  stop() {
    cancelAnimationFrame(this.raf); this.raf = 0;
    this.tracks = {};
    const c = $('guide-overlay');
    if (c && c.getContext) c.getContext('2d').clearRect(0, 0, c.width, c.height);
  },
  reset(target) { this.tracks = {}; this.target = target || ''; },
  update(dets, target, objects) {
    const now = performance.now();
    if (target) this.target = target;
    const feed = (key, role, label, d) => {
      const t = this.tracks[key] || (this.tracks[key] = { role, label, cur: null, alpha: 0 });
      t.dets = d || []; t.at = now; t.label = label;
    };
    feed('target', 'target', this.target, dets);
    (objects || []).forEach((o) => feed(o.role + ':' + o.label, o.role, o.label, o.detections));
  },
  flash(verdict) {
    this.flashColor = verdict === 'pass' ? '#12d98a' : verdict === 'fail' ? '#ff5a5f' : '#ffb020';
    this.flashUntil = performance.now() + 900;
  },
  pulse() { this.pulseAt = performance.now(); },
  /* Ask Ustad points at a part: a gold marker that holds for 7 s. */
  point(label, dets) {
    this.tracks.point = { role: 'point', label, dets, at: performance.now(), ttl: 7000, cur: null, alpha: 0 };
    this.pulseAt = performance.now();
  },
  danger(label) { this.dangerLabel = label; this.dangerUntil = performance.now() + 2500; },

  loop() {
    this.raf = requestAnimationFrame(() => this.loop());
    const video = $('guide-video');
    const c = $('guide-overlay');
    if (!c || !video || !video.videoWidth) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const W = video.clientWidth, H = video.clientHeight;
    if (c.width !== Math.round(W * dpr) || c.height !== Math.round(H * dpr)) {
      c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
    }
    const ctx = c.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);

    // Video is object-fit:cover, so replicate the crop maths.
    const vr = video.videoWidth / video.videoHeight, cr = W / H;
    let dw, dh, ox, oy;
    if (vr > cr) { dh = H; dw = dh * vr; ox = (W - dw) / 2; oy = 0; }
    else { dw = W; dh = dw / vr; ox = 0; oy = (H - dh) / 2; }
    const now = performance.now();

    // hazards first, then tools, then the target, and a pointed-at part on top
    const order = { hazard: 0, tool: 1, target: 2, point: 3 };
    Object.values(this.tracks).sort((a, b) => order[a.role] - order[b.role]).forEach((t) => {
      const best = (t.dets || []).length
        ? t.dets.reduce((a, b) => ((b.confidence || 0) > (a.confidence || 0) ? b : a)) : null;
      const want = best && now - t.at < (t.ttl || 3000) ? 1 : 0;
      t.alpha += (want - t.alpha) * 0.12;
      if (best) {
        const r = {
          x: ox + (best.x1 / best.img_w) * dw, y: oy + (best.y1 / best.img_h) * dh,
          w: ((best.x2 - best.x1) / best.img_w) * dw, h: ((best.y2 - best.y1) / best.img_h) * dh,
        };
        if (!t.cur) t.cur = r;
        else for (const k of ['x', 'y', 'w', 'h']) t.cur[k] += (r[k] - t.cur[k]) * 0.22;
      }
      if (!t.cur || t.alpha < 0.02) return;
      this.draw(ctx, t, now, W);
    });
    ctx.globalAlpha = 1;
  },

  draw(ctx, t, now, W) {
    const { x, y, w, h } = t.cur;
    const style = STYLE[t.role] || STYLE.target;
    let color = style.color;
    if (t.role === 'target' && now < this.flashUntil) color = this.flashColor;
    const alarm = t.role === 'hazard' && now < this.dangerUntil && t.label === this.dangerLabel;
    ctx.globalAlpha = t.alpha * (alarm ? 0.55 + 0.45 * Math.abs(Math.sin(now / 120)) : 1);

    ctx.fillStyle = color + (t.role === 'hazard' ? (alarm ? '55' : '22') : '1f');
    roundRect(ctx, x, y, w, h, 10); ctx.fill();
    ctx.strokeStyle = color; ctx.lineCap = 'round';
    if (t.role === 'hazard') {
      ctx.lineWidth = alarm ? 4 : 2.5;
      ctx.setLineDash([9, 7]);
      roundRect(ctx, x, y, w, h, 10); ctx.stroke();
      ctx.setLineDash([]);
    } else {
      const L = Math.max(12, Math.min(w, h) * (t.role === 'tool' ? 0.2 : 0.26));
      ctx.lineWidth = t.role === 'tool' ? 3 : 4;
      ctx.shadowColor = color; ctx.shadowBlur = t.role === 'tool' ? 6 : 12;
      ctx.beginPath();
      ctx.moveTo(x, y + L); ctx.lineTo(x, y); ctx.lineTo(x + L, y);
      ctx.moveTo(x + w - L, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w, y + L);
      ctx.moveTo(x + w, y + h - L); ctx.lineTo(x + w, y + h); ctx.lineTo(x + w - L, y + h);
      ctx.moveTo(x + L, y + h); ctx.lineTo(x, y + h); ctx.lineTo(x, y + h - L);
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    if (t.role === 'target' || t.role === 'point') {
      const since = now - this.pulseAt;
      if (since < 1400) {
        const k = since / 1400;
        ctx.globalAlpha = t.alpha * (1 - k);
        ctx.lineWidth = 3;
        roundRect(ctx, x - 28 * k, y - 28 * k, w + 56 * k, h + 56 * k, 14); ctx.stroke();
        ctx.globalAlpha = t.alpha;
      }
    }

    const label = style.prefix + (t.label || '');
    if (label.trim()) {
      ctx.font = `600 ${t.role === 'target' ? 14 : 12.5}px Figtree, -apple-system, sans-serif`;
      const tw = ctx.measureText(label).width;
      const lh = t.role === 'target' ? 26 : 22;
      const lx = Math.max(6, Math.min(x, W - tw - 30));
      const ly = y > lh + 8 ? y - lh - 8 : y + h + 8;
      ctx.fillStyle = color;
      roundRect(ctx, lx, ly, tw + 22, lh, lh / 2); ctx.fill();
      ctx.fillStyle = t.role === 'tool' ? '#052414' : t.role === 'point' ? '#2a1d00' : '#fff';
      ctx.fillText(label, lx + 11, ly + lh * 0.68);
    }
  },
};

function roundRect(ctx, x, y, w, h, r) {
  r = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* ------------------------------------------------------------- report */

const Report = {
  async open(sessionId) {
    Guide.teardown();
    show('report');
    const body = $('report-body');
    body.innerHTML = '<div class="empty">Loading…</div>';
    const data = await fetch('/api/sessions/' + sessionId).then((r) => r.json());
    const events = data.events || [];
    const passes = events.filter((e) => e.verdict === 'pass').length;
    const fails = events.filter((e) => e.verdict === 'fail').length;
    const autos = events.filter((e) => (e.reason || '').startsWith('[auto]')).length;
    const timed = events.filter((e) => e.latency_ms);
    const avg = timed.length
      ? (timed.reduce((a, e) => a + e.latency_ms, 0) / timed.length / 1000).toFixed(1)
      : '0';

    const asked = events.filter((e) => e.verdict === 'asked' || e.verdict === 'help').length;
    body.innerHTML =
      `<div class="kpis">
         <div class="kpi"><b>${passes}</b><span>verified${autos ? ` (${autos} auto)` : ''}</span></div>
         <div class="kpi"><b>${fails}</b><span>caught</span></div>
         <div class="kpi"><b>${asked ? asked : avg + ' s'}</b><span>${asked ? 'questions answered' : 'per check'}</span></div>
       </div>
       <a class="pack-link" href="/evidence?s=${encodeURIComponent(sessionId)}" target="_blank" rel="noopener">Open the evidence pack</a>
       <button class="pack-link again" id="report-again">Run this procedure again</button>`;
    const pid = data.session && data.session.procedure_id;
    if (pid) $('report-again').onclick = () => Guide.open(pid);
    else $('report-again').remove();

    const LABEL = { asked: 'question', help: 'supervisor needed', notice: 'notice' };
    events.forEach((e) => {
      const card = document.createElement('div');
      card.className = 'card event ' + e.verdict;
      const title = document.createElement('h3');
      title.textContent = `Step ${e.step_index + 1}: ${LABEL[e.verdict] || e.verdict}`;
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = e.reason || '';
      card.append(title, meta);
      if (e.evidence_path) {
        const img = document.createElement('img');
        img.src = '/' + e.evidence_path;
        img.loading = 'lazy';
        card.appendChild(img);
      }
      body.appendChild(card);
    });
  },
};

/* -------------------------------------------------------------- utils */

function flash(verdict) {
  const el = $('verdict-flash');
  el.className = 'verdict-flash ' + verdict;
  setTimeout(() => { el.className = 'verdict-flash'; }, 700);
}

function bytesToBase64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onloadend = () => resolve(String(r.result).split(',')[1]);
    r.onerror = reject;
    r.readAsDataURL(blob);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* The live camera went away mid-screen. A mute is often brief (a notification,
 * Control Center), so give it a moment before covering the view; an ended
 * track never comes back on its own, so offer the retry straight away. */
function cameraLost(wrapEl, why, retry) {
  clearTimeout(cameraLost.timer);
  if (why === 'back') { clearCameraError(wrapEl); return; }
  const lost = { name: 'CameraLost' };
  if (why === 'ended') { showCameraError(wrapEl, lost, retry); return; }
  cameraLost.timer = setTimeout(() => showCameraError(wrapEl, lost, retry), 1500);
}

/* Clean, actionable overlay when the camera/mic is unavailable or denied.
 * iOS silently keeps a denial until the site's data is cleared, so the message
 * has to tell the worker exactly where to go rather than just failing. */
function showCameraError(wrapEl, err, retry) {
  if (!wrapEl) return;
  clearCameraError(wrapEl);
  const denied = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
  const missing = err && (err.name === 'NotFoundError' || err.name === 'OverconstrainedError');
  const busy = err && (err.name === 'NotReadableError' || err.name === 'AbortError' || err.name === 'CameraLost');
  const inApp = err && err.name === 'InAppBrowser';
  const title = denied ? 'Camera and microphone are blocked'
    : missing ? 'No camera found'
    : busy ? 'The camera was taken away'
    : inApp ? 'Open this in Safari'
    : 'Could not start the camera';
  const help = denied
    ? 'Ustad needs the camera to check your work. Tap aA in the address bar, Website Settings, set Camera and Microphone to Allow, then retry.'
    : missing ? 'This device did not expose a camera to the browser.'
    : busy ? 'Another tab or app is using the camera, or a call interrupted it. Close other tabs with Ustad open, then retry.'
    : inApp ? 'Apps like WhatsApp and Gmail open links in a browser that cannot use the camera. Tap the menu and choose Open in Safari.'
    : (err && err.message) || 'Unknown error.';
  const el = document.createElement('div');
  el.className = 'cam-error';
  el.innerHTML =
    `<div class="cam-error-card">
       <h3></h3><p></p>
       <button class="cockpit-primary cam-retry">Allow and retry</button>
     </div>`;
  el.querySelector('h3').textContent = title;
  el.querySelector('p').textContent = help;
  el.querySelector('.cam-retry').onclick = () => { clearCameraError(wrapEl); retry && retry(); };
  wrapEl.appendChild(el);
}

function clearCameraError(wrapEl) {
  const old = wrapEl && wrapEl.querySelector('.cam-error');
  if (old) old.remove();
}

/* -------------------------------------------------------------- boot */

/* A dashboard "Run" link: /?run=<procedure id> opens the procedure list
 * with that one ready, one tap from starting. */
Lang.init();

Access.status();   // known before the first tap, so the app opens at once

(async function accessLink() {
  // In the fragment, which browsers never send to a server, so it can't reach any log.
  const m = /^#access=([^&]+)$/.exec(location.hash);
  if (!m) return;
  const code = decodeURIComponent(m[1]);
  history.replaceState(null, '', location.pathname + location.search);
  const r = await fetch('/api/login', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }),
  }).catch(() => null);
  if (r && r.ok) Access.state = { required: true, signed_in: true };
})();

(function signinLink() {
  const next = new URLSearchParams(location.search).get('signin');
  if (next === null) return;
  // Only a page on this site: never send someone elsewhere after signing in.
  const safe = /^\/(dashboard|evidence)(\?|$)/.test(next) ? next : '/';
  Access.then(() => { location.replace(safe); });
})();

(function runLink() {
  const pid = new URLSearchParams(location.search).get('run');
  if (pid) Access.then(() => Library.open(pid, 'Ready to run. Tap to start'));
})();

fetch('/api/health')
  .then((r) => r.json())
  .then((h) => {
    $('health-dot').className = 'dot ' + (h.key_configured ? 'ok' : 'bad');
    if (!h.key_configured) {
      $('health-text').textContent = 'MODEL_API_KEY not set on the server';
      return;
    }
    const where = h.reasoning_backend === 'open' ? 'self-hosted' : 'Meta Model API';
    $('health-text').textContent = 'Live · Built for Meta glasses';
    $('home-status').title = `${h.models.reasoning} · ${h.models.sam} · ${where}`;
  })
  .catch(() => {
    $('health-dot').className = 'dot bad';
    $('health-text').textContent = 'backend unreachable';
  });

/* -------------------------------------------------- landing jump nav */
/* The landing is one flowing page; the nav links scroll to a section and
 * highlight whichever section is currently in view. */
(function initJump() {
  const links = Array.from(document.querySelectorAll('a[data-jump]'));
  const root = document.getElementById('screen-home');
  if (!links.length || !root) return;
  const byId = new Map(links.map((a) => [a.dataset.jump, a]));
  const setActive = (id) => links.forEach((a) => a.classList.toggle('active', a.dataset.jump === id));
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting && byId.has(e.target.id)) setActive(e.target.id);
    });
  }, { root, rootMargin: '-45% 0px -50% 0px' });
  links.forEach((a) => { const s = document.getElementById(a.dataset.jump); if (s) io.observe(s); });
})();

/* Hero hotspots: each label is pinned to a 3D point on the glasses and shown
 * only while its part faces the viewer. Facing is the dot product of the
 * part's surface normal with the direction to the camera, recomputed on every
 * camera move (auto-rotate included). */
(function initHotspots() {
  const mv = document.getElementById('hero-glasses');
  if (!mv) return;
  const spots = Array.from(mv.querySelectorAll('.hs')).map((el) => {
    const n = (el.dataset.normal || '0 0 1').split(/\s+/).map(parseFloat);
    const len = Math.hypot(n[0], n[1], n[2]) || 1;
    return { el, n: n.map((v) => v / len) };
  });
  const update = () => {
    if (!mv.getCameraOrbit) return;
    const { theta, phi } = mv.getCameraOrbit();
    const dir = [Math.sin(phi) * Math.sin(theta), Math.cos(phi), Math.sin(phi) * Math.cos(theta)];
    spots.forEach(({ el, n }) => {
      el.classList.toggle('on', n[0] * dir[0] + n[1] * dir[1] + n[2] * dir[2] > 0.25);
    });
  };
  mv.addEventListener('camera-change', update);
  mv.addEventListener('load', update);
  update();
})();
