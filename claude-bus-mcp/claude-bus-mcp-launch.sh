#!/usr/bin/env bash
# Launch the claude-bus MCP. Reads ~/.config/claude-bus/config.toml-style
# environment if present (just sourced as shell-syntax key=value lines).
set -euo pipefail

CONFIG="${CLAUDE_BUS_CONFIG:-$HOME/.config/claude-bus/env}"
if [[ -f "$CONFIG" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$CONFIG"; set +a
fi

# Sensible defaults if nothing's configured.
export CLAUDE_BUS_NATS_SERVERS="${CLAUDE_BUS_NATS_SERVERS:-nats://luke.local:4222,nats://leia.local:4222,nats://phi.local:4222}"
export CLAUDE_BUS_INBOX_DIR="${CLAUDE_BUS_INBOX_DIR:-/tmp}"
export CLAUDE_BUS_SESSION_TTL="${CLAUDE_BUS_SESSION_TTL:-600}"

exec uv --directory "$(dirname "$0")" run claude-bus-mcp
