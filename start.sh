#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${PYTHON:-python3.11}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  PYTHON=python3
fi

cd "$ROOT/backend"
if [[ ! -d .venv ]]; then
  "$PYTHON" -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  python -m playwright install chromium
else
  source .venv/bin/activate
fi

cd "$ROOT/frontend"
if [[ ! -d node_modules ]]; then
  npm install
fi

cd "$ROOT"
(cd "$ROOT/backend" && source .venv/bin/activate && exec uvicorn app.main:app --host 127.0.0.1 --port 8000) &
BACK_PID=$!
trap 'kill "$BACK_PID" 2>/dev/null || true' EXIT
cd "$ROOT/frontend"
npm run dev -- --host 127.0.0.1 --port 5173
