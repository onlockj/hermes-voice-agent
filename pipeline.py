"""Voice pipeline — fully free, low-latency.

Flow per user utterance:
  audio bytes (PCM16 16kHz mono)
    → Groq Whisper (STT, ~200-400ms)
    → Groq Llama 3.3 streamed tokens (LLM, ~150ms TTFT)
    → ElevenLabs Flash WS streamed audio (TTS, ~75ms TTFB)
       └─ tokens piped in as the LLM emits them
       └─ PCM24k frames fly back to the client
  Fallback TTS: Microsoft Edge TTS (free, unlimited, no key) — MP3 per sentence.

Audio frames are dispatched as { format: "pcm16_24k" | "mp3", bytes }
so the browser can decode each natively without server-side ffmpeg.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import wave
from typing import AsyncIterator, Awaitable, Callable, List, Optional

import httpx
import websockets

from config import settings

log = logging.getLogger("hermes.pipeline")

GROQ_BASE = "https://api.groq.com/openai/v1"
ELEVEN_WS = (
    "wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
    "?model_id={model}&output_format=pcm_24000&inactivity_timeout=20"
)

HERMES_SYSTEM_PROMPT = (
    "You are Hermes, the voice-enabled execution agent for LCKD. "
    "You speak with precision, speed, and tactical clarity. "
    "Your user is a solo operator building autonomous systems. "
    "Keep responses concise — usually one or two sentences. "
    "You can be interrupted at any moment. "
    "Acknowledge commands with 'Copy that' or 'Executing.' "
    "Never use markdown, lists, code blocks, or emoji — your output is spoken aloud."
)

AudioSink = Callable[[str, bytes], Awaitable[None]]   # (format, bytes)
EventSink = Callable[[dict], Awaitable[None]]


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def pcm16_to_wav(pcm: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw PCM16 mono bytes into a WAV container (for STT upload)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


# ────────────────────────────────────────────────────────────────────
# STT — Groq Whisper
# ────────────────────────────────────────────────────────────────────


async def transcribe(pcm: bytes, sample_rate: int = 16000) -> str:
    if len(pcm) < 1600:  # < 50ms — discard
        return ""
    wav_bytes = pcm16_to_wav(pcm, sample_rate)
    files = {"file": ("speech.wav", wav_bytes, "audio/wav")}
    data = {
        "model": settings.groq_stt_model,
        "response_format": "json",
        "temperature": "0",
        "language": "en",
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            f"{GROQ_BASE}/audio/transcriptions",
            files=files,
            data=data,
            headers=headers,
        )
        r.raise_for_status()
        return (r.json().get("text") or "").strip()


# ────────────────────────────────────────────────────────────────────
# LLM — Groq chat completions (streamed)
# ────────────────────────────────────────────────────────────────────


async def stream_llm(history: List[dict]) -> AsyncIterator[str]:
    """Yield content tokens as they arrive from Groq."""
    payload = {
        "model": settings.groq_llm_model,
        "messages": [{"role": "system", "content": HERMES_SYSTEM_PROMPT}, *history],
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 400,
    }
    headers = {
        "Authorization": f"Bearer {settings.groq_api_key}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream(
            "POST", f"{GROQ_BASE}/chat/completions", json=payload, headers=headers
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    obj = json.loads(data)
                    delta = obj["choices"][0]["delta"].get("content")
                    if delta:
                        yield delta
                except Exception:
                    continue


# ────────────────────────────────────────────────────────────────────
# TTS — ElevenLabs streaming WS (text-in, audio-out)
# ────────────────────────────────────────────────────────────────────


async def tts_elevenlabs(
    text_iter: AsyncIterator[str],
    audio_sink: AudioSink,
    cancel_event: asyncio.Event,
) -> str:
    """Pipe LLM tokens into ElevenLabs WS, push PCM24k frames to audio_sink."""
    url = ELEVEN_WS.format(
        voice_id=settings.elevenlabs_voice_id,
        model=settings.elevenlabs_model,
    )
    headers = {"xi-api-key": settings.elevenlabs_api_key}
    full_text_parts: List[str] = []

    async with websockets.connect(
        url, additional_headers=headers, max_size=16 * 1024 * 1024
    ) as ws:
        # Initial config frame
        await ws.send(
            json.dumps(
                {
                    "text": " ",
                    "voice_settings": {
                        "stability": 0.4,
                        "similarity_boost": 0.75,
                        "speed": 1.05,
                    },
                    "generation_config": {
                        "chunk_length_schedule": [50, 90, 120, 150]
                    },
                }
            )
        )

        async def pump_text() -> None:
            buf = ""
            async for tok in text_iter:
                if cancel_event.is_set():
                    break
                buf += tok
                full_text_parts.append(tok)
                # Flush at natural breakpoints to keep TTFB tight
                if any(c in tok for c in ".!?,;:\n") or len(buf) > 40:
                    await ws.send(
                        json.dumps({"text": buf, "try_trigger_generation": True})
                    )
                    buf = ""
            if buf:
                await ws.send(json.dumps({"text": buf}))
            await ws.send(json.dumps({"text": ""}))  # signal end

        async def pump_audio() -> None:
            async for raw in ws:
                if cancel_event.is_set():
                    break
                try:
                    msg = json.loads(raw) if isinstance(raw, str) else None
                except Exception:
                    msg = None
                if not msg:
                    continue
                if msg.get("audio"):
                    pcm = base64.b64decode(msg["audio"])
                    await audio_sink("pcm16_24k", pcm)
                if msg.get("isFinal"):
                    break

        await asyncio.gather(pump_text(), pump_audio())

    return "".join(full_text_parts).strip()


# ────────────────────────────────────────────────────────────────────
# TTS — Edge TTS fallback (free, unlimited) → MP3 per sentence
# ────────────────────────────────────────────────────────────────────


async def _synthesize_edge(sentence: str) -> bytes:
    """Synthesize one sentence with edge-tts, return MP3 bytes."""
    import edge_tts

    communicate = edge_tts.Communicate(sentence, settings.edge_tts_voice)
    mp3 = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            mp3.extend(chunk["data"])
    return bytes(mp3)


async def tts_edge(
    text_iter: AsyncIterator[str],
    audio_sink: AudioSink,
    cancel_event: asyncio.Event,
) -> str:
    """Accumulate sentences, synthesize each with edge-tts, send MP3 to client.

    Latency: ~400-700ms per sentence — acceptable for free fallback.
    Browser decodes MP3 natively, no server-side ffmpeg needed.
    """
    full_parts: List[str] = []
    buffer = ""

    async def flush_sentence(sentence: str) -> None:
        if not sentence.strip() or cancel_event.is_set():
            return
        try:
            mp3 = await _synthesize_edge(sentence)
            if mp3 and not cancel_event.is_set():
                await audio_sink("mp3", mp3)
        except Exception as e:
            log.warning("edge-tts synthesize failed: %s", e)

    async for tok in text_iter:
        if cancel_event.is_set():
            break
        buffer += tok
        full_parts.append(tok)
        # Flush at the earliest sentence boundary
        while True:
            idx = -1
            for ch in ".!?\n":
                j = buffer.find(ch)
                if j != -1 and (idx == -1 or j < idx):
                    idx = j
            if idx == -1 or idx < 4:
                break
            sentence, buffer = buffer[: idx + 1], buffer[idx + 1 :]
            await flush_sentence(sentence)

    if buffer.strip():
        await flush_sentence(buffer)

    return "".join(full_parts).strip()


# ────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────


async def run_turn(
    pcm_audio: bytes,
    history: List[dict],
    audio_sink: AudioSink,
    event_sink: EventSink,
    cancel_event: asyncio.Event,
) -> Optional[dict]:
    """One full STT → LLM → TTS turn. Mutates `history` in place."""
    # ── STT ────────────────────────────────────────────────────────
    try:
        await event_sink({"type": "stt.start"})
        user_text = await transcribe(pcm_audio)
    except Exception as e:
        log.warning("STT failed: %s", e)
        await event_sink({"type": "error", "stage": "stt", "message": str(e)})
        return None

    if not user_text or cancel_event.is_set():
        await event_sink({"type": "stt.empty"})
        return None

    await event_sink({"type": "stt.done", "text": user_text})
    history.append({"role": "user", "content": user_text})

    # ── LLM (stream) ────────────────────────────────────────────────
    await event_sink({"type": "llm.start"})

    token_queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    async def llm_to_queue() -> None:
        try:
            async for tok in stream_llm(history):
                if cancel_event.is_set():
                    break
                await token_queue.put(tok)
                await event_sink({"type": "llm.delta", "text": tok})
        except Exception as e:
            log.warning("LLM stream failed: %s", e)
            await event_sink({"type": "error", "stage": "llm", "message": str(e)})
        finally:
            await token_queue.put(None)

    async def queue_to_iter() -> AsyncIterator[str]:
        while True:
            tok = await token_queue.get()
            if tok is None:
                return
            yield tok

    llm_task = asyncio.create_task(llm_to_queue())

    # ── TTS (stream) ────────────────────────────────────────────────
    provider = settings.resolved_tts_provider()
    await event_sink({"type": "tts.start", "provider": provider})

    assistant_text = ""
    try:
        if provider == "elevenlabs" and settings.elevenlabs_api_key:
            assistant_text = await tts_elevenlabs(queue_to_iter(), audio_sink, cancel_event)
        else:
            assistant_text = await tts_edge(queue_to_iter(), audio_sink, cancel_event)
    except Exception as e:
        log.warning("TTS failed (%s): %s — falling back to edge", provider, e)
        await event_sink({"type": "tts.fallback", "from": provider})
        assistant_text = await tts_edge(queue_to_iter(), audio_sink, cancel_event)

    await llm_task

    if cancel_event.is_set():
        await event_sink({"type": "turn.cancelled"})
        return {"user_text": user_text, "assistant_text": assistant_text, "cancelled": True}

    if assistant_text:
        history.append({"role": "assistant", "content": assistant_text})

    await event_sink({"type": "turn.done", "assistant": assistant_text})
    return {"user_text": user_text, "assistant_text": assistant_text}
