#!/usr/bin/env bash
set -euo pipefail

# Tail-Pass (ts-reserve) Lightweight Bootstrap & Dependency Installer
# Checks for python3, python3-venv, and pip, prompts to install with sudo if missing,
# builds/repairs .venv, installs requirements.txt, and launches Tail-Pass.

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

MISSING_PKGS=()

# 1. Check for python3
if ! command -v python3 >/dev/null 2>&1; then
    MISSING_PKGS+=("python3")
fi

# 2. Check for python3-venv and ensurepip (Ubuntu/Debian require python3-venv for working .venv with pip)
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

# 4. Create or repair .venv (if .venv/bin/pip is missing because python3-venv wasn't installed earlier, rebuild it)
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
echo ""
echo -e "To start Tail-Pass at any time, run:"
echo -e "  ${CYAN}./run.sh${NC}"
echo -e "  (or directly: ${CYAN}./.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080${NC})"
echo ""

# 6. Offer to start Tail-Pass immediately (unless --no-start was passed)
if [[ "${1:-}" != "--no-start" ]]; then
    read -r -p "Would you like to start the Tail-Pass server now on port 8080? [Y/n] " start_now
    start_now="${start_now:-Y}"
    if [[ "$start_now" =~ ^([yY][eE][sS]|[yY])$ ]]; then
        exec ./run.sh
    fi
fi
