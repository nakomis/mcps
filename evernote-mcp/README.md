# evernote-mcp

MCP server providing read-only access to Evernote notes and notebooks, for use with Claude Code.

## Setup

### 1. Get a developer token

Go to: https://www.evernote.com/api/DeveloperToken.action

This generates a permanent personal access token for the Evernote production API.

### 2. Set your token

Edit `~/.claude/mcp.json` and replace `YOUR_TOKEN_HERE` with your token:

```json
{
  "mcpServers": {
    "evernote": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/martinmu_1/repos/nakomis/mcps/evernote-mcp",
        "run",
        "evernote-mcp"
      ],
      "env": {
        "EVERNOTE_TOKEN": "S=s1:U=...:E=...:C=...:P=...:A=en_oauth:V=2:H=..."
      }
    }
  }
}
```

### 3. Test it

In Claude Code, the following tools will be available:

- **`list_notebooks`** — list all notebooks
- **`list_notes`** — list notes in a notebook by name
- **`search_notes`** — full-text search, optionally within a notebook
- **`get_note`** — get a note's full content by GUID
- **`get_note_by_title`** — find and retrieve a note by title (partial match)

## Development

```bash
uv sync
uv run evernote-mcp   # runs the MCP server (communicates via stdio)
```

## Dependencies

- [`mcp`](https://github.com/modelcontextprotocol/python-sdk) — MCP Python SDK
- [`evernote3`](https://pypi.org/project/evernote3/) — Evernote API client (Python 3 port)
- [`beautifulsoup4`](https://pypi.org/project/beautifulsoup4/) + `lxml` — ENML → plain text conversion
