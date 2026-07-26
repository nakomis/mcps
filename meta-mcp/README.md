# meta-mcp

A lazy-loading proxy MCP server. Instead of loading all your MCP servers at Claude Code startup (bloating the context window with tool schemas), you load only `meta-mcp`. Sub-MCPs are spawned on demand when you first use them and kept alive for the session.

## How it works

Claude Code sees exactly three tools:

| Tool | Purpose |
|---|---|
| `list_mcps()` | List configured sub-MCPs and their one-line descriptions |
| `describe_mcp(name)` | Get the full tool schemas for a sub-MCP (spawns it if needed) |
| `call_mcp(name, tool, args)` | Call a tool on a sub-MCP |

When you ask Claude to capture an oscilloscope trace, it calls `describe_mcp("oscilloscope")` to discover the available tools, then `call_mcp("oscilloscope", "get_waveform", {...})`. The oscilloscope MCP process is spawned on the first call and kept running until Claude Code exits.

## Installation

```bash
cd meta-mcp
uv venv && uv pip install -e .
cp config.toml.example config.toml
# Edit config.toml to point at your sub-MCPs
```

## Configuration

`config.toml` lives in the `meta-mcp/` directory:

```toml
[[mcps]]
name = "oscilloscope"
description = "oscilloscope waveform capture"
command = ["python", "-m", "nakoscope_mcp"]

[[mcps]]
name = "ollama"
description = "local LLM inference"
command = ["npx", "-y", "@ollama/mcp"]
```

Each sub-MCP entry needs:
- `name` — identifier used in `describe_mcp` / `call_mcp`
- `description` — one-line summary shown in `list_mcps` output
- `command` — command list to start the sub-MCP server (stdio transport)

## Claude Code settings

Replace all your individual MCP entries in `~/.claude/settings.json` with a single entry:

```json
{
  "mcpServers": {
    "meta-mcp": {
      "command": "/path/to/meta-mcp/.venv/bin/meta-mcp"
    }
  }
}
```

The sub-MCPs do not need entries in `settings.json` — they are managed entirely by meta-mcp.

## Sub-MCP compatibility

Any MCP server that uses stdio transport (the default) works as a sub-MCP. The existing MCPs in this repo (`ollama-mcp`, `falai-mcp`, `tplink-deco-mcp`, etc.) are all compatible and remain independently installable for use without meta-mcp.
