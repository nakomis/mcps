#!/usr/bin/env bash
# Installs the daily LaunchAgent that picks up the Plane MCP's renewed
# certificate (HOME-389). Idempotent: re-running reloads it.
set -euo pipefail

LABEL=com.nakomis.plane-mcp-cert-refresh
HERE="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/plane-mcp-cert-refresh.log"
DOMAIN="gui/$(id -u)"

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s#REPLACE_SCRIPT#${HERE}/refresh-cert.sh#" -e "s#REPLACE_LOG#${LOG}#" \
    "${HERE}/${LABEL}.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null

launchctl bootout "${DOMAIN}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "loaded ${LABEL}; it runs now and daily at 09:30"
echo "log: ${LOG}"
