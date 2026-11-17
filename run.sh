#!/usr/bin/env bash
set -euo pipefail

# Tail-Pass (ts-reserve) Launcher Script
# Always invokes the virtualenv's Python & Uvicorn directly so global /usr/bin/uvicorn
# never shadows the virtualenv packages.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -x ".venv/bin/python" ] || ! ./.venv/bin/python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    echo "Tail-Pass virtual environment or dependencies not found. Running ./install.sh first..."
    ./install.sh --no-start
fi

PORT="${PORT:-8080}"
HOST="${HOST:-0.0.0.0}"

echo "Starting Tail-Pass on http://${HOST}:${PORT} ..."
exec ./.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
