"""Hermes Voice Agent — FastAPI + Telegram + OpenAI Realtime relay.

Single deployable unit:
- POST /webhook  → Telegram updates
- GET  /        → Voice PWA (static/index.html)
- WS   /ws/voice → Client mic ⇄ OpenAI Realtime API relay
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

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes

from config import settings
from sessions import SessionStore, VoiceSession, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("hermes")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"

HERMES_SYSTEM_PROMPT = (
    "You are Hermes, the voice-enabled execution agent for LCKD. "
    "You speak with precision, speed, and tactical clarity. "
    "Your user is a solo operator building autonomous systems. "
    "Keep responses concise. You can be interrupted. "
    "Acknowledge commands with 'Copy that' or 'Executing.'"
)

OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"

telegram_app: Optional[Application] = None


# ────────────────────────────────────────────────────────────────────
# Telegram bot
# ────────────────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if settings.allowed_user_ids and user.id not in settings.allowed_user_ids:
        await update.message.reply_text("Access denied.")
        return

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "▣ OPEN VOICE LINK",
                    web_app=WebAppInfo(url=settings.webapp_url),
                )
            ]
        ]
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
    if settings.allowed_user_ids and user.id not in settings.allowed_user_ids:
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
        .updater(None)  # webhook mode
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("voice", cmd_voice))
    return app


# ────────────────────────────────────────────────────────────────────
# Telegram WebApp initData verification
# ────────────────────────────────────────────────────────────────────


def verify_init_data(init_data: str, bot_token: str) -> Optional[dict]:
    """Validate Telegram WebApp initData per HMAC-SHA256 spec.

    Returns parsed payload (dict) if valid, else None.
    """
    if not init_data:
        return None
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{k}={parsed[k]}" for k in sorted(parsed.keys())
        )
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calc_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(calc_hash, received_hash):
            return None
        if "user" in parsed:
            try:
                parsed["user"] = json.loads(parsed["user"])
            except json.JSONDecodeError:
                pass
        return parsed
    except Exception as e:
        log.warning("initData verification error: %s", e)
        return None


# ────────────────────────────────────────────────────────────────────
# FastAPI lifespan
# ────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    global telegram_app
    telegram_app = build_telegram_app()
    await telegram_app.initialize()
    await telegram_app.start()

    # Register webhook
    webhook_url = f"{settings.webapp_url}/webhook"
    try:
        await telegram_app.bot.set_webhook(
            url=webhook_url,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=True,
        )
        log.info("Webhook set: %s", webhook_url)
    except Exception as e:
        log.warning("Failed to set webhook automatically: %s", e)

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
    return {"status": "ok", "sessions": store.count()}


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
# Voice relay WebSocket
# ────────────────────────────────────────────────────────────────────


async def _pump_upstream_to_client(session: VoiceSession) -> None:
    """Forward OpenAI Realtime events → client WebSocket."""
    try:
        async for raw in session.upstream_ws:
            if session.closed:
                break
            try:
                await session.client_ws.send_text(raw if isinstance(raw, str) else raw.decode())
            except Exception:
                break
    except websockets.ConnectionClosed:
        pass
    except Exception as e:
        log.warning("upstream→client pump error: %s", e)
    finally:
        try:
            await session.client_ws.close()
        except Exception:
            pass


async def _open_upstream(session: VoiceSession) -> None:
    url = OPENAI_REALTIME_URL.format(model=settings.model)
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "OpenAI-Beta": "realtime=v1",
    }
    session.upstream_ws = await websockets.connect(
        url,
        additional_headers=headers,
        max_size=16 * 1024 * 1024,
        ping_interval=20,
    )

    # Configure session
    session_update = {
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "instructions": HERMES_SYSTEM_PROMPT,
            "voice": settings.voice,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500,
            },
            "temperature": 0.7,
        },
    }
    await session.upstream_ws.send(json.dumps(session_update))
    session.upstream_task = asyncio.create_task(_pump_upstream_to_client(session))


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
            await websocket.send_text(
                json.dumps({"type": "error", "error": "invalid_init_data"})
            )
            await websocket.close(code=4401)
            return
        user = parsed.get("user")
        if isinstance(user, dict):
            user_id = user.get("id")
            if settings.allowed_user_ids and user_id not in settings.allowed_user_ids:
                await websocket.send_text(
                    json.dumps({"type": "error", "error": "forbidden"})
                )
                await websocket.close(code=4403)
                return
    else:
        if settings.allowed_user_ids:
            await websocket.send_text(
                json.dumps({"type": "error", "error": "auth_required"})
            )
            await websocket.close(code=4401)
            return

    session = VoiceSession(
        session_id=secrets.token_urlsafe(12),
        user_id=user_id,
        client_ws=websocket,
    )
    await store.add(session)
    log.info("session opened id=%s user=%s", session.session_id, user_id)

    try:
        await _open_upstream(session)
        await websocket.send_text(json.dumps({"type": "ready", "session": session.session_id}))

        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if "text" in msg and msg["text"] is not None:
                # Client control / event passthrough
                try:
                    data = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                # Forward known event types upstream
                kind = data.get("type")
                if kind in {
                    "input_audio_buffer.append",
                    "input_audio_buffer.commit",
                    "input_audio_buffer.clear",
                    "response.create",
                    "response.cancel",
                    "conversation.item.create",
                }:
                    await session.upstream_ws.send(json.dumps(data))
            elif "bytes" in msg and msg["bytes"] is not None:
                # Binary audio frame from client (raw pcm16). Wrap as buffer append.
                import base64

                evt = {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(msg["bytes"]).decode(),
                }
                await session.upstream_ws.send(json.dumps(evt))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("ws_voice error: %s", e)
    finally:
        session.closed = True
        if session.upstream_ws is not None:
            try:
                await session.upstream_ws.close()
            except Exception:
                pass
        if session.upstream_task is not None:
            session.upstream_task.cancel()
        await store.remove(session.session_id)
        log.info("session closed id=%s", session.session_id)


# ────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )
