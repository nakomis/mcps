#!/usr/bin/env bash
# Reads Taiga credentials from the macOS login keychain and launches the MCP.
# Keychain entries required:
#   security add-generic-password -s taiga -a username -w <admin-username>
#   security add-generic-password -s taiga -a password -w <admin-password>
set -euo pipefail

export TAIGA_URL="${TAIGA_URL:-http://luke.local:9000}"
export TAIGA_USERNAME=$(security find-generic-password -s taiga -a username -w)
export TAIGA_PASSWORD=$(security find-generic-password -s taiga -a password -w)

exec uv --directory "$(dirname "$0")" run taiga-mcp
