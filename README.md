# ▣ HERMES — VOICE LINK

Live voice channel for the Hermes Telegram agent. One Python process, one Telegram bot, one WebSocket relay to the OpenAI Realtime API, and a brutalist PWA front-end you open as a Telegram Mini App.

```
Telegram → Web App button → PWA in browser
   ↑                              ↓ mic (PCM16 @ 24kHz)
   └── voice replies ←── OpenAI Realtime ←── FastAPI relay
```

## Stack

- FastAPI + Uvicorn (async, WebSocket native)
- `python-telegram-bot` v21 (webhook mode)
- OpenAI Realtime API (voice in / voice out)
- Vanilla JS front-end (Web Audio API, AudioContext, no build step)
- Telegram WebApp `initData` HMAC-SHA256 verification

## Quick Start (Local)

Requires Python 3.11+, a public HTTPS URL (use `ngrok`/`cloudflared`/Tailscale Funnel), a Telegram bot from `@BotFather`, and an OpenAI key with Realtime access.

```bash
cd hermes-voice-agent
cp .env.example .env
# Fill TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, WEBAPP_URL (your public https URL)

./deploy.sh local
```

`./deploy.sh local` will:
1. Create `.venv`, install deps
2. Register the Telegram webhook → `${WEBAPP_URL}/webhook`
3. Run `uvicorn main:app` on `${PORT:-8080}`

Then:
- Open Telegram → your bot → `/start` → **OPEN VOICE LINK**
- Tap **ENGAGE** → speak → Hermes responds in real time
- Interrupt freely; server-side VAD detects barge-in and cancels the in-flight reply

## Deploy

| Target | Command | Notes |
|---|---|---|
| Railway | `./deploy.sh railway` | uses `railway.toml`; needs `railway login` |
| Render | `./deploy.sh render` | uses `render.yaml` Blueprint flow |
| Fly.io | `./deploy.sh fly` | uses `fly.toml`; needs `fly auth login` |
| Docker | `./deploy.sh docker` | builds image, prints run command |
| Heroku-style | `Procfile` included | works with any PaaS that respects Procfiles |

After any cloud deploy, re-register the webhook against the public URL:

```bash
WEBAPP_URL=https://your-domain.example ./deploy.sh webhook
```

## Configuration

All via `.env` (see `.env.example`):

| Var | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✓ | — | BotFather token |
| `OPENAI_API_KEY` | ✓ | — | Realtime API access |
| `WEBAPP_URL` | ✓ | — | Public HTTPS root of this app |
| `ALLOWED_USER_IDS` | — | empty | CSV of Telegram user IDs; empty = open |
| `MODEL` | — | `gpt-4o-realtime-preview-2024-10-01` | Realtime model |
| `VOICE` | — | `ash` | `alloy`/`echo`/`shimmer`/`ash`/`ballad`/`coral`/`sage`/`verse` |
| `HOST` / `PORT` | — | `0.0.0.0` / `8080` | bind |

The Hermes system prompt is hardcoded in `main.py` (`HERMES_SYSTEM_PROMPT`) — edit there.

## Architecture Detail

### Endpoints
- `GET /` — serves the PWA shell
- `POST /webhook` — Telegram updates
- `WS /ws/voice?initData=…` — voice relay
- `GET /health` — JSON `{status, sessions}`

### WebSocket Relay (`/ws/voice`)
1. Client connects with `initData` query param (Telegram Mini App `Telegram.WebApp.initData`).
2. Server verifies HMAC-SHA256 against bot token (`verify_init_data`). Rejects 4401 if bad; 4403 if user not whitelisted.
3. Server opens upstream to `wss://api.openai.com/v1/realtime?model=…` with `OpenAI-Beta: realtime=v1`.
4. Server sends `session.update` injecting the Hermes prompt, server VAD config, PCM16 24 kHz formats, and Whisper transcription.
5. **Client → server**: raw PCM16 binary frames (auto-wrapped server-side into `input_audio_buffer.append`), plus JSON control events (`response.cancel`, etc.).
6. **Server → client**: all upstream JSON events forwarded verbatim (`response.audio.delta`, transcripts, VAD events, errors).

### Audio Pipeline (Browser)
- `getUserMedia` → `AudioContext` (browser-native rate, usually 48 kHz)
- ScriptProcessor → linear downsample to 24 kHz → `Float32` → `Int16` PCM
- WS binary send (~40 ms frames)
- Replies: base64 → `Int16Array` → `AudioBuffer` @ 24 kHz, scheduled in order on a single playback graph
- Barge-in: when upstream emits `input_audio_buffer.speech_started`, client flushes the playback queue and sends `response.cancel`

## Security Notes

- `initData` is verified server-side with HMAC-SHA256 (`WebAppData` + bot token). Never trust client user IDs.
- Webhook endpoint is not auth-protected by default (Telegram signs nothing in `POST /webhook` — relies on URL secrecy). For tightening, add `setWebhook` with `secret_token` and check the `X-Telegram-Bot-Api-Secret-Token` header.
- `ALLOWED_USER_IDS` enforces a whitelist on both Telegram commands and WebSocket sessions.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `AUTH` red dot | Open from Telegram (so `initData` is present) — or open in a browser if `ALLOWED_USER_IDS` is empty |
| No audio in | Browser blocked mic; check site permissions |
| No audio out | iOS Safari requires a user gesture before `AudioContext.resume()` — the ENGAGE click is that gesture |
| `invalid_init_data` | Bot token in `.env` doesn't match the bot whose Mini App you opened |
| Realtime API 401 | OpenAI key lacks Realtime access — verify on the OpenAI dashboard |
| Latency spikes | Network. The relay itself adds ~1ms; jitter is upstream + client codec |

## Files

```
hermes-voice-agent/
├── main.py            FastAPI app, Telegram webhook, WebSocket relay
├── config.py          Pydantic settings
├── sessions.py        In-memory session store
├── requirements.txt
├── .env.example
├── deploy.sh          one-shot installer / deployer
├── Dockerfile
├── Procfile
├── railway.toml
├── render.yaml
├── fly.toml
└── static/
    ├── index.html
    ├── app.js
    ├── style.css
    ├── manifest.json
    └── icon.png
```

## Brand

LCKD private stack. Monochrome only. Square corners. JetBrains Mono labels, Inter body. Film grain ambient. Don't put orange in this one — pure void.

```
▣ END TRANSMISSION
```
