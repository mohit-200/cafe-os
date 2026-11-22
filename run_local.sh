#!/usr/bin/env bash
# Run CAFÉ OS locally: in-memory storage, rule-based intent parsing, demo clock pinned to 14:30.
# Optional: export GEMINI_BACKEND=apikey GEMINI_API_KEY=... (or vertex + GOOGLE_CLOUD_PROJECT) to use Gemini.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv 2>/dev/null || python3 -m virtualenv .venv
  .venv/bin/pip install -q -r requirements-dev.txt
fi

export APP_ENV=dev
export DEMO_START="${DEMO_START:-14:30}"
exec .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port "${PORT:-8080}"
