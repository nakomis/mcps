#!/usr/bin/env bash
# Launch wrapper used by meta-mcp / Claude Code MCP config.
# Same pattern as the other MCPs in this directory.
set -euo pipefail
cd "$(dirname "$0")"
exec uv run --quiet cabal-mcp "$@"
