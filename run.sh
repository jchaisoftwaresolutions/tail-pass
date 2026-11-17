#!/usr/bin/env bash
set -euo pipefail

# Tail-Pass (ts-reserve) Launcher Script
# Always invokes the virtualenv's Python & Uvicorn directly and checks if the target
# port is already in use (e.g., by Headscale on 8080 or a previous Tail-Pass instance).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

if [ ! -x ".venv/bin/python" ] || ! ./.venv/bin/python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    echo "Tail-Pass virtual environment or dependencies not found. Running ./install.sh first..."
    ./install.sh --no-start
fi

PORT="${1:-${PORT:-8080}}"
HOST="${HOST:-0.0.0.0}"

port_in_use() {
    local p="$1"
    if command -v ss >/dev/null 2>&1; then
        ss -tuln | grep -qE ":${p}[[:space:]]"
    elif command -v lsof >/dev/null 2>&1; then
        lsof -iTCP:"${p}" -sTCP:LISTEN >/dev/null 2>&1
    else
        ./.venv/bin/python -c "import socket; s=socket.socket(); res=s.connect_ex(('127.0.0.1', int('${p}'))); s.close(); exit(0 if res==0 else 1)"
    fi
}

while port_in_use "$PORT"; do
    echo -e "${YELLOW}⚠️  Port ${PORT} is already in use on this machine:${NC}"
    if command -v ss >/dev/null 2>&1; then
        sudo ss -tulnp | grep -E ":${PORT}[[:space:]]" || true
    fi

    # Suggest next free port (e.g. 8085)
    SUGGESTED_PORT=8085
    while port_in_use "$SUGGESTED_PORT"; do
        SUGGESTED_PORT=$((SUGGESTED_PORT + 1))
    done

    echo ""
    echo "Options:"
    echo "  1) Use port ${SUGGESTED_PORT} instead (Recommended if Headscale or another service uses ${PORT})"
    echo "  2) Kill the process currently listening on port ${PORT} (via sudo fuser -k ${PORT}/tcp)"
    echo "  3) Enter a custom port"
    read -r -p "Select an option [1/2/3] (default: 1): " choice
    choice="${choice:-1}"

    case "$choice" in
        1)
            PORT="$SUGGESTED_PORT"
            break
            ;;
        2)
            echo -e "${YELLOW}Stopping process on port ${PORT}...${NC}"
            sudo fuser -k "${PORT}/tcp" || true
            sleep 1
            ;;
        3)
            read -r -p "Enter custom port number: " custom_port
            PORT="${custom_port:-$SUGGESTED_PORT}"
            ;;
        *)
            PORT="$SUGGESTED_PORT"
            break
            ;;
    esac
done

export PORT
export TAILPASS_BASE_URL="${TAILPASS_BASE_URL:-http://localhost:${PORT}}"

echo -e "${GREEN}Starting Tail-Pass on http://${HOST}:${PORT} ...${NC}"
exec ./.venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT"
