# tplink-deco-mcp

MCP server for managing a TP-Link Deco mesh network via the unofficial Deco admin API.

## Tools

| Tool | Description |
|---|---|
| `list_clients` | List all devices connected to the mesh (name, IP, MAC, band, node) |
| `list_deco_nodes` | List all Deco mesh nodes and their status |
| `reboot_deco_nodes` | Reboot one or more nodes by MAC address |
| `logout_deco` | Explicitly log out (next call re-authenticates automatically) |

## Setup

```bash
cd ~/repos/nakomis/mcps/tplink-deco-mcp
uv sync
```

Register with Claude Code:

```bash
claude mcp add tplink-deco \
  -e DECO_HOST=192.168.1.1 \
  -e DECO_USERNAME=admin \
  -e DECO_PASSWORD=your-password \
  -- uv --directory ~/repos/nakomis/mcps/tplink-deco-mcp run tplink-deco-mcp
```

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `DECO_HOST` | `192.168.1.1` | Router IP or hostname |
| `DECO_USERNAME` | `admin` | Admin username |
| `DECO_PASSWORD` | *(required)* | Admin password |

## Notes

- The Deco admin API uses a custom RSA+AES encryption scheme for all requests. This is handled transparently — you just need the credentials above.
- Sessions are maintained across tool calls within the same process. If the session expires, the next call re-authenticates automatically.
- The Deco only allows one admin session at a time. If you're logged in via the Deco app, API calls may fail with a 403 until the app session expires.
- Based on the reverse-engineered API from [amosyuen/ha-tplink-deco](https://github.com/amosyuen/ha-tplink-deco) and [rosmo's gist](https://gist.github.com/rosmo/29200c1aedb991ce55942c4ae8b54edd).
