#!/usr/bin/env bash
# Reads Claude's Plane API token from the macOS login keychain and launches the MCP.
# Keychain entry required (also in SSM /plane/claude/api-token):
#   security add-generic-password -s plane -a claude-api-token -w <token>
set -euo pipefail

export PLANE_URL="${PLANE_URL:-https://plane.home.nakomis.com}"
export PLANE_WORKSPACE="${PLANE_WORKSPACE:-nakomis}"
export PLANE_API_KEY=$(security find-generic-password -s plane -a claude-api-token -w)

exec uv --directory "$(dirname "$0")" run plane-mcp
