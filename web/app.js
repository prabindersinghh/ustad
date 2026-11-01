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
  else if (dest === 'capture') Capture.open();
  else if (dest === 'library') Library.open();
});

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
    u.rate = 1.02;
    u.onend = u.onerror = () => { this.quietUntil = Date.now() + 450; };
    this.quietUntil = Date.now() + 60000;   // until onend says otherwise
    window.speechSynthesis.speak(u);
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
    this.stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' }, width: { ideal: 1280 } },
      audio: audio ? { echoCancellation: true, noiseSuppression: true, autoGainControl: true } : false,
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
  grab(videoEl, width = FRAME_WIDTH, quality = JPEG_QUALITY) {
    const vw = videoEl.videoWidth, vh = videoEl.videoHeight;
    if (!vw || !vh) return null;
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

  async start(stream, onchunk) {
    this.stop();
    const track = stream && stream.getAudioTracks()[0];
    if (!track) throw new Error('no microphone track');
    this.onchunk = onchunk;
    const AC = window.AudioContext || window.webkitAudioContext;
    this.ctx = new AC();
    await this.ctx.resume();
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
            this.onchunk && this.onchunk(chunk.buffer);
            out = new Int16Array(this.CHUNK); n = 0;
          }
        }
      }
    };

    if (this.ctx.audioWorklet) {
      const code = `class M extends AudioWorkletProcessor{process(i){const c=i[0]&&i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor('ustad-mic',M)`;
      const url = URL.createObjectURL(new Blob([code], { type: 'application/javascript' }));
      await this.ctx.audioWorklet.addModule(url);
      this.node = new AudioWorkletNode(this.ctx, 'ustad-mic');
      this.node.port.onmessage = (e) => push(e.data);
    } else {                                  // older Safari
      this.node = this.ctx.createScriptProcessor(4096, 1, 1);
      this.node.onaudioprocess = (e) => push(e.inputBuffer.getChannelData(0));
    }
    this.src.connect(this.node);
    this.node.connect(this.sink);
  },

  stop() {
    try { this.src && this.src.disconnect(); } catch (_) {}
    try { this.node && this.node.disconnect(); } catch (_) {}
    if (this.ctx) this.ctx.close().catch(() => {});
    this.ctx = this.src = this.node = this.sink = null;
    this.onchunk = null;
  },
};

/* ------------------------------------------------------------ capture */

const Capture = {
  ws: null, timer: null, recorder: null, chunks: [], recording: false,
  connecting: false, processing: false, streamingMic: false, frames: 0,

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
    this.resetButton();
    this.clearDrafts();
    $('cap-status').textContent = 'Describe each step out loud as you do it.';
    $('cap-btn').onclick = () => (this.recording ? this.stop() : this.start());
  },

  start() {
    // One tap, one socket: a second tap while connecting used to open a second
    // capture socket and split the frames from the stop message.
    if (this.connecting || this.recording) return;
    this.connecting = true;
    this.frames = 0;
    this.clearDrafts();
    $('cap-btn').disabled = true;
    $('cap-btn').textContent = 'Connecting…';
    this.ws = new WebSocket(WS_BASE + '/ws/capture');
    this.ws.binaryType = 'arraybuffer';
    this.chunks = [];

    this.ws.onopen = async () => {
      this.connecting = false;
      this.ws.send(JSON.stringify({ type: 'start' }));
      this.recording = true;
      $('cap-btn').disabled = false;
      $('cap-btn').textContent = 'Stop & build procedure';
      $('cap-btn').classList.add('stop');
      $('cap-badge').classList.add('rec');
      this.setBadge();
      $('cap-status').textContent = 'Listening. Say each step as you do it.';

      // First frame now, then sample on an interval. Even a short take has frames.
      this.sendFrame();
      this.timer = setInterval(() => this.sendFrame(), CAPTURE_FRAME_MS);

      // Narration streams live: captions and step cards appear as you talk.
      try {
        await Mic.start(MediaSource_.stream, (buf) => {
          if (this.ws && this.ws.readyState === 1) this.ws.send(buf);
        });
        this.streamingMic = true;
      } catch (err) {
        // Fall back to recording the narration and uploading it at the end.
        this.streamingMic = false;
        const audioOnly = new MediaStream(MediaSource_.stream.getAudioTracks());
        const mime = MediaRecorder.isTypeSupported('audio/mp4') ? 'audio/mp4' : 'audio/webm';
        try {
          this.recorder = new MediaRecorder(audioOnly, { mimeType: mime });
          this.recorder.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
          this.recorder.start();
          this.mime = mime;
        } catch (err2) {
          $('cap-status').textContent = 'Narration recording unavailable: ' + err2.message;
        }
      }
    };

    this.ws.onmessage = (evt) => {
      const msg = JSON.parse(evt.data);
      if (msg.type === 'frame_ack') { this.frames = msg.count; this.setBadge(); }
      if (msg.type === 'heard') this.caption(msg.text, msg.final);
      if (msg.type === 'draft') this.addDraft(msg);
      if (msg.type === 'draft_title') this.titleDraft(msg);
      if (msg.type === 'status') $('cap-status').textContent = msg.message;
      if (msg.type === 'error') {
        $('cap-status').textContent = msg.message;
        this.resetButton();
      }
      if (msg.type === 'procedure') {
        $('cap-status').textContent =
          `Saved "${msg.procedure.title}". ${msg.procedure.steps.length} steps. Opening your procedures…`;
        $('cap-badge').textContent = 'saved';
        Voice.say(`Procedure saved. ${msg.procedure.steps.length} steps.`);
        setTimeout(() => Library.open(msg.procedure_id), 1600);
      }
    };

    this.ws.onerror = () => {};
    this.ws.onclose = () => {
      if (this.connecting || this.recording || this.processing) {
        $('cap-status').textContent = 'Lost the connection to the server. Check the laptop is running, then try again.';
        this.connecting = this.recording = this.processing = false;
        clearInterval(this.timer);
        Mic.stop();
        this.resetButton();
      }
    };
  },

  sendFrame() {
    const data = MediaSource_.grab($('cap-video'));
    if (data && this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: 'frame', data }));
    }
  },

  setBadge() { $('cap-badge').textContent = `recording · ${this.frames || 0} frames`; },

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

  titleDraft({ n, title }) {
    const card = document.querySelector(`#cap-drafts .draft[data-n="${n}"] .draft-title`);
    if (card) card.textContent = title;
  },

  resetButton() {
    this.processing = false;
    $('cap-btn').disabled = false;
    $('cap-btn').textContent = 'Start recording';
    $('cap-btn').classList.remove('stop');
    $('cap-badge').classList.remove('rec');
    $('cap-badge').textContent = 'ready';
  },

  async stop() {
    if (!this.recording) return;
    this.recording = false;
    this.processing = true;
    clearInterval(this.timer);
    this.sendFrame();   // the end state of the job is the most useful frame
    $('cap-btn').textContent = 'Building your procedure…';
    $('cap-btn').disabled = true;
    $('cap-badge').textContent = 'processing';
    $('cap-badge').classList.remove('rec');

    if (this.streamingMic) {
      // Give the last words a moment to reach the server, then stop the mic.
      await new Promise((r) => setTimeout(r, 400));
      Mic.stop();
    } else if (this.recorder && this.recorder.state !== 'inactive') {
      await new Promise((resolve) => {
        this.recorder.onstop = resolve;
        this.recorder.stop();
      });
      const blob = new Blob(this.chunks, { type: this.mime });
      const b64 = await blobToBase64(blob);
      this.ws.send(JSON.stringify({ type: 'audio', data: b64, mime: this.mime }));
    }
    this.ws.send(JSON.stringify({ type: 'stop' }));
  },

  teardown() {
    clearInterval(this.timer);
    Mic.stop();
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    this.recording = this.connecting = this.processing = false;
    if (this.ws) { const ws = this.ws; this.ws = null; ws.onclose = null; try { ws.close(); } catch (_) {} }
    MediaSource_.stop();
    Voice.stop();
  },
};

/* ------------------------------------------------------------ library */

const Library = {
  async open(highlightId) {
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
          tag.textContent = 'Just captured';
          el.prepend(tag);
        }
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
  closing: false, retries: 0, reconnectTimer: null, watchTimer: null, step: 0,
  MAX_RETRIES: 6,

  async open(procedureId) {
    show('guide');
    this.procedureId = procedureId;
    this.sessionId = null;
    this.closing = false;
    this.retries = 0;
    this.step = 0;
    $('guide-instruction').textContent = 'Starting…';
    $('guide-step-title').textContent = '';
    this.setLive('connecting', 'Connecting');
    const wrap = $('guide-video').closest('.camera-wrap');
    try {
      await MediaSource_.start($('guide-video'), { audio: true });
      clearCameraError(wrap);
    } catch (err) {
      showCameraError(wrap, err, () => this.open(this.procedureId));
      return;
    }

    $('guide-done').onclick = () => this.check();
    $('guide-repeat').onclick = () => this.send({ type: 'repeat' });
    $('guide-skip').onclick = () => this.send({ type: 'skip' });
    LiveBox.start();
    this.connect();
  },

  /* Open the socket and (re)join the session. On a first connect we `start`;
   * on a reconnect we `resume`, and the server decides which step to resume at
   * from what already passed -- a dropped connection never restarts the job. */
  connect() {
    clearTimeout(this.reconnectTimer);
    this.ws = new WebSocket(WS_BASE + '/ws/guide');
    this.ws.binaryType = 'arraybuffer';
    this.ws.onopen = async () => {
      this.retries = 0;
      this.setBanner('');
      if (this.sessionId) this.send({ type: 'resume', session_id: this.sessionId });
      else this.send({ type: 'start', procedure_id: this.procedureId });

      // The live view: one small frame a second for the watcher.
      clearInterval(this.watchTimer);
      this.watchTimer = setInterval(() => this.sendWatch(), WATCH_FRAME_MS);
      this.sendWatch();

      // Hands-free: the mic streams to the server, which listens for commands.
      try {
        await Mic.start(MediaSource_.stream, (buf) => {
          if (this.ws && this.ws.readyState === 1) this.ws.send(buf);
        });
        $('guide-hints').classList.add('on');
      } catch (_) {
        $('guide-hints').classList.remove('on');
      }
    };
    this.ws.onmessage = (evt) => this.handle(JSON.parse(evt.data));
    this.ws.onerror = () => {};  // onclose drives reconnect
    this.ws.onclose = () => {
      clearInterval(this.watchTimer);
      Mic.stop();
      if (!this.closing) this.scheduleReconnect();
    };
  },

  /* Send the live view only when it changed. Grounding a frame costs a SAM
   * call, and a camera resting on the work shows the same thing second after
   * second -- so compare a 32x24 grayscale thumbnail with the last one sent
   * and, if nothing moved, send a tiny "still" heartbeat instead. */
  sendWatch() {
    if (!this.ws || this.ws.readyState !== 1) return;
    const video = $('guide-video');
    if (!video.videoWidth) return;
    const t = this.thumb || (this.thumb = Object.assign(document.createElement('canvas'), { width: 32, height: 24 }));
    const tctx = t.getContext('2d', { willReadFrequently: true });
    tctx.drawImage(video, 0, 0, 32, 24);
    const px = tctx.getImageData(0, 0, 32, 24).data;
    const gray = new Uint8Array(32 * 24);
    for (let i = 0; i < gray.length; i++) gray[i] = (px[i * 4] * 3 + px[i * 4 + 1] * 6 + px[i * 4 + 2]) / 10;
    let diff = 255;
    if (this.lastGray) {
      let s = 0;
      for (let i = 0; i < gray.length; i++) s += Math.abs(gray[i] - this.lastGray[i]);
      diff = s / gray.length;
    }
    const stale = Date.now() - (this.lastSentAt || 0) > 8000;   // refresh now and then anyway
    if (diff < 7 && !stale) { this.send({ type: 'still' }); return; }
    const data = MediaSource_.grab(video, WATCH_WIDTH, 0.62);
    if (!data) return;
    this.send({ type: 'watch', data });
    this.lastGray = gray;
    this.lastSentAt = Date.now();
    this.haveView = true;
  },

  scheduleReconnect() {
    if (this.retries >= this.MAX_RETRIES) {
      this.setBanner('Offline. Your place is saved.', true);
      return;
    }
    const delay = Math.min(1000 * 2 ** this.retries, 8000);
    this.retries += 1;
    this.setBanner(`Reconnecting (${this.retries})…`, true);
    this.setLive('connecting', 'Reconnecting');
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
    el.querySelector('b').textContent = label;
  },

  send(obj) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj)); },

  resetDone() {
    this.busy = false;
    $('guide-done').disabled = false;
    $('guide-done').textContent = 'Done. Check it';
  },

  handle(msg) {
    switch (msg.type) {
      case 'session':
        this.sessionId = msg.session_id;
        if (msg.resumed_at) $('guide-instruction').textContent = `Reconnected. Resuming step ${msg.resumed_at}.`;
        this.setLive('watching', msg.auto ? 'Watching' : 'Live');
        break;

      case 'speak':
        if (msg.step) {
          $('guide-progress').textContent = `step ${msg.step} / ${msg.of}`;
          if (msg.step !== this.step && msg.target !== undefined) LiveBox.reset(msg.target);
          this.step = msg.step;
        }
        $('guide-progress').classList.remove('warn');
        if (msg.title !== undefined) $('guide-step-title').textContent = msg.title || '';
        $('guide-instruction').textContent = msg.text;
        Voice.say(msg.text);
        break;

      case 'track':
        LiveBox.update(msg.detections || [], msg.target);
        break;

      case 'heard':
        this.caption(msg.text, msg.final);
        break;

      case 'checking':
        this.busy = true;
        $('guide-done').disabled = true;
        $('guide-done').textContent = 'Looking…';
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

      case 'pulse':
        LiveBox.pulse();
        break;

      case 'budget':
        this.setLive('connecting', 'Budget saver');
        this.toast(`Spend $${msg.spent} of $${msg.budget}: live watching paused, say done to check`);
        break;

      case 'verdict':
        this.resetDone();
        flash(msg.verdict);
        LiveBox.flash(msg.verdict);
        this.setLive('watching', 'Watching');
        if (msg.source === 'auto' && msg.advance) this.toast('Verified automatically');
        else if (msg.latency_ms) this.toast(`${msg.verdict === 'pass' ? 'Verified' : msg.verdict === 'fail' ? 'Caught' : 'Need a better view'} · ${(msg.latency_ms / 1000).toFixed(1)} s`);
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
    el.textContent = text || 'Unsafe. Stop.';
    el.classList.add('on');
    clearTimeout(this.hazTimer);
    this.hazTimer = setTimeout(() => el.classList.remove('on'), 6000);
  },

  check() {
    if (this.busy) return;
    // The server judges its own live view (already grounded, so it's fast).
    // Before the first view arrives, send one with the request.
    const msg = { type: 'check', said: 'done' };
    if (!this.haveView) {
      const data = MediaSource_.grab($('guide-video'));
      if (!data) return;
      msg.data = data;
    }
    this.send(msg);
  },

  teardown() {
    this.closing = true;                 // stop the reconnect loop from firing
    clearTimeout(this.reconnectTimer);
    clearInterval(this.watchTimer);
    Mic.stop();
    if (this.ws) {
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
  },
};

/* ------------------------------------------------------------ live box */

/* Draws the step's target on top of the camera like an AR marker: corner
 * brackets and a label, eased toward each new SAM grounding so it reads as
 * tracking rather than flicker, fading out when the target is lost. */
const LiveBox = {
  dets: [], target: '', at: 0, cur: null, alpha: 0, raf: 0,
  flashColor: null, flashUntil: 0, pulseAt: 0,

  start() { if (!this.raf) this.loop(); },
  stop() {
    cancelAnimationFrame(this.raf); this.raf = 0;
    this.dets = []; this.cur = null; this.alpha = 0;
    const c = $('guide-overlay');
    if (c && c.getContext) c.getContext('2d').clearRect(0, 0, c.width, c.height);
  },
  reset(target) { this.dets = []; this.cur = null; this.alpha = 0; this.target = target || ''; },
  update(dets, target) { this.dets = dets; if (target) this.target = target; this.at = performance.now(); },
  flash(verdict) {
    this.flashColor = verdict === 'pass' ? '#12d98a' : verdict === 'fail' ? '#ff5a5f' : '#ffb020';
    this.flashUntil = performance.now() + 900;
  },
  pulse() { this.pulseAt = performance.now(); },

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

    const now = performance.now();
    const best = this.dets.length
      ? this.dets.reduce((a, b) => ((b.confidence || 0) > (a.confidence || 0) ? b : a))
      : null;
    const stale = now - this.at > 3000;
    const wantAlpha = best && !stale ? 1 : 0;
    this.alpha += (wantAlpha - this.alpha) * 0.12;
    if (this.alpha < 0.02 && !best) return;

    if (best) {
      // Video is object-fit:cover, so replicate the crop maths.
      const vr = video.videoWidth / video.videoHeight;
      const cr = W / H;
      let dw, dh, ox, oy;
      if (vr > cr) { dh = H; dw = dh * vr; ox = (W - dw) / 2; oy = 0; }
      else { dw = W; dh = dw / vr; ox = 0; oy = (H - dh) / 2; }
      const r = {
        x: ox + (best.x1 / best.img_w) * dw, y: oy + (best.y1 / best.img_h) * dh,
        w: ((best.x2 - best.x1) / best.img_w) * dw, h: ((best.y2 - best.y1) / best.img_h) * dh,
      };
      if (!this.cur) this.cur = r;
      else for (const k of ['x', 'y', 'w', 'h']) this.cur[k] += (r[k] - this.cur[k]) * 0.22;
    }
    if (!this.cur) return;

    const { x, y, w, h } = this.cur;
    const color = now < this.flashUntil ? this.flashColor : '#3b82ff';
    ctx.globalAlpha = this.alpha;

    ctx.fillStyle = color + '1f';
    roundRect(ctx, x, y, w, h, 10); ctx.fill();

    const L = Math.max(14, Math.min(w, h) * 0.26);
    ctx.strokeStyle = color; ctx.lineWidth = 4; ctx.lineCap = 'round';
    ctx.shadowColor = color; ctx.shadowBlur = 12;
    ctx.beginPath();
    ctx.moveTo(x, y + L); ctx.lineTo(x, y); ctx.lineTo(x + L, y);
    ctx.moveTo(x + w - L, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w, y + L);
    ctx.moveTo(x + w, y + h - L); ctx.lineTo(x + w, y + h); ctx.lineTo(x + w - L, y + h);
    ctx.moveTo(x + L, y + h); ctx.lineTo(x, y + h); ctx.lineTo(x, y + h - L);
    ctx.stroke();
    ctx.shadowBlur = 0;

    const since = now - this.pulseAt;
    if (since < 1400) {
      const k = since / 1400;
      ctx.globalAlpha = this.alpha * (1 - k);
      ctx.lineWidth = 3;
      roundRect(ctx, x - 14 * k * 2, y - 14 * k * 2, w + 56 * k, h + 56 * k, 14); ctx.stroke();
      ctx.globalAlpha = this.alpha;
    }

    if (this.target) {
      const label = this.target;
      ctx.font = '600 14px Figtree, -apple-system, sans-serif';
      const tw = ctx.measureText(label).width;
      const lx = Math.max(6, Math.min(x, W - tw - 30));
      const ly = y > 34 ? y - 34 : y + h + 8;
      ctx.fillStyle = color;
      roundRect(ctx, lx, ly, tw + 24, 26, 13); ctx.fill();
      ctx.fillStyle = '#fff';
      ctx.fillText(label, lx + 12, ly + 18);
    }
    ctx.globalAlpha = 1;
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

    body.innerHTML =
      `<div class="kpis">
         <div class="kpi"><b>${passes}</b><span>verified${autos ? ` (${autos} auto)` : ''}</span></div>
         <div class="kpi"><b>${fails}</b><span>caught</span></div>
         <div class="kpi"><b>${avg} s</b><span>per check</span></div>
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
