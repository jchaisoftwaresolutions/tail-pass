#!/usr/bin/env bash
set -euo pipefail

# Tail-Pass (ts-reserve) Lightweight Bootstrap, Port Selector & Systemd Service Installer

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}=========================================================${NC}"
echo -e "${CYAN}  Tail-Pass (ts-reserve) — Setup & Dependency Installer  ${NC}"
echo -e "${CYAN}=========================================================${NC}"

install_system_packages() {
    local pkgs=("$@")
    echo -e "${YELLOW}Running: sudo apt-get update && sudo apt-get install -y ${pkgs[*]}${NC}"
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update
        sudo apt-get install -y "${pkgs[@]}"
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-pip
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --noconfirm python python-pip
    else
        echo -e "${RED}Unsupported package manager. Please install: ${pkgs[*]} manually.${NC}"
        exit 1
    fi
}

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

MISSING_PKGS=()

# 1. Check for python3
if ! command -v python3 >/dev/null 2>&1; then
    MISSING_PKGS+=("python3")
fi

# 2. Check for python3-venv and ensurepip
if command -v python3 >/dev/null 2>&1; then
    if ! python3 -c "import venv, ensurepip" >/dev/null 2>&1; then
        MISSING_PKGS+=("python3-venv" "python3-pip")
    fi
else
    MISSING_PKGS+=("python3-venv" "python3-pip")
fi

# 3. Prompt user to install missing system packages via sudo
if [ ${#MISSING_PKGS[@]} -gt 0 ]; then
    echo -e "${YELLOW}Missing required system packages:${NC} ${MISSING_PKGS[*]}"
    read -r -p "Would you like to install them now using sudo? [Y/n] " response
    response="${response:-Y}"
    if [[ "$response" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        install_system_packages "${MISSING_PKGS[@]}"
    else
        echo -e "${RED}Cannot proceed without ${MISSING_PKGS[*]}. Exiting.${NC}"
        exit 1
    fi
else
    echo -e "${GREEN}✓ python3 and python3-venv are installed.${NC}"
fi

# 4. Create or repair .venv
if [ ! -x ".venv/bin/python" ] || [ ! -x ".venv/bin/pip" ]; then
    if [ -d ".venv" ]; then
        echo -e "${YELLOW}Detected incomplete .venv (missing pip/uvicorn inside venv). Rebuilding .venv...${NC}"
        rm -rf .venv
    else
        echo -e "${CYAN}Creating Python virtual environment (.venv)...${NC}"
    fi
    python3 -m venv .venv
fi

echo -e "${GREEN}✓ Virtual environment (.venv) is ready.${NC}"

# 5. Install Python dependencies explicitly into .venv
echo -e "${CYAN}Installing Python packages from requirements.txt into .venv...${NC}"
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo -e "${GREEN}✓ All Tail-Pass dependencies (FastAPI, Uvicorn, SQLModel, AsyncSSH, etc.) installed!${NC}"

if [[ "${1:-}" == "--no-start" ]]; then
    exit 0
fi

# 6. Resolve Port (and save to .env so run.sh and systemd service share it)
PORT="${PORT:-8080}"
if [ -f ".env" ]; then
    # shellcheck disable=SC1091
    source .env || true
fi

# Stop existing tail-pass systemd service temporarily while checking ports if already installed
if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet tail-pass.service 2>/dev/null; then
    echo -e "${YELLOW}Note: tail-pass.service is currently active.${NC}"
fi

while port_in_use "$PORT"; do
    echo ""
    echo -e "${YELLOW}⚠️  Port ${PORT} is currently in use on this machine:${NC}"
    if command -v ss >/dev/null 2>&1; then
        sudo ss -tulnp | grep -E ":${PORT}[[:space:]]" || true
    fi

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

cat > .env <<EOF
PORT=${PORT}
HOST=0.0.0.0
TAILPASS_DB_PATH=${SCRIPT_DIR}/tailpass.db
TAILPASS_BASE_URL=http://localhost:${PORT}
EOF

echo -e "${GREEN}✓ Saved port configuration (PORT=${PORT}) to .env${NC}"

# 7. Final Prompt: Install and start as a systemd background service
echo ""
read -r -p "Would you like to install and run Tail-Pass as a background systemd service (auto-starts on boot)? [Y/n] " svc_resp
svc_resp="${svc_resp:-Y}"

if [[ "$svc_resp" =~ ^([yY][eE][sS]|[yY])$ ]]; then
    SERVICE_USER="$(id -un)"
    SERVICE_GROUP="$(id -gn)"
    SERVICE_FILE="/etc/systemd/system/tail-pass.service"

    echo -e "${CYAN}Installing systemd service to ${SERVICE_FILE} (using sudo)...${NC}"
    sudo tee "$SERVICE_FILE" > /dev/null <<EOF
[Unit]
Description=Tail-Pass (ts-reserve) Tailnet Seat Checkout & ACL Gateway
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${SCRIPT_DIR}
EnvironmentFile=${SCRIPT_DIR}/.env
ExecStart=${SCRIPT_DIR}/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable tail-pass.service
    sudo systemctl restart tail-pass.service

    echo ""
    echo -e "${GREEN}=========================================================${NC}"
    echo -e "${GREEN}✓ Tail-Pass is now running as a systemd service!${NC}"
    echo -e "  • Web UI:      ${CYAN}http://0.0.0.0:${PORT}${NC}"
    echo -e "  • Status:      ${CYAN}sudo systemctl status tail-pass${NC}"
    echo -e "  • Live Logs:   ${CYAN}sudo journalctl -u tail-pass -f${NC}"
    echo -e "  • Restart:     ${CYAN}sudo systemctl restart tail-pass${NC}"
    echo -e "${GREEN}=========================================================${NC}"
else
    echo ""
    read -r -p "Would you like to start Tail-Pass in the foreground now on port ${PORT}? [Y/n] " fg_resp
    fg_resp="${fg_resp:-Y}"
    if [[ "$fg_resp" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        exec ./run.sh "$PORT"
    else
        echo -e "You can start Tail-Pass anytime with: ${CYAN}./run.sh${NC}"
    fi
fi
