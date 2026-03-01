# nakomis/mcps

MCP servers for use with Claude Code. All code is CC0 — no rights reserved.

## Servers

| Server | Description |
|---|---|
| [evernote-mcp](evernote-mcp/) | Read-only access to Evernote notes via exported .enex files |
| [trello-mcp](trello-mcp/) | Read/write access to Trello boards, lists, and cards |

## Installation

Each server is a self-contained Python package managed with `uv`. See the individual server's README for setup instructions.

Add servers to `~/.claude/mcp.json` — see each server's README for the exact snippet.
