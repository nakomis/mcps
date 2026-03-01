# trello-mcp

MCP server providing read/write access to Trello boards, lists, and cards.

## Setup

### 1. Get your Trello credentials

Go to **https://trello.com/app-key**:
- Copy the **API Key** (short hex string at the top)
- Click **"Generate a Token"** and copy the **Token** (longer hex string)

### 2. Set environment variables

Add to `~/.zshenv`:

```sh
export TRELLO_API_KEY="your-api-key-here"
export TRELLO_TOKEN="your-token-here"
```

Then reload: `source ~/.zshenv`

### 3. Install the MCP

Add to `~/.claude/mcp.json`:

```json
{
  "mcpServers": {
    "trello": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/martinmu_1/repos/nakomis/mcps/trello-mcp",
        "run",
        "trello-mcp"
      ],
      "env": {
        "TRELLO_API_KEY": "${TRELLO_API_KEY}",
        "TRELLO_TOKEN": "${TRELLO_TOKEN}"
      }
    }
  }
}
```

### 4. Install dependencies

```sh
cd ~/repos/nakomis/mcps/trello-mcp
uv sync
```

## Available Tools

| Tool | Description |
|---|---|
| `list_boards` | List all your Trello boards |
| `get_board` | Get details of a specific board |
| `list_lists` | Get all lists on a board |
| `create_list` | Create a new list on a board |
| `list_cards` | Get all cards in a list |
| `get_card` | Get full details of a card |
| `create_card` | Create a card in a list |
| `update_card` | Update name, description, position, or move to another list |
| `move_card_to_top` | Move a card to the top of its list |
| `archive_card` | Archive a card |
| `search_cards` | Search cards by text across all boards or a specific board |
| `list_labels` | Get all labels on a board |
| `add_label_to_card` | Add a label to a card |
