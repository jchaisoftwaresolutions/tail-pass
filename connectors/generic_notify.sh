#!/usr/bin/env bash
# Tail-Pass Optional Host Connector Script (generic_notify.sh)
# Usage: generic_notify.sh "<requester_name>" "<timeout_seconds>" "<request_id>" "<callback_url>"

REQUESTER="${1:-Another user}"
TIMEOUT="${2:-60}"
REQUEST_ID="${3:-unknown}"
CALLBACK_URL="${4:-http://localhost:8080}"

TITLE="Tail-Pass Seat Handoff Request"
BODY="${REQUESTER} is requesting access to this Seat (${TIMEOUT}s remaining). Visit ${CALLBACK_URL} to Accept, Deny, or +10m."

# 1. Linux Desktop OSD via notify-send (X11/Wayland)
if command -v notify-send >/dev/null 2>&1; then
    export DISPLAY="${DISPLAY:-:0}"
    notify-send -u critical -t $((TIMEOUT * 1000)) "${TITLE}" "${BODY}" || true
fi

# 2. macOS OSD via osascript
if command -v osascript >/dev/null 2>&1; then
    osascript -e "display notification \"${BODY}\" with title \"${TITLE}\"" || true
fi

# 3. Terminal wall broadcast fallback
if command -v wall >/dev/null 2>&1; then
    echo "${TITLE}: ${BODY}" | wall 2>/dev/null || true
fi

exit 0
