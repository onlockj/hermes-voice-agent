// HERMES VOICE LINK — client (free streaming stack)
//
// Pipeline (per utterance):
//   mic 48k → resample 16k mono PCM16 → stream binary frames to /ws/voice
//      ↳ client-side VAD detects silence → send {type:"utterance.end"}
//   server STT → LLM → TTS → audio frames stream back
//      ↳ format: "pcm16_24k" (ElevenLabs)  → Int16 → AudioBuffer
//      ↳ format: "mp3"      (Edge TTS)    → decodeAudioData
//
// Barge-in: if VAD detects speech while playing, flush playback + send {type:"barge_in"}

(() => {
  const INPUT_SR = 16000;
  const OUTPUT_SR = 24000;
  const SILENCE_MS_TO_END = 700;   // silence required to commit utterance
  const SPEECH_RMS_THRESHOLD = 0.015;
  const SPEECH_FRAMES_TO_START = 3;

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

  const tg = window.Telegram?.WebApp || null;
  if (tg) {
    try {
      tg.ready(); tg.expand();
      tg.setHeaderColor?.("#0a0a0a");
      tg.setBackgroundColor?.("#0a0a0a");
    } catch (_) {}
  }

  // ── State ─────────────────────────────────────────────────────
  const state = {
    ws: null,
    audioCtx: null,
    micStream: null,
    micNode: null,
    procNode: null,
    analyser: null,
    muted: false,
    // VAD
    vadSpeechFrames: 0,
    vadSilenceMs: 0,
    inUtterance: false,
    // Playback
    playbackCtx: null,
    playHead: 0,
    sources: [],
    isPlaying: false,
    // Meta
    pendingAudioMeta: null,
    captionBuf: "",
    lastSendAt: 0,
    rafId: null,
    vizData: new Uint8Array(64),
    // Pending audio (waiting for binary frame)
    awaitingFormat: null,
  };

  // ── UI ─────────────────────────────────────────────────────────
  function setStatus(kind, text) {
    statusDot.className = "dot dot--" + kind;
    statusText.textContent = text;
  }
  function showCaption(text) { captionEl.textContent = text; }
  function appendAssistant(delta) {
    state.captionBuf += delta;
    captionEl.textContent = state.captionBuf;
  }
  function resetCaption() { state.captionBuf = ""; captionEl.textContent = ""; }

  // ── DSP ────────────────────────────────────────────────────────
  function resampleFloat32(input, srcRate, dstRate) {
    if (srcRate === dstRate) return input;
    const ratio = srcRate / dstRate;
    const out = new Float32Array(Math.floor(input.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const s = i * ratio, i0 = Math.floor(s), i1 = Math.min(i0 + 1, input.length - 1);
      const t = s - i0;
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
    for (let i = 0; i < int16.length; i++) out[i] = int16[i] / 0x8000;
    return out;
  }
  function rms(f32) {
    let s = 0;
    for (let i = 0; i < f32.length; i++) s += f32[i] * f32[i];
    return Math.sqrt(s / f32.length);
  }

  // ── Mic + VAD ─────────────────────────────────────────────────
  async function startMic() {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    state.micStream = stream;
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    state.audioCtx = ctx;
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 128;
    state.analyser = analyser;
    source.connect(analyser);

    const proc = ctx.createScriptProcessor(2048, 1, 1);
    proc.onaudioprocess = (ev) => {
      if (state.muted || !state.ws || state.ws.readyState !== WebSocket.OPEN) return;
      const inBuf = ev.inputBuffer.getChannelData(0);
      const resampled = resampleFloat32(inBuf, ctx.sampleRate, INPUT_SR);
      const energy = rms(resampled);

      // VAD state machine
      if (energy > SPEECH_RMS_THRESHOLD) {
        state.vadSpeechFrames++;
        state.vadSilenceMs = 0;
        if (!state.inUtterance && state.vadSpeechFrames >= SPEECH_FRAMES_TO_START) {
          state.inUtterance = true;
          // Barge-in if currently playing
          if (state.isPlaying) {
            flushPlayback();
            try { state.ws.send(JSON.stringify({ type: "barge_in" })); } catch (_) {}
            setStatus("live", "LISTENING");
          } else {
            setStatus("live", "LISTENING");
          }
        }
      } else {
        state.vadSpeechFrames = 0;
        if (state.inUtterance) {
          state.vadSilenceMs += (resampled.length / INPUT_SR) * 1000;
          if (state.vadSilenceMs > SILENCE_MS_TO_END) {
            // End of utterance
            state.inUtterance = false;
            state.vadSilenceMs = 0;
            try { state.ws.send(JSON.stringify({ type: "utterance.end" })); } catch (_) {}
            state.lastSendAt = performance.now();
            setStatus("connecting", "THINKING");
          }
        }
      }

      // Stream audio frames during utterance (always, server buffers; trims when needed)
      if (state.inUtterance) {
        const pcm = floatToPCM16(resampled);
        try { state.ws.send(pcm.buffer); } catch (_) {}
      }
    };
    source.connect(proc);
    proc.connect(ctx.destination);
    state.procNode = proc;
    state.micNode = source;
  }

  function stopMic() {
    try { state.procNode?.disconnect(); } catch (_) {}
    try { state.micNode?.disconnect(); } catch (_) {}
    try { state.analyser?.disconnect(); } catch (_) {}
    if (state.micStream) state.micStream.getTracks().forEach((t) => t.stop());
    state.micStream = null; state.procNode = null; state.micNode = null;
  }

  // ── Playback ──────────────────────────────────────────────────
  function ensurePlaybackCtx() {
    if (state.playbackCtx) return state.playbackCtx;
    state.playbackCtx = new (window.AudioContext || window.webkitAudioContext)();
    state.playHead = state.playbackCtx.currentTime;
    return state.playbackCtx;
  }

  function schedulePlay(buffer) {
    const ctx = ensurePlaybackCtx();
    const src = ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(ctx.destination);
    const now = ctx.currentTime;
    if (state.playHead < now) state.playHead = now;
    src.start(state.playHead);
    state.playHead += buffer.duration;
    state.sources.push(src);
    state.isPlaying = true;
    src.onended = () => {
      const i = state.sources.indexOf(src);
      if (i >= 0) state.sources.splice(i, 1);
      if (state.sources.length === 0) {
        state.isPlaying = false;
        setStatus("live", "LIVE");
      }
    };
  }

  async function enqueueAudio(format, bytes) {
    const ctx = ensurePlaybackCtx();
    if (state.lastSendAt) {
      const lat = Math.round(performance.now() - state.lastSendAt);
      latencyEl.textContent = `${lat}MS`;
      state.lastSendAt = 0;
    }
    if (format === "pcm16_24k") {
      const int16 = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength / 2);
      const f32 = pcm16ToFloat(int16);
      const buf = ctx.createBuffer(1, f32.length, OUTPUT_SR);
      buf.copyToChannel(f32, 0, 0);
      schedulePlay(buf);
    } else if (format === "mp3") {
      try {
        const buf = await ctx.decodeAudioData(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
        schedulePlay(buf);
      } catch (e) {
        console.warn("mp3 decode failed", e);
      }
    } else {
      console.warn("unknown audio format", format);
    }
    setStatus("live", "SPEAKING");
  }

  function flushPlayback() {
    state.sources.forEach((s) => { try { s.stop(); } catch (_) {} });
    state.sources = [];
    state.isPlaying = false;
    if (state.playbackCtx) state.playHead = state.playbackCtx.currentTime;
  }

  // ── Viz ───────────────────────────────────────────────────────
  function startViz() {
    const ctx = viz.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    function draw() {
      const w = (viz.width = viz.clientWidth * dpr);
      const h = (viz.height = viz.clientHeight * dpr);
      ctx.clearRect(0, 0, w, h);
      if (state.analyser) state.analyser.getByteFrequencyData(state.vizData);
      const data = state.vizData;
      const bars = 48;
      const cx = w / 2, cy = h / 2;
      const baseR = Math.min(w, h) * 0.18;
      ctx.strokeStyle = "#2a2a2a";
      ctx.lineWidth = 1 * dpr;
      ctx.beginPath(); ctx.arc(cx, cy, baseR, 0, Math.PI * 2); ctx.stroke();
      ctx.strokeStyle = state.isPlaying ? "#e6e4e0" : "#d4d0ca";
      ctx.lineWidth = 1.5 * dpr;
      for (let i = 0; i < bars; i++) {
        const v = data[Math.floor((i / bars) * data.length)] / 255;
        const angle = (i / bars) * Math.PI * 2 - Math.PI / 2;
        const r1 = baseR + 4 * dpr;
        const r2 = r1 + v * baseR * 0.9;
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(angle) * r1, cy + Math.sin(angle) * r1);
        ctx.lineTo(cx + Math.cos(angle) * r2, cy + Math.sin(angle) * r2);
        ctx.stroke();
      }
      ctx.fillStyle = "#6a6a68";
      ctx.font = `${10 * dpr}px JetBrains Mono, monospace`;
      ctx.textAlign = "center";
      ctx.fillText(state.inUtterance ? "◉" : "▣", cx, cy + 3 * dpr);
      state.rafId = requestAnimationFrame(draw);
    }
    draw();
  }
  function stopViz() {
    if (state.rafId) cancelAnimationFrame(state.rafId);
    state.rafId = null;
  }

  // ── WebSocket ─────────────────────────────────────────────────
  function buildWsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    let url = `${proto}//${location.host}/ws/voice`;
    const initData = tg?.initData || "";
    if (initData) url += `?initData=${encodeURIComponent(initData)}`;
    return url;
  }

  function onJsonEvent(evt) {
    switch (evt.type) {
      case "ready":
        sessionEl.textContent = (evt.session || "—").slice(0, 8).toUpperCase();
        setStatus("live", "LIVE");
        showCaption("Speak whenever you're ready.");
        break;
      case "audio":
        state.awaitingFormat = evt.format;
        break;
      case "stt.done":
        showCaption(`«${evt.text}»`);
        resetCaption();
        setTimeout(() => { if (state.captionBuf === "") captionEl.textContent = ""; }, 1500);
        break;
      case "llm.start":
        resetCaption();
        break;
      case "llm.delta":
        appendAssistant(evt.text || "");
        break;
      case "turn.done":
        setTimeout(() => {
          if (!state.isPlaying && !state.inUtterance) {
            resetCaption();
            setStatus("live", "LIVE");
          }
        }, 3000);
        break;
      case "turn.cancelled":
        flushPlayback();
        break;
      case "error":
        showCaption(`[error:${evt.stage || "?"}] ${evt.message || evt.error || ""}`);
        setStatus("error", "ERROR");
        break;
      case "tts.fallback":
        console.warn("TTS fell back from", evt.from);
        break;
      default:
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

    ws.onopen = () => setStatus("connecting", "HANDSHAKE");
    ws.onmessage = async (e) => {
      if (typeof e.data === "string") {
        try { onJsonEvent(JSON.parse(e.data)); } catch (_) {}
      } else {
        const fmt = state.awaitingFormat;
        state.awaitingFormat = null;
        if (!fmt) return;
        await enqueueAudio(fmt, new Uint8Array(e.data));
      }
    };
    ws.onerror = () => setStatus("error", "ERROR");
    ws.onclose = (e) => {
      teardown();
      if (e.code === 4401) { setStatus("error", "AUTH"); showCaption("Open from Telegram."); }
      else if (e.code === 4403) { setStatus("error", "FORBIDDEN"); showCaption("Not authorized."); }
      else { setStatus("idle", "STANDBY"); }
    };

    muteBtn.disabled = false;
    disconnectBtn.disabled = false;
    startViz();
  }

  function teardown() {
    stopMic();
    flushPlayback();
    stopViz();
    try { state.audioCtx?.close(); } catch (_) {} state.audioCtx = null;
    try { state.playbackCtx?.close(); } catch (_) {} state.playbackCtx = null;
    state.ws = null;
    state.inUtterance = false;
    connectBtn.disabled = false;
    muteBtn.disabled = true;
    disconnectBtn.disabled = true;
    muteBtn.textContent = "◉ MUTE";
    state.muted = false;
  }

  function disconnect() {
    if (state.ws) { try { state.ws.close(1000); } catch (_) {} } else { teardown(); }
  }

  function toggleMute() {
    state.muted = !state.muted;
    muteBtn.textContent = state.muted ? "◉ UNMUTE" : "◉ MUTE";
  }

  connectBtn.addEventListener("click", connect);
  disconnectBtn.addEventListener("click", disconnect);
  muteBtn.addEventListener("click", toggleMute);

  setStatus("idle", "STANDBY");
  if (!tg || !tg.initData) {
    showCaption("Open inside Telegram for verified session.\nDirect access allowed if no whitelist set.");
  } else {
    showCaption("Tap ENGAGE to open the voice link.");
  }
})();
