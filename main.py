"""Hermes Voice Agent — FastAPI + Telegram + free streaming pipeline.

Endpoints:
  POST /webhook       Telegram updates
  GET  /              Voice PWA (static/index.html)
  WS   /ws/voice      Client mic ⇄ Groq STT/LLM ⇄ ElevenLabs/Edge TTS
  GET  /health        liveness
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from config import settings
from pipeline import run_turn
from sessions import VoiceSession, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("hermes")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

telegram_app: Optional[Application] = None


# ────────────────────────────────────────────────────────────────────
# Telegram bot
# ────────────────────────────────────────────────────────────────────


def _allowed(user_id: int) -> bool:
    return not settings.allowed_user_ids or user_id in settings.allowed_user_ids


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _allowed(user.id):
        await update.message.reply_text("Access denied.")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("▣ OPEN VOICE LINK", web_app=WebAppInfo(url=settings.webapp_url))]]
    )
    await update.message.reply_text(
        "HERMES VOICE LINK\n"
        "──────────────────\n"
        "Tap to open the live voice channel.\n"
        "Speak naturally. Interrupt freely.",
        reply_markup=kb,
    )


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not _allowed(user.id):
        await update.message.reply_text("Access denied.")
        return
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("▣ VOICE LINK", web_app=WebAppInfo(url=settings.webapp_url))]]
    )
    await update.message.reply_text("Live voice channel:", reply_markup=kb)


def build_telegram_app() -> Application:
    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .updater(None)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("voice", cmd_voice))
    return app


# ────────────────────────────────────────────────────────────────────
# Telegram WebApp initData verification
# ────────────────────────────────────────────────────────────────────


def verify_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    if not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{k}={parsed[k]}" for k in sorted(parsed.keys()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        if "user" in parsed:
            try:
                parsed["user"] = json.loads(parsed["user"])
            except json.JSONDecodeError:
                pass
        return parsed
    except Exception as e:
        log.warning("initData verify error: %s", e)
        return None


# ────────────────────────────────────────────────────────────────────
# Lifespan
# ────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    telegram_app = build_telegram_app()
    await telegram_app.initialize()
    await telegram_app.start()
    webhook_url = f"{settings.webapp_url}/webhook"
    try:
        await telegram_app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        log.info("Webhook set: %s", webhook_url)
    except Exception as e:
        log.warning("Failed to set webhook: %s", e)
    yield
    try:
        await telegram_app.stop()
        await telegram_app.shutdown()
    except Exception as e:
        log.warning("Telegram shutdown error: %s", e)


app = FastAPI(title="Hermes Voice Agent", lifespan=lifespan)


# ────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sessions": store.count(),
        "tts_provider": settings.resolved_tts_provider(),
        "llm": settings.groq_llm_model,
        "stt": settings.groq_stt_model,
    }


@app.post("/webhook")
async def telegram_webhook(request: Request):
    if telegram_app is None:
        raise HTTPException(503, "Bot not initialized")
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ────────────────────────────────────────────────────────────────────
# Voice WebSocket
# ────────────────────────────────────────────────────────────────────


async def _send_audio(session: VoiceSession, fmt: str, data: bytes) -> None:
    if session.closed:
        return
    try:
        await session.client_ws.send_json({"type": "audio", "format": fmt, "bytes": len(data)})
        await session.client_ws.send_bytes(data)
    except Exception as e:
        log.debug("audio send failed: %s", e)


async def _send_event(session: VoiceSession, evt: dict) -> None:
    if session.closed:
        return
    try:
        await session.client_ws.send_json(evt)
    except Exception as e:
        log.debug("event send failed: %s", e)


async def _start_turn(session: VoiceSession) -> None:
    """Kick off a STT→LLM→TTS pipeline turn for the buffered audio."""
    if session.turn_task and not session.turn_task.done():
        return  # already running
    audio = bytes(session.capture_buf)
    session.capture_buf.clear()
    if not audio:
        return
    session.cancel_event = asyncio.Event()

    async def runner():
        try:
            await run_turn(
                pcm_audio=audio,
                history=session.history,
                audio_sink=lambda fmt, data: _send_audio(session, fmt, data),
                event_sink=lambda evt: _send_event(session, evt),
                cancel_event=session.cancel_event,
            )
            # Keep last 16 turns
            if len(session.history) > 32:
                session.history[:] = session.history[-32:]
        except Exception as e:
            log.warning("turn error: %s", e)
            await _send_event(session, {"type": "error", "stage": "pipeline", "message": str(e)})

    session.turn_task = asyncio.create_task(runner())


async def _cancel_turn(session: VoiceSession) -> None:
    session.cancel_event.set()
    if session.turn_task and not session.turn_task.done():
        try:
            await asyncio.wait_for(session.turn_task, timeout=2.0)
        except asyncio.TimeoutError:
            session.turn_task.cancel()


@app.websocket("/ws/voice")
async def ws_voice(
    websocket: WebSocket,
    init_data: Optional[str] = Query(default=None, alias="initData"),
):
    await websocket.accept()

    user_id: Optional[int] = None
    if init_data:
        parsed = verify_init_data(init_data, settings.telegram_bot_token)
        if parsed is None:
            await websocket.send_json({"type": "error", "error": "invalid_init_data"})
            await websocket.close(code=4401)
            return
        user = parsed.get("user")
        if isinstance(user, dict):
            user_id = user.get("id")
            if not _allowed(user_id):
                await websocket.send_json({"type": "error", "error": "forbidden"})
                await websocket.close(code=4403)
                return
    elif settings.allowed_user_ids:
        await websocket.send_json({"type": "error", "error": "auth_required"})
        await websocket.close(code=4401)
        return

    session = VoiceSession(
        session_id=secrets.token_urlsafe(12),
        user_id=user_id,
        client_ws=websocket,
    )
    await store.add(session)
    log.info("session open id=%s user=%s", session.session_id, user_id)

    await websocket.send_json(
        {
            "type": "ready",
            "session": session.session_id,
            "tts_provider": settings.resolved_tts_provider(),
            "audio_in_sr": 16000,
            "audio_out_sr": 24000,
        }
    )

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            if msg.get("bytes") is not None:
                # Raw PCM16 16kHz mono frame from client
                session.capture_buf.extend(msg["bytes"])
                continue

            if msg.get("text") is None:
                continue

            try:
                data = json.loads(msg["text"])
            except json.JSONDecodeError:
                continue

            kind = data.get("type")
            if kind == "utterance.end":
                await _start_turn(session)
            elif kind == "barge_in" or kind == "cancel":
                await _cancel_turn(session)
                session.capture_buf.clear()
            elif kind == "reset":
                await _cancel_turn(session)
                session.history.clear()
                session.capture_buf.clear()
                await _send_event(session, {"type": "reset.done"})
            elif kind == "ping":
                await _send_event(session, {"type": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws_voice error: %s", e)
    finally:
        session.closed = True
        await _cancel_turn(session)
        await store.remove(session.session_id)
        log.info("session closed id=%s", session.session_id)


# ────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host=settings.host, port=settings.port, log_level="info")
