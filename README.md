---
title: Hermes Voice Agent
emoji: "◼"
colorFrom: gray
colorTo: gray
sdk: docker
app_port: 8080
pinned: false
short_description: Live voice channel for the Hermes Telegram agent
---

# ▣ HERMES — VOICE LINK

Live voice channel for the Hermes Telegram agent. **Runs for $0** on the free tier of every service in the chain.

```
Telegram → Web App button → PWA in browser
   ↑                              ↓ mic (PCM16 16k mono, client VAD)
   └── voice replies ←── ElevenLabs Flash WS  ←┐
                       (or Edge TTS, free fallback)
                                               │
                          Groq Llama-3.3 70B ──┘  (streamed token-by-token)
                                ↑
                          Groq Whisper-Large-v3-Turbo
```

**End-to-end latency:** typically **600–900 ms** from when you stop talking until first audio plays — because the LLM streams tokens straight into the TTS WebSocket and audio starts before the LLM has finished thinking.

## Free Stack

| Layer | Service | Free allowance | Why |
|---|---|---|---|
| **STT** | Groq Whisper-Large-v3-Turbo | Generous free tier | Fastest Whisper available, OpenAI-compatible |
| **LLM** | Groq Llama-3.3-70B-Versatile | Free, ~500 tok/s | Feels real-time, streams |
| **TTS** | ElevenLabs Flash v2.5 (WS) | 10k chars/mo free | ~75ms TTFB, premium voice |
| **TTS fallback** | Microsoft Edge TTS | **Unlimited, no key** | Free forever — auto-engaged when ElevenLabs key is empty or quota burned |
| **Hosting** | Hugging Face Spaces (Docker) | Free, persistent, WS-capable | No cold starts on most days |
| **Telegram bot** | Telegram Bot API | Free | unchanged |

Total at-rest cost: **$0**.

## One-Click Free Deploy → Hugging Face Spaces

1. **Get keys:**
   - Telegram bot from [@BotFather](https://t.me/botfather) → `TELEGRAM_BOT_TOKEN`
   - Free Groq key → [console.groq.com/keys](https://console.groq.com/keys)
   - (Optional) Free ElevenLabs key → [elevenlabs.io](https://elevenlabs.io). Skip this and Edge TTS engages automatically.

2. **Create the Space:**
   - [huggingface.co/new-space](https://huggingface.co/new-space) → SDK: **Docker** → Hardware: **CPU basic (free)**
   - Push this repo to it, or use **Duplicate Space** from your fork

3. **Set Space secrets** (Settings → Variables and secrets):
   ```
   TELEGRAM_BOT_TOKEN=...
   GROQ_API_KEY=...
   WEBAPP_URL=https://<username>-hermes-voice-agent.hf.space
   ELEVENLABS_API_KEY=...          # optional
   ELEVENLABS_VOICE_ID=...         # optional, default Adam
   ALLOWED_USER_IDS=...            # optional CSV
   ```

4. **Open Telegram → your bot → `/start` → OPEN VOICE LINK → ENGAGE.** Talk. Hermes talks back.

The webhook auto-registers on startup against `WEBAPP_URL`.

## Local Dev

Need a public HTTPS URL (use `cloudflared`, `ngrok`, or Tailscale Funnel) because Telegram WebApps require HTTPS.

```bash
cd hermes-voice-agent
cp .env.example .env       # fill TELEGRAM_BOT_TOKEN, GROQ_API_KEY, WEBAPP_URL
./deploy.sh local
```

`./deploy.sh local` will: create `.venv`, install deps, register the webhook, run uvicorn on `${PORT:-8080}`.

## Other Free / Cheap Hosts

| Target | Command | Notes |
|---|---|---|
| Hugging Face Spaces | push the repo | **Recommended free path.** See above. |
| Fly.io | `./deploy.sh fly` | Hobby plan, small VMs |
| Render | `./deploy.sh render` | Free tier sleeps after inactivity (cold start hits latency) |
| Docker | `./deploy.sh docker` | builds local image; deploy anywhere |

After any deploy, re-register the webhook if you change the URL:

```bash
WEBAPP_URL=https://… ./deploy.sh webhook
```

## Configuration

All via `.env` (or Space secrets). See [`.env.example`](.env.example).

| Var | Required | Default | Notes |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✓ | — | BotFather |
| `GROQ_API_KEY` | ✓ | — | STT + LLM (free) |
| `WEBAPP_URL` | ✓ | — | Public HTTPS root |
| `ELEVENLABS_API_KEY` | — | empty | If empty, Edge TTS is used |
| `ELEVENLABS_VOICE_ID` | — | `pNInz6obpgDQGcFmaJgB` (Adam) | Any ElevenLabs voice ID |
| `ELEVENLABS_MODEL` | — | `eleven_flash_v2_5` | Lowest-latency model |
| `TTS_PROVIDER` | — | `auto` | `auto` \| `elevenlabs` \| `edge` |
| `EDGE_TTS_VOICE` | — | `en-US-GuyNeural` | Any Edge neural voice |
| `GROQ_STT_MODEL` | — | `whisper-large-v3-turbo` | |
| `GROQ_LLM_MODEL` | — | `llama-3.3-70b-versatile` | |
| `ALLOWED_USER_IDS` | — | empty | CSV of Telegram user IDs |
| `HOST` / `PORT` | — | `0.0.0.0` / `8080` | |

The Hermes system prompt is in `pipeline.py` → `HERMES_SYSTEM_PROMPT`. Edit there.

## Architecture

```
client (PWA)                            server (FastAPI)
─────────────                            ────────────────
mic → resample 16k → PCM16 ──binary──▶  capture_buf (per session)
client VAD detects silence
                       ──JSON──▶  {type:"utterance.end"}
                                        ↓
                                  Groq Whisper (POST /audio/transcriptions)
                                        ↓
                                  Groq chat completions (stream)
                                        ↓ token-by-token
                                  ┌──────────────────────────┐
                                  │ ElevenLabs Flash WS      │
                                  │ (text-in / audio-out)    │
                                  └──────────────────────────┘
                                        ↓ PCM24k chunks
client receives ◀──binary── audio frames
  ↳ Int16 → AudioBuffer → schedule on AudioContext
  ↳ playback head advances per-chunk → seamless stream

barge-in:
  VAD speech-start during playback
    → flush playback queue
    → send {type:"barge_in"}
    → server cancels in-flight turn
```

### Why this beats a "STT → wait → LLM → wait → TTS" loop

The conventional chain waits for each stage to finish:
- `STT_time + LLM_time + TTS_time ≈ 300 + 1500 + 500 = 2.3s`

This stack overlaps LLM and TTS:
- `STT_time + LLM_TTFT + TTS_TTFB ≈ 300 + 150 + 75 = 525ms`

The LLM keeps emitting tokens *while* ElevenLabs is already speaking the first sentence. That's the whole trick.

## Security

- Telegram `initData` is HMAC-SHA256 verified server-side (`verify_init_data` in [main.py](main.py)). Never trust client user IDs.
- `ALLOWED_USER_IDS` whitelist enforced on both Telegram commands and WS sessions.
- Webhook endpoint URL is the only secret protecting `/webhook` by default — tighten with `setWebhook secret_token` if needed.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `AUTH` red dot | Open from Telegram (initData) — or leave `ALLOWED_USER_IDS` empty for open access |
| Mic permission denied | Browser site permissions |
| No audio out | First click (ENGAGE) is the required gesture for AudioContext |
| Long pauses before reply | Check Groq API status; STT can be the bottleneck on cold connections |
| Robotic / clipped audio | Edge TTS sometimes returns short clips; usually self-resolves on next turn |
| ElevenLabs quota exhausted | Set `TTS_PROVIDER=edge` (or just unset `ELEVENLABS_API_KEY`) to switch fully free |

## Files

```
hermes-voice-agent/
├── main.py            FastAPI app, Telegram webhook, WS handler
├── pipeline.py        Groq STT/LLM + ElevenLabs WS + Edge TTS fallback
├── config.py          Pydantic settings
├── sessions.py        Per-WS session state (history, capture_buf, cancel)
├── requirements.txt
├── .env.example
├── deploy.sh          one-shot installer / deployer
├── Dockerfile         used by HF Spaces / Render / Fly
├── Procfile
├── railway.toml
├── render.yaml
├── fly.toml
└── static/
    ├── index.html
    ├── app.js         client VAD, dual-format playback, barge-in
    ├── style.css
    ├── manifest.json
    └── icon.png
```

```
▣ END TRANSMISSION
```
