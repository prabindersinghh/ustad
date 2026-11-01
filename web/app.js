/* Ustad PWA client.
 *
 * Runs in iPhone Safari. The phone is a stand-in for AI glasses: it supplies
 * a point-of-view camera, a microphone and a speaker, which is exactly the
 * surface the Meta Wearables Device Access Toolkit exposes. When the glasses
 * arrive, only `MediaSource` below changes -- the protocol and the backend
 * stay identical.
 */
'use strict';

const WS_BASE = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host;
const CAPTURE_FRAME_MS = 2000;   // sample rate while an expert records
const JPEG_QUALITY = 0.7;
const FRAME_WIDTH = 960;         // downscale before upload: bandwidth + token cost

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
  else if (dest === 'capture') Capture.open();
  else if (dest === 'library') Library.open();
});

/* -------------------------------------------------------------- voice */

const Voice = {
  unlocked: false,
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
    window.speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.0;
    u.pitch = 1.0;
    window.speechSynthesis.speak(u);
  },
  stop() { window.speechSynthesis && window.speechSynthesis.cancel(); },
};

/* ------------------------------------------------------- media source */

const MediaSource_ = {
  stream: null,
  async start(videoEl, { audio = false } = {}) {
    this.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 } },
      audio,
    });
    videoEl.srcObject = this.stream;
    await videoEl.play().catch(() => {});
    await WakeLock.acquire();
    return this.stream;
  },
  stop() {
    if (this.stream) this.stream.getTracks().forEach((t) => t.stop());
    this.stream = null;
    WakeLock.release();
  },
  /* Grab the current video frame as a downscaled JPEG data URL. */
  grab(videoEl) {
    const vw = videoEl.videoWidth, vh = videoEl.videoHeight;
    if (!vw || !vh) return null;
    const w = Math.min(FRAME_WIDTH, vw);
    const h = Math.round((vh / vw) * w);
    const c = document.createElement('canvas');
    c.width = w; c.height = h;
    c.getContext('2d').drawImage(videoEl, 0, 0, w, h);
    return c.toDataURL('image/jpeg', JPEG_QUALITY);
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

/* ------------------------------------------------------------ capture */

const Capture = {
  ws: null, timer: null, recorder: null, chunks: [], recording: false,

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
    $('cap-btn').onclick = () => (this.recording ? this.stop() : this.start());
  },

  start() {
    this.ws = new WebSocket(WS_BASE + '/ws/capture');
    this.chunks = [];

    this.ws.onopen = () => {
      this.ws.send(JSON.stringify({ type: 'start' }));
      this.recording = true;
      $('cap-btn').textContent = 'Stop & build procedure';
      $('cap-btn').classList.add('stop');
      $('cap-badge').textContent = 'recording';
      $('cap-badge').classList.add('rec');

      // Sample frames on an interval rather than streaming every frame.
      this.timer = setInterval(() => {
        const data = MediaSource_.grab($('cap-video'));
        if (data && this.ws.readyState === 1) {
          this.ws.send(JSON.stringify({ type: 'frame', data }));
        }
      }, CAPTURE_FRAME_MS);

      // Record narration in one go; transcribe it when the expert stops.
      const mime = MediaRecorder.isTypeSupported('audio/mp4')
        ? 'audio/mp4' : 'audio/webm';
      try {
        this.recorder = new MediaRecorder(MediaSource_.stream, { mimeType: mime });
        this.recorder.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
        this.recorder.start();
        this.mime = mime;
      } catch (err) {
        $('cap-status').textContent = 'Audio recording unavailable: ' + err.message;
      }
    };

    this.ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.type === 'status') $('cap-status').textContent = msg.message;
      if (msg.type === 'error') $('cap-status').textContent = 'Error: ' + msg.message;
      if (msg.type === 'procedure') {
        $('cap-status').textContent =
          `Built "${msg.procedure.title}". ${msg.procedure.steps.length} steps.`;
        Voice.say(`Procedure captured. ${msg.procedure.steps.length} steps.`);
        setTimeout(() => Library.open(), 1200);
      }
    };

    this.ws.onerror = () => { $('cap-status').textContent = 'Connection failed.'; };
  },

  async stop() {
    this.recording = false;
    clearInterval(this.timer);
    $('cap-btn').textContent = 'Processing…';
    $('cap-btn').disabled = true;
    $('cap-badge').textContent = 'processing';
    $('cap-badge').classList.remove('rec');

    if (this.recorder && this.recorder.state !== 'inactive') {
      await new Promise((resolve) => {
        this.recorder.onstop = resolve;
        this.recorder.stop();
      });
      const blob = new Blob(this.chunks, { type: this.mime });
      const b64 = await blobToBase64(blob);
      this.ws.send(JSON.stringify({ type: 'audio', data: b64, mime: this.mime }));
    }
    this.ws.send(JSON.stringify({ type: 'stop' }));
    setTimeout(() => { $('cap-btn').disabled = false; $('cap-btn').classList.remove('stop'); }, 500);
  },

  teardown() {
    clearInterval(this.timer);
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    if (this.ws) { try { this.ws.close(); } catch (_) {} this.ws = null; }
    this.recording = false;
    MediaSource_.stop();
    Voice.stop();
  },
};

/* ------------------------------------------------------------ library */

const Library = {
  async open() {
    show('library');
    const list = $('library-list');
    list.innerHTML = '<div class="empty">Loading…</div>';
    try {
      const procs = await fetch('/api/procedures').then((r) => r.json());
      if (!procs.length) {
        list.innerHTML =
          '<div class="empty">No procedures yet.<br>Capture one first.</div>';
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
        el.onclick = () => Guide.open(p.id);
        list.appendChild(el);
      });
    } catch (err) {
      list.innerHTML = '<div class="empty">Could not load procedures.</div>';
    }
  },
};

/* -------------------------------------------------------------- guide */

const Guide = {
  ws: null, sessionId: null, procedureId: null, busy: false,
  closing: false, retries: 0, reconnectTimer: null,
  MAX_RETRIES: 6,

  async open(procedureId) {
    show('guide');
    this.procedureId = procedureId;
    this.sessionId = null;
    this.closing = false;
    this.retries = 0;
    $('guide-instruction').textContent = 'Starting…';
    $('guide-step-title').textContent = '';
    const wrap = $('guide-video').closest('.camera-wrap');
    try {
      await MediaSource_.start($('guide-video'), { audio: false });
      clearCameraError(wrap);
    } catch (err) {
      showCameraError(wrap, err, () => this.open(this.procedureId));
      return;
    }

    $('guide-done').onclick = () => this.check();
    $('guide-repeat').onclick = () => this.send({ type: 'repeat' });
    $('guide-skip').onclick = () => this.send({ type: 'skip' });
    this.connect();
  },

  /* Open the socket and (re)join the session. On a first connect we `start`;
   * on a reconnect we `resume`, and the server decides which step to resume at
   * from what already passed -- a dropped connection never restarts the job. */
  connect() {
    clearTimeout(this.reconnectTimer);
    this.ws = new WebSocket(WS_BASE + '/ws/guide');
    this.ws.onopen = () => {
      this.retries = 0;
      this.setBanner('');
      if (this.sessionId) {
        this.send({ type: 'resume', session_id: this.sessionId });
      } else {
        this.send({ type: 'start', procedure_id: this.procedureId });
      }
    };
    this.ws.onmessage = (evt) => this.handle(JSON.parse(evt.data));
    this.ws.onerror = () => {};  // onclose drives reconnect
    this.ws.onclose = () => { if (!this.closing) this.scheduleReconnect(); };
  },

  scheduleReconnect() {
    if (this.retries >= this.MAX_RETRIES) {
      this.setBanner('Offline. Your place is saved.', true);
      return;
    }
    const delay = Math.min(1000 * 2 ** this.retries, 8000);
    this.retries += 1;
    this.setBanner(`Reconnecting (${this.retries})…`, true);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  },

  setBanner(text, warn) {
    const el = $('guide-progress');
    if (!el) return;
    if (!text) { el.classList.remove('warn'); return; }
    el.textContent = text;
    el.classList.toggle('warn', !!warn);
  },

  send(obj) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj)); },

  handle(msg) {
    if (msg.type === 'session') {
      this.sessionId = msg.session_id;
      if (msg.resumed_at) $('guide-instruction').textContent = `Reconnected. Resuming step ${msg.resumed_at}.`;
    }

    if (msg.type === 'speak') {
      if (msg.step) { $('guide-progress').textContent = `step ${msg.step} / ${msg.of}`; }
      $('guide-progress').classList.remove('warn');   // a message means we're live again
      if (msg.title !== undefined) $('guide-step-title').textContent = msg.title || '';
      $('guide-instruction').textContent = msg.text;
      Voice.say(msg.text);
    }

    if (msg.type === 'verdict') {
      this.busy = false;
      $('guide-done').disabled = false;
      $('guide-done').textContent = 'Done. Check it';
      flash(msg.verdict);
      drawBoxes(msg.detections || []);
    }

    if (msg.type === 'complete') {
      Report.open(msg.session_id);
    }

    if (msg.type === 'error') {
      this.busy = false;
      $('guide-done').disabled = false;
      $('guide-done').textContent = 'Done. Check it';
      $('guide-instruction').textContent = 'Error: ' + msg.message;
    }
  },

  check() {
    if (this.busy) return;
    const data = MediaSource_.grab($('guide-video'));
    if (!data) return;
    this.busy = true;
    $('guide-done').disabled = true;
    $('guide-done').textContent = 'Looking…';
    Voice.stop();
    this.send({ type: 'check', data, said: 'done' });
  },

  teardown() {
    this.closing = true;                 // stop the reconnect loop from firing
    clearTimeout(this.reconnectTimer);
    if (this.ws) {
      this.send({ type: 'abort' });
      try { this.ws.close(); } catch (_) {}
      this.ws = null;
    }
    MediaSource_.stop();
    Voice.stop();
    clearBoxes();
  },
};

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
    const avg = events.length
      ? Math.round(events.reduce((a, e) => a + (e.latency_ms || 0), 0) / events.length)
      : 0;

    body.innerHTML =
      `<div class="kpis">
         <div class="kpi"><b>${passes}</b><span>verified</span></div>
         <div class="kpi"><b>${fails}</b><span>caught</span></div>
         <div class="kpi"><b>${avg}ms</b><span>per check</span></div>
       </div>`;

    events.forEach((e) => {
      const card = document.createElement('div');
      card.className = 'card event ' + e.verdict;
      const title = document.createElement('h3');
      title.textContent = `Step ${e.step_index + 1}: ${e.verdict}`;
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

function drawBoxes(dets) {
  const video = $('guide-video');
  const c = $('guide-overlay');
  c.width = video.clientWidth;
  c.height = video.clientHeight;
  const ctx = c.getContext('2d');
  ctx.clearRect(0, 0, c.width, c.height);
  if (!dets.length) return;

  // Video is object-fit:cover, so replicate the crop maths.
  const vr = video.videoWidth / video.videoHeight;
  const cr = c.width / c.height;
  let dw, dh, ox, oy;
  if (vr > cr) { dh = c.height; dw = dh * vr; ox = (c.width - dw) / 2; oy = 0; }
  else { dw = c.width; dh = dw / vr; ox = 0; oy = (c.height - dh) / 2; }

  ctx.lineWidth = 3;
  ctx.strokeStyle = '#ff7a1a';
  ctx.fillStyle = '#ff7a1a22';
  dets.slice(0, 4).forEach((d) => {
    const x = ox + (d.x1 / d.img_w) * dw;
    const y = oy + (d.y1 / d.img_h) * dh;
    const w = ((d.x2 - d.x1) / d.img_w) * dw;
    const h = ((d.y2 - d.y1) / d.img_h) * dh;
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x, y, w, h);
  });
  setTimeout(clearBoxes, 2500);
}

function clearBoxes() {
  const c = $('guide-overlay');
  if (c && c.getContext) c.getContext('2d').clearRect(0, 0, c.width, c.height);
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

/* Clean, actionable overlay when the camera/mic is unavailable or denied.
 * iOS silently keeps a denial until the site's data is cleared, so the message
 * has to tell the worker exactly where to go rather than just failing. */
function showCameraError(wrapEl, err, retry) {
  if (!wrapEl) return;
  clearCameraError(wrapEl);
  const denied = err && (err.name === 'NotAllowedError' || err.name === 'SecurityError');
  const missing = err && (err.name === 'NotFoundError' || err.name === 'OverconstrainedError');
  const title = denied ? 'Camera and microphone are blocked'
    : missing ? 'No camera found'
    : 'Could not start the camera';
  const help = denied
    ? 'Ustad needs the camera to check your work. Enable it in Settings, Safari, Camera, then reload.'
    : missing ? 'This device did not expose a camera to the browser.'
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

fetch('/api/health')
  .then((r) => r.json())
  .then((h) => {
    $('health-dot').className = 'dot ' + (h.key_configured ? 'ok' : 'bad');
    if (!h.key_configured) {
      $('health-text').textContent = 'MODEL_API_KEY not set on the server';
      return;
    }
    const where = h.reasoning_backend === 'open' ? 'self-hosted' : 'Meta Model API';
    $('health-text').textContent = `${h.models.reasoning} · ${h.models.sam} · ${where}`;
  })
  .catch(() => {
    $('health-dot').className = 'dot bad';
    $('health-text').textContent = 'backend unreachable';
  });

/* -------------------------------------------------- landing jump nav */
/* The landing is one flowing page; the jump bar scrolls to a section and
 * highlights whichever section is currently in view. */
(function initJump() {
  const links = Array.from(document.querySelectorAll('a[data-jump]'));
  const root = document.getElementById('screen-home');
  if (!links.length || !root) return;
  const byId = new Map(links.map((a) => [a.dataset.jump, a]));
  const setActive = (id) => links.forEach((a) => a.classList.toggle('active', a.dataset.jump === id));
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => {
      if (e.isIntersecting && byId.has(e.target.id)) {
        setActive(e.target.id);
      }
    });
  }, { root, rootMargin: '-45% 0px -50% 0px' });
  links.forEach((a) => { const s = document.getElementById(a.dataset.jump); if (s) io.observe(s); });
})();

/* The loop figure is same-origin: size its frame to the drawing so it never
 * shows a scrollbar, and again whenever the page width changes. */
(function fitLoopFigure() {
  const frame = document.getElementById('loop-frame');
  if (!frame) return;
  const fit = () => {
    try {
      const doc = frame.contentDocument;
      const plate = doc && doc.querySelector('.iso-plate');
      if (!plate) return;
      doc.documentElement.style.overflow = 'hidden';
      frame.style.height = Math.ceil(plate.getBoundingClientRect().bottom + 12) + 'px';
    } catch (_) {}
  };
  frame.addEventListener('load', fit);
  window.addEventListener('resize', fit);
  fit();
})();
