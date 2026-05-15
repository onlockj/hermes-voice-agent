// HERMES VOICE LINK — client
// Pipeline:
//   mic → AudioWorklet (Float32 @ ctx rate) → resample to PCM16 @ 24kHz
//        → WebSocket binary frames to /ws/voice
//   server → base64 pcm16 chunks (response.audio.delta) → queue → AudioBufferSourceNode

(() => {
  const TARGET_SAMPLE_RATE = 24000;
  const FRAME_MS = 40; // ~40ms per chunk

  const $ = (id) => document.getElementById(id);
  const statusDot = $("status-dot");
  const statusText = $("status-text");
  const sessionEl = $("session-id");
  const latencyEl = $("latency");
  const captionEl = $("caption");
  const connectBtn = $("connect-btn");
  const muteBtn = $("mute-btn");
  const disconnectBtn = $("disconnect-btn");
  const viz = $("viz");

  const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  if (tg) {
    try {
      tg.ready();
      tg.expand();
      tg.setHeaderColor && tg.setHeaderColor("#0a0a0a");
      tg.setBackgroundColor && tg.setBackgroundColor("#0a0a0a");
    } catch (_) {}
  }

  // ── State ────────────────────────────────────────────────────────
  const state = {
    ws: null,
    audioCtx: null,
    micStream: null,
    micNode: null,
    workletNode: null,
    muted: false,
    playbackCtx: null,
    playHead: 0,
    sources: [],
    captionBuf: "",
    lastSendAt: 0,
    lastReplyAt: 0,
    vizData: new Uint8Array(64),
    analyser: null,
    rafId: null,
    pendingResample: [],
    resampleSrcRate: 48000,
  };

  // ── UI helpers ───────────────────────────────────────────────────
  function setStatus(kind, text) {
    statusDot.className = "dot dot--" + kind;
    statusText.textContent = text;
  }

  function showCaption(text) {
    captionEl.textContent = text;
  }

  function appendCaption(delta) {
    state.captionBuf += delta;
    captionEl.textContent = state.captionBuf;
  }

  function resetCaption() {
    state.captionBuf = "";
    captionEl.textContent = "";
  }

  // ── Resampler (linear, good enough for speech 48k→24k) ──────────
  function resampleFloat32(input, srcRate, dstRate) {
    if (srcRate === dstRate) return input;
    const ratio = srcRate / dstRate;
    const newLength = Math.floor(input.length / ratio);
    const out = new Float32Array(newLength);
    for (let i = 0; i < newLength; i++) {
      const srcIdx = i * ratio;
      const i0 = Math.floor(srcIdx);
      const i1 = Math.min(i0 + 1, input.length - 1);
      const t = srcIdx - i0;
      out[i] = input[i0] * (1 - t) + input[i1] * t;
    }
    return out;
  }

  function floatToPCM16(f32) {
    const out = new Int16Array(f32.length);
    for (let i = 0; i < f32.length; i++) {
      const s = Math.max(-1, Math.min(1, f32[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }

  function pcm16ToFloat(int16) {
    const out = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      out[i] = int16[i] / 0x8000;
    }
    return out;
  }

  function b64ToBytes(b64) {
    const bin = atob(b64);
    const len = bin.length;
    const bytes = new Uint8Array(len);
    for (let i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return bytes;
  }

  // ── Mic capture (AudioWorklet if available, fallback to ScriptProcessor) ──
  async function startMic() {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });
    state.micStream = stream;

    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    state.audioCtx = ctx;
    state.resampleSrcRate = ctx.sampleRate;

    const source = ctx.createMediaStreamSource(stream);

    // Analyser for viz
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 128;
    state.analyser = analyser;
    source.connect(analyser);

    const frameSamples = Math.floor((ctx.sampleRate * FRAME_MS) / 1000);
    // Power of two close to frameSamples
    const bufferSize = 2048;

    // ScriptProcessor is deprecated but universally available; fine for v1.
    const proc = ctx.createScriptProcessor(bufferSize, 1, 1);
    proc.onaudioprocess = (ev) => {
      if (state.muted) return;
      if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      const inBuf = ev.inputBuffer.getChannelData(0);
      const resampled = resampleFloat32(inBuf, ctx.sampleRate, TARGET_SAMPLE_RATE);
      const pcm = floatToPCM16(resampled);
      try {
        state.ws.send(pcm.buffer);
        state.lastSendAt = performance.now();
      } catch (_) {}
    };
    source.connect(proc);
    proc.connect(ctx.destination); // required for ScriptProcessor to fire on some browsers
    state.workletNode = proc;
    state.micNode = source;
  }

  function stopMic() {
    try { state.workletNode && state.workletNode.disconnect(); } catch (_) {}
    try { state.micNode && state.micNode.disconnect(); } catch (_) {}
    try { state.analyser && state.analyser.disconnect(); } catch (_) {}
    if (state.micStream) {
      state.micStream.getTracks().forEach((t) => t.stop());
    }
    state.micStream = null;
    state.workletNode = null;
    state.micNode = null;
  }

  // ── Playback ─────────────────────────────────────────────────────
  function ensurePlaybackCtx() {
    if (state.playbackCtx) return state.playbackCtx;
    state.playbackCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: TARGET_SAMPLE_RATE,
    });
    state.playHead = state.playbackCtx.currentTime;
    return state.playbackCtx;
  }

  function enqueueAudio(int16) {
    const ctx = ensurePlaybackCtx();
    const float = pcm16ToFloat(int16);
    const buf = ctx.createBuffer(1, float.length, TARGET_SAMPLE_RATE);
    buf.copyToChannel(float, 0, 0);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    if (state.playHead < now) state.playHead = now;
    src.start(state.playHead);
    state.playHead += buf.duration;
    state.sources.push(src);
    src.onended = () => {
      const i = state.sources.indexOf(src);
      if (i >= 0) state.sources.splice(i, 1);
    };
    state.lastReplyAt = performance.now();
  }

  function flushPlayback() {
    state.sources.forEach((s) => {
      try { s.stop(); } catch (_) {}
    });
    state.sources = [];
    if (state.playbackCtx) {
      state.playHead = state.playbackCtx.currentTime;
    }
  }

  // ── Visualizer ───────────────────────────────────────────────────
  function startViz() {
    const ctx = viz.getContext("2d");
    function draw() {
      const w = (viz.width = viz.clientWidth * devicePixelRatio);
      const h = (viz.height = viz.clientHeight * devicePixelRatio);
      ctx.clearRect(0, 0, w, h);

      if (state.analyser) {
        state.analyser.getByteFrequencyData(state.vizData);
      }
      const data = state.vizData;
      const bars = 48;
      const cx = w / 2;
      const cy = h / 2;
      const baseR = Math.min(w, h) * 0.18;
      ctx.strokeStyle = "#2a2a2a";
      ctx.lineWidth = 1 * devicePixelRatio;
      ctx.beginPath();
      ctx.arc(cx, cy, baseR, 0, Math.PI * 2);
      ctx.stroke();

      ctx.strokeStyle = "#d4d0ca";
      ctx.lineWidth = 1.5 * devicePixelRatio;
      for (let i = 0; i < bars; i++) {
        const v = data[Math.floor((i / bars) * data.length)] / 255;
        const angle = (i / bars) * Math.PI * 2 - Math.PI / 2;
        const r1 = baseR + 4 * devicePixelRatio;
        const r2 = r1 + v * baseR * 0.9;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(angle) * r1, cy + Math.sin(angle) * r1);
        ctx.lineTo(cx + Math.cos(angle) * r2, cy + Math.sin(angle) * r2);
        ctx.stroke();
      }

      // Reticle
      ctx.fillStyle = "#6a6a68";
      ctx.font = `${10 * devicePixelRatio}px JetBrains Mono, monospace`;
      ctx.textAlign = "center";
      ctx.fillText("◉", cx, cy + 3 * devicePixelRatio);

      state.rafId = requestAnimationFrame(draw);
    }
    draw();
  }

  function stopViz() {
    if (state.rafId) cancelAnimationFrame(state.rafId);
    state.rafId = null;
  }

  // ── WebSocket ────────────────────────────────────────────────────
  function buildWsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let url = `${proto}//${location.host}/ws/voice`;
    const initData = tg && tg.initData ? tg.initData : "";
    if (initData) url += `?initData=${encodeURIComponent(initData)}`;
    return url;
  }

  function onUpstreamEvent(evt) {
    const t = evt.type;
    if (!t) return;
    switch (t) {
      case "ready":
        sessionEl.textContent = (evt.session || "—").slice(0, 8).toUpperCase();
        setStatus("live", "LIVE");
        break;

      case "error":
        showCaption(`[error] ${evt.error || JSON.stringify(evt)}`);
        setStatus("error", "ERROR");
        break;

      case "response.audio.delta":
        if (evt.delta) {
          const bytes = b64ToBytes(evt.delta);
          const int16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
          enqueueAudio(int16);
          if (state.lastSendAt) {
            const lat = Math.round(performance.now() - state.lastSendAt);
            latencyEl.textContent = `${lat}MS`;
          }
        }
        break;

      case "response.audio_transcript.delta":
        if (evt.delta) appendCaption(evt.delta);
        break;

      case "response.audio_transcript.done":
      case "response.done":
        // Reset caption shortly after final
        setTimeout(() => { resetCaption(); }, 4000);
        break;

      case "input_audio_buffer.speech_started":
        // Barge-in: stop any in-flight playback
        flushPlayback();
        try {
          state.ws && state.ws.send(JSON.stringify({ type: "response.cancel" }));
        } catch (_) {}
        break;

      case "conversation.item.input_audio_transcription.completed":
        if (evt.transcript) {
          // Briefly flash user transcript
          showCaption(`«${evt.transcript}»`);
          setTimeout(() => { if (captionEl.textContent.startsWith("«")) resetCaption(); }, 2500);
        }
        break;

      case "session.created":
      case "session.updated":
        // no-op
        break;

      default:
        // Useful for debugging:
        // console.debug("upstream event", t, evt);
        break;
    }
  }

  async function connect() {
    connectBtn.disabled = true;
    setStatus("connecting", "CONNECTING");
    resetCaption();

    try {
      await startMic();
    } catch (e) {
      setStatus("error", "MIC DENIED");
      showCaption("Microphone access denied.");
      connectBtn.disabled = false;
      return;
    }

    const ws = new WebSocket(buildWsUrl());
    ws.binaryType = "arraybuffer";
    state.ws = ws;

    ws.onopen = () => {
      setStatus("connecting", "HANDSHAKE");
    };

    ws.onmessage = (e) => {
      if (typeof e.data === "string") {
        try {
          const evt = JSON.parse(e.data);
          onUpstreamEvent(evt);
        } catch (_) {}
      }
    };

    ws.onerror = () => {
      setStatus("error", "ERROR");
    };

    ws.onclose = (e) => {
      teardown();
      if (e.code === 4401) {
        setStatus("error", "AUTH");
        showCaption("Authentication required. Open from Telegram.");
      } else if (e.code === 4403) {
        setStatus("error", "FORBIDDEN");
        showCaption("Your account is not authorized.");
      } else {
        setStatus("idle", "STANDBY");
      }
    };

    muteBtn.disabled = false;
    disconnectBtn.disabled = false;
    startViz();
  }

  function teardown() {
    stopMic();
    flushPlayback();
    stopViz();
    if (state.audioCtx) {
      try { state.audioCtx.close(); } catch (_) {}
      state.audioCtx = null;
    }
    if (state.playbackCtx) {
      try { state.playbackCtx.close(); } catch (_) {}
      state.playbackCtx = null;
    }
    state.ws = null;
    connectBtn.disabled = false;
    muteBtn.disabled = true;
    disconnectBtn.disabled = true;
    muteBtn.textContent = "◉ MUTE";
    state.muted = false;
  }

  function disconnect() {
    if (state.ws) {
      try { state.ws.close(1000); } catch (_) {}
    } else {
      teardown();
    }
  }

  function toggleMute() {
    state.muted = !state.muted;
    muteBtn.textContent = state.muted ? "◉ UNMUTE" : "◉ MUTE";
  }

  connectBtn.addEventListener("click", connect);
  disconnectBtn.addEventListener("click", disconnect);
  muteBtn.addEventListener("click", toggleMute);

  // Initial status
  setStatus("idle", "STANDBY");

  // Auto-show hint if outside Telegram
  if (!tg || !tg.initData) {
    showCaption("Open inside Telegram for verified session.\nDirect access allowed if no whitelist set.");
  } else {
    showCaption("Tap ENGAGE to open the voice link.");
  }
})();
