# claude-bus-mcp

MCP server exposing the home-estate cross-Claude coordination bus. Backed by
a three-node NATS JetStream cluster (Luke, Leia, Mac); see
`nakomis/home-servers/claude-bus/` for the cluster setup.

## What it gives Claude

- `acquire / renew / release / list_resources / whoholds` — distributed leases
  on named real-world resources with strict TTLs (`leia-apply`,
  `luke-apply`, …).
- `announce / heartbeat / list_sessions` — session presence with auto-expiring
  TTLs; no PID liveness needed.
- `tell / broadcast / ask / reply / inbox` — pub/sub + blocking request/reply
  messaging between sessions. `ask` blocks until the recipient replies or the
  timeout fires.

## Push delivery

`announce()` starts NATS subscriptions on the session's inbox subjects. Each
incoming `tell`, `broadcast`, or `ask` is written as one JSON line to
`/tmp/claude-bus-<session_id>.log`.

The Claude session should `Monitor` that file to be woken on each event:

```
Monitor: stdbuf -oL tail -F -n0 /tmp/claude-bus-<session_id>.log
```

(`stdbuf -oL` is required — plain `tail -F` buffers when its stdout is a pipe.)

## Configuration

Environment variables (read by `claude-bus-mcp-launch.sh`):

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_BUS_NATS_SERVERS` | `nats://luke.local:4222,nats://leia.local:4222,nats://phi.local:4222` | Comma-separated NATS URLs; client picks one. |
| `CLAUDE_BUS_INBOX_DIR` | `/tmp` | Where per-session inbox logs live. |
| `CLAUDE_BUS_SESSION_TTL` | `600` | Session presence TTL (seconds). |

Place an override in `~/.config/claude-bus/env`:

```
CLAUDE_BUS_NATS_SERVERS=nats://luke.local:4222
```

## Wiring into meta-mcp

Add to `~/repos/nakomis/mcps/meta-mcp/config.toml`:

```toml
[mcps.claude-bus]
command = "/Users/nakomis/repos/nakomis/mcps/claude-bus-mcp/claude-bus-mcp-launch.sh"
```

## Local development

```bash
# Single-node NATS for testing
docker run -d --name claude-bus-dev -p 4222:4222 -p 8222:8222 \
  nats:2.11-alpine -js

# Init streams + KV (from home-servers repo)
cd ~/repos/nakomis/home-servers/claude-bus
NATS_URL=nats://localhost:4222 ./init-streams.sh

# Run the MCP standalone
cd ~/repos/nakomis/mcps/claude-bus-mcp
CLAUDE_BUS_NATS_SERVERS=nats://localhost:4222 uv run claude-bus-mcp
```
