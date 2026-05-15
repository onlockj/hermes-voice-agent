#!/usr/bin/env bash
# HERMES VOICE AGENT — one-shot setup & deploy
set -euo pipefail

CYAN="\033[36m"; DIM="\033[2m"; BOLD="\033[1m"; RED="\033[31m"; GREEN="\033[32m"; NC="\033[0m"

banner() {
  echo -e "${BOLD}"
  echo "  ▣ HERMES // VOICE LINK"
  echo "  ─────────────────────────"
  echo -e "${NC}${DIM}  LCKD private stack · live voice deploy${NC}"
  echo
}

require() {
  command -v "$1" >/dev/null 2>&1 || { echo -e "${RED}Missing: $1${NC}"; exit 1; }
}

prompt_env() {
  local key="$1" cur="${2:-}" secret="${3:-}" val
  if [ -n "$cur" ]; then
    echo -e "${DIM}$key already set — keeping existing value${NC}"
    return
  fi
  if [ "$secret" = "secret" ]; then
    read -rsp "  $key: " val; echo
  else
    read -rp "  $key: " val
  fi
  echo "$key=$val" >> .env
}

write_env() {
  if [ -f .env ]; then
    echo -e "${DIM}.env exists. Reusing.${NC}"
  else
    echo -e "${CYAN}» Creating .env${NC}"
    : > .env
    echo "Enter required values:"
    prompt_env TELEGRAM_BOT_TOKEN "" secret
    prompt_env GROQ_API_KEY "" secret
    prompt_env WEBAPP_URL "" plain
    echo
    echo -e "${DIM}Optional: ElevenLabs key for premium voice (leave blank to use free Edge TTS)${NC}"
    read -rp "  ELEVENLABS_API_KEY (optional): " ELEVENLABS_KEY
    echo "ELEVENLABS_API_KEY=${ELEVENLABS_KEY}" >> .env
    echo "ELEVENLABS_VOICE_ID=pNInz6obpgDQGcFmaJgB" >> .env
    echo "ELEVENLABS_MODEL=eleven_flash_v2_5" >> .env
    echo "TTS_PROVIDER=auto" >> .env
    echo "EDGE_TTS_VOICE=en-US-GuyNeural" >> .env
    echo "GROQ_STT_MODEL=whisper-large-v3-turbo" >> .env
    echo "GROQ_LLM_MODEL=llama-3.3-70b-versatile" >> .env
    echo "ALLOWED_USER_IDS=" >> .env
    echo "HOST=0.0.0.0" >> .env
    echo "PORT=8080" >> .env
  fi
}

install_local() {
  echo -e "${CYAN}» Installing Python dependencies${NC}"
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install --upgrade pip
  pip install -r requirements.txt
  echo -e "${GREEN}✓ Dependencies installed.${NC}"
  echo -e "${DIM}Activate later with: source .venv/bin/activate${NC}"
}

set_webhook() {
  # shellcheck disable=SC1091
  set -a; source .env; set +a
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${WEBAPP_URL:-}" ]; then
    echo -e "${RED}Cannot set webhook: TELEGRAM_BOT_TOKEN or WEBAPP_URL missing${NC}"
    return
  fi
  echo -e "${CYAN}» Registering Telegram webhook → ${WEBAPP_URL}/webhook${NC}"
  curl -fsS -X POST \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
    -d "url=${WEBAPP_URL}/webhook" \
    -d "drop_pending_updates=true" \
    -d 'allowed_updates=["message","callback_query"]' | sed 's/^/    /'
  echo
}

run_local() {
  echo -e "${CYAN}» Starting local server at ${WEBAPP_URL:-http://localhost:8080}${NC}"
  # shellcheck disable=SC1091
  source .venv/bin/activate
  exec python main.py
}

deploy_railway() {
  require railway
  echo -e "${CYAN}» Deploying to Railway${NC}"
  railway up
  set_webhook
}

deploy_render() {
  echo -e "${CYAN}» Render deploy${NC}"
  echo "  1) Push this repo to GitHub"
  echo "  2) On Render: New → Blueprint → select repo"
  echo "  3) Render will pick up render.yaml"
  echo "  4) Set TELEGRAM_BOT_TOKEN, OPENAI_API_KEY, WEBAPP_URL in dashboard"
  echo "  5) After deploy, run:  ./deploy.sh webhook"
}

deploy_fly() {
  require fly
  echo -e "${CYAN}» Deploying to Fly.io${NC}"
  fly launch --no-deploy --copy-config --name hermes-voice-agent || true
  # Push every non-empty .env line as a fly secret
  while IFS='=' read -r key val; do
    [ -z "$key" ] || [ "${key:0:1}" = "#" ] && continue
    [ -z "$val" ] && continue
    fly secrets set "$key=$val" || true
  done < .env
  fly deploy
  set_webhook
}

deploy_hf() {
  echo -e "${CYAN}» Hugging Face Spaces (free, recommended)${NC}"
  cat <<'EOM'

  1. Go to https://huggingface.co/new-space
       Name: hermes-voice-agent
       SDK:  Docker
       Hardware: CPU basic (free)
  2. Push this repo:
       git remote add hf https://huggingface.co/spaces/<your-username>/hermes-voice-agent
       git push hf main
  3. Set these Space secrets (Settings → Variables and secrets):
       TELEGRAM_BOT_TOKEN, GROQ_API_KEY,
       WEBAPP_URL=https://<your-username>-hermes-voice-agent.hf.space
       (optional) ELEVENLABS_API_KEY, ELEVENLABS_VOICE_ID, ALLOWED_USER_IDS
  4. After it boots, the bot will auto-register its webhook against WEBAPP_URL.

EOM
}

deploy_docker() {
  require docker
  echo -e "${CYAN}» Building docker image${NC}"
  docker build -t hermes-voice-agent .
  echo -e "${GREEN}✓ Built. Run with:${NC}"
  echo "  docker run -d --name hermes --env-file .env -p 8080:8080 hermes-voice-agent"
}

usage() {
  cat <<EOF
${BOLD}Usage:${NC} ./deploy.sh <target>

Targets:
  local      Install deps and run the server locally (uvicorn)
  hf         Print Hugging Face Spaces deploy steps (free, recommended)
  railway    Deploy to Railway (needs: railway CLI logged in)
  render     Print Render Blueprint steps
  fly        Deploy to Fly.io (needs: fly CLI logged in)
  docker     Build Docker image and print run command
  webhook    (Re)register Telegram webhook with WEBAPP_URL
  zip        Build hermes-voice-agent.zip (no .env / .venv)
EOF
}

build_zip() {
  echo -e "${CYAN}» Packaging hermes-voice-agent.zip${NC}"
  rm -f hermes-voice-agent.zip
  zip -r hermes-voice-agent.zip . \
    -x ".venv/*" "__pycache__/*" "*/__pycache__/*" ".git/*" ".env" \
       "hermes-voice-agent.zip" "*.pyc"
  echo -e "${GREEN}✓ hermes-voice-agent.zip${NC}"
  ls -lh hermes-voice-agent.zip
}

main() {
  banner
  local target="${1:-help}"
  case "$target" in
    local)    require python3; write_env; install_local; set_webhook; run_local ;;
    hf)       deploy_hf ;;
    railway)  write_env; deploy_railway ;;
    render)   write_env; deploy_render ;;
    fly)      write_env; deploy_fly ;;
    docker)   write_env; deploy_docker ;;
    webhook)  set_webhook ;;
    zip)      build_zip ;;
    help|*)   usage ;;
  esac
}

main "$@"
