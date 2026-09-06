#!/bin/bash
set -e

cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

if [ -z "$Grok_XAI_API_KEY" ] && [ -z "$XAI_API_KEY" ] && [ -z "$GROQ_API_KEY" ]; then
  echo "ERROR: no xAI/Grok API key is set."
  echo "Run: export Grok_XAI_API_KEY=\"your_xai_api_key_here\""
  echo "     (XAI_API_KEY is also accepted; GROQ_API_KEY is a last-resort legacy fallback.)"
  exit 1
fi

python3 app.py
