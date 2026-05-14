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

Store credentials in the macOS Keychain (run once):

```bash
bash scripts/store-credentials.sh
```

Register with Claude Code:

```bash
claude mcp add tplink-deco \
  -- uv --directory ~/repos/nakomis/mcps/tplink-deco-mcp run tplink-deco-mcp
```

## Credentials

Credentials are stored in the macOS Keychain under service `tplink-deco`:

| Keychain account | Default | Description |
|---|---|---|
| `host` | `192.168.1.1` | Router IP or hostname |
| `username` | `admin` | Admin username |
| `password` | *(required)* | Admin password |

No credentials are stored in the Claude settings file.

## Notes

- The Deco admin API uses a custom RSA+AES encryption scheme for all requests. This is handled transparently — you just need the credentials above.
- Sessions are maintained across tool calls within the same process. If the session expires, the next call re-authenticates automatically.
- The Deco only allows one admin session at a time. If you're logged in via the Deco app, API calls may fail with a 403 until the app session expires.

## Credits

Based on the reverse-engineered API from:

- [amosyuen/ha-tplink-deco](https://github.com/amosyuen/ha-tplink-deco) (MIT licence) — the main reference for the RSA+AES login flow and admin endpoint structure
- [rosmo's gist](https://gist.github.com/rosmo/29200c1aedb991ce55942c4ae8b54edd) — additional detail on the sign/data format difference between login and admin calls

## Sign format

The RSA-signed string has two forms:

- **Login**: `k={aes_key}&i={aes_iv}&h={md5(user+pass)}&s={seq+datalen}` — includes the AES session key
- **Admin calls**: `h={md5(user+pass)}&s={seq+datalen}` — hash and sequence only

Older API implementations that always use the login form for every call will receive a 403 on newer Deco firmware, which now validates the sign format server-side.
