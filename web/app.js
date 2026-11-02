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
    // resume() outside a gesture can hang on iOS rather than reject.
    await Promise.race([this.ctx.resume(), new Promise((r) => setTimeout(r, 800))]);
    if (this.ctx.state !== 'running') {
      this.stop();
      throw new Error('audio suspended');     // caller falls back to MediaRecorder
    }
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

  disconnect() {
    try { this.src && this.src.disconnect(); } catch (_) {}
    try { this.node && this.node.disconnect(); } catch (_) {}
    try { this.sink && this.sink.disconnect(); } catch (_) {}
    this.src = this.node = this.sink = null;
    this.onchunk = null;
    this.onlevel = null;
  },

  stop() {
    this.disconnect();
    if (this.ctx) this.ctx.close().catch(() => {});
    this.ctx = null;
  },
};

/* ------------------------------------------------------------ capture */

/* Recording a job is the expert's one shot, so the screen tells them exactly
 * where they are: a countdown before it starts, a job clock with a guide
 * length and a hard limit, a live mic level, pause and resume, and a "Mark
 * step" button (or saying "next step") to set step boundaries themselves. */
const CAPTURE_GUIDE_S = [60, 300];   // "aim for 1 to 5 minutes"
const CAPTURE_LIMIT_S = 600;         // 10 minutes, then it stops and builds

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
      'Aim for 1 to 5 minutes. Say each step out loud as you do it, or tap Mark step between steps.';
    $('cap-btn').onclick = () => (this.recording ? this.stop() : this.start());
    $('cap-pause').onclick = () => (this.paused ? this.resume() : this.pause());
    $('cap-mark').onclick = () => this.mark();
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
      $('cap-status').textContent = 'Ten minute limit reached. Building your procedure.';
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
    this.ws = new WebSocket(WS_BASE + '/ws/capture');
    this.ws.binaryType = 'arraybuffer';
    this.chunks = [];

    this.ws.onopen = async () => {
      this.connecting = false;
      this.ws.send(JSON.stringify({ type: 'start' }));
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
      this.sendFrame();
      this.timer = setInterval(() => this.sendFrame(), CAPTURE_FRAME_MS);

      // Narration streams live: captions and step cards appear as you talk.
      try {
        this.micArgs = [
          (buf) => { if (!this.paused && this.ws && this.ws.readyState === 1) this.ws.send(buf); },
          (level) => { $('cap-level').style.width = (this.paused ? 0 : Math.round(level * 100)) + '%'; },
        ];
        await Mic.start(MediaSource_.stream, ...this.micArgs);
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
      if (msg.type === 'marker') this.addMarker(msg);
      if (msg.type === 'status') $('cap-status').textContent = msg.message;
      if (msg.type === 'error') {
        $('cap-status').textContent = msg.message;
        this.resetButton();
      }
      if (msg.type === 'procedure') {
        this.setState('saved', 'saved');
        $('cap-status').textContent =
          `Saved "${msg.procedure.title}". ${msg.procedure.steps.length} steps. Opening your procedures…`;
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
        clearInterval(this.clock);
        Mic.stop();
        this.resetButton();
      }
    };
  },

  /* ---- pause / resume / mark -------------------------------------------- */
  pause() {
    if (!this.recording || this.paused) return;
    this.paused = true;
    this.pausedAt = Date.now();
    clearInterval(this.timer);
    this.send({ type: 'pause' });
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
    this.sendFrame();
    this.timer = setInterval(() => this.sendFrame(), CAPTURE_FRAME_MS);
    this.setState('recording', 'recording');
    $('cap-pause').textContent = 'Pause';
    $('cap-mark').disabled = false;
    $('cap-status').textContent = 'Recording again.';
  },

  mark() {
    if (!this.recording || this.paused) return;
    this.sendFrame();                // the frame at the boundary is worth keeping
    this.send({ type: 'mark' });
  },

  send(obj) { if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj)); },

  sendFrame() {
    if (this.paused) return;
    const data = MediaSource_.grab($('cap-video'));
    if (data && this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({ type: 'frame', data }));
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
    this.sendFrame();   // the end state of the job is the most useful frame
    this.setState('building', 'building');
    $('cap-btn').textContent = 'Building your procedure…';
    $('cap-btn').disabled = true;
    $('cap-pause').hidden = true;
    $('cap-mark').hidden = true;

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
    clearInterval(this.clock);
    Mic.stop();
    if (this.recorder && this.recorder.state !== 'inactive') this.recorder.stop();
    this.recording = this.connecting = this.processing = this.paused = false;
    this.startedAt = 0;
    if (this.ws) { const ws = this.ws; this.ws = null; ws.onclose = null; try { ws.close(); } catch (_) {} }
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
    Mic.prime();                                // inside the tap that opened the job
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
    MediaSource_.onlost = (why) => cameraLost(wrap, why, () => this.restartCamera());

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
        await Mic.start(MediaSource_.stream, this.micSend);
        $('guide-hints').classList.add('on');
      } catch (_) {
        $('guide-hints').classList.remove('on');
      }
    };
    this.ws.onmessage = (evt) => this.handle(JSON.parse(evt.data));
    this.ws.onerror = () => {};  // onclose drives reconnect
    this.ws.onclose = () => {
      clearInterval(this.watchTimer);
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

      case 'proximity':
        LiveBox.danger(msg.hazard);
        this.hazard(`Hand near the ${msg.hazard}. Keep clear.`);
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
        if (msg.verdict === 'confirmed') this.toast('Confirmed by voice');
        else if (msg.source === 'auto' && msg.advance) this.toast('Verified automatically');
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

  /* What kind of check this step is: a gauge reading to hit, a timer to
   * wait out, or a check only the operator can confirm. */
  setSpec({ kind, reading, duration }) {
    const el = $('guide-spec');
    clearInterval(this.specTimer);
    el.className = 'step-spec ' + (kind || 'visual');
    if (kind === 'reading' && reading) {
      el.textContent = `Read: ${reading}`;
      el.hidden = false;
    } else if (kind === 'timed' && duration) {
      const end = Date.now() + duration * 1000;
      const draw = () => {
        const left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
        el.textContent = left > 0 ? `Timer ${mmss(left)}` : 'Time is up. Say done.';
        el.classList.toggle('up', left === 0);
        if (left === 0) { clearInterval(this.specTimer); Voice.say('Time is up.'); }
      };
      draw();
      this.specTimer = setInterval(draw, 500);
      el.hidden = false;
    } else if (kind === 'confirm') {
      el.textContent = 'Not visible to the camera. Say done once you have checked it.';
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
      if (!data) {
        if (MediaSource_.dark) this.toast('No picture from the camera. Check the lens is clear.');
        return;
      }
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
    clearInterval(this.specTimer);
    $("guide-spec").hidden = true;
  },
};

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

    // hazards first, then tools, then the target on top
    const order = { hazard: 0, tool: 1, target: 2 };
    Object.values(this.tracks).sort((a, b) => order[a.role] - order[b.role]).forEach((t) => {
      const best = (t.dets || []).length
        ? t.dets.reduce((a, b) => ((b.confidence || 0) > (a.confidence || 0) ? b : a)) : null;
      const want = best && now - t.at < 3000 ? 1 : 0;
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
      ctx.shadowColor = color; ctx.shadowBlur = t.role === 'target' ? 12 : 6;
      ctx.beginPath();
      ctx.moveTo(x, y + L); ctx.lineTo(x, y); ctx.lineTo(x + L, y);
      ctx.moveTo(x + w - L, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w, y + L);
      ctx.moveTo(x + w, y + h - L); ctx.lineTo(x + w, y + h); ctx.lineTo(x + w - L, y + h);
      ctx.moveTo(x + L, y + h); ctx.lineTo(x, y + h); ctx.lineTo(x, y + h - L);
      ctx.stroke();
      ctx.shadowBlur = 0;
    }

    if (t.role === 'target') {
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
      ctx.fillStyle = t.role === 'tool' ? '#052414' : '#fff';
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
