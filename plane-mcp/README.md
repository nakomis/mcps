# plane-mcp

MCP server for the self-hosted Plane CE at `https://plane.home.nakomis.com`,
workspace `nakomis`. It replaced taiga-mcp when Taiga was migrated to Plane on
14 Sep 2026 (HOME-372).

Work items are addressed by ref, `PROJECT-N` (e.g. `HOME-42`). Items migrated
from Taiga kept their Taiga numbers, so a ref from an old commit, PR title or
conversation resolves to the same item. Descriptions and comments go in and
come back as markdown; Plane stores HTML.

## Setup

Plane sits behind Leia's nginx, which requires a client certificate (mTLS).
The API token belongs to the `claude` Plane user:

```sh
security add-generic-password -s plane -a claude-api-token -w <token>   # also in SSM /plane/claude/api-token
cd ~/repos/nakomis/mcps/plane-mcp && uv sync
```

Registered through meta-mcp (`meta-mcp/config.toml`, gitignored):

```toml
[[mcps]]
name = "plane"
description = "Plane project management"
command = ["/Users/nakomis/repos/nakomis/mcps/plane-mcp/plane-mcp-launch.sh"]
env = {PLANE_URL = "https://plane.home.nakomis.com", PLANE_WORKSPACE = "nakomis", PLANE_CLIENT_CERT = "/Users/nakomis/.config/nakomis/client.crt", PLANE_CLIENT_KEY = "/Users/nakomis/.config/nakomis/client.key"}
```

## Tools

| Tool | Does |
|---|---|
| `list_projects` | Identifiers, names, archived flag; `refresh=True` bypasses the cache |
| `get_project` | States (with group), labels, members; `refresh=True` bypasses the cache |
| `create_project` | New project; adds martin@ and plane@ as admins, and registers it with the nakom.is shortener |
| `sync_shortener_projects` | Register every project with the nakom.is shortener (backfill or recovery) |
| `list_work_items` | Open items by default; filter by state, label, assignee (`me`) |
| `search_work_items` | Text search across the workspace or one project |
| `get_story` | One item by ref, with description, comments, parent and project context. Read-only |
| `pick_up_story` | Move to "In progress", assign to Claude, return the item |
| `create_work_item` | Name, markdown description, state, labels, assignees, priority, parent ref, `epic` |
| `update_work_item` | Any of the above by ref; `add_labels` / `remove_labels` |
| `add_comment` | Markdown comment by ref |
| `list_epics` | Items labelled `epic`, with sub-item counts |
| `link_work_items` | Attach a URL (e.g. a PR) to an item |

## Caching

Projects, states, labels and members are cached for five minutes (`PLANE_CACHE_TTL`, seconds), not for the life of the process: Claude sessions run for days, and a project created from another session should appear. Pass `refresh=True` to `list_projects` or `get_project` to re-read at once. Looking up an unknown identifier always refreshes.

## nakom.is shortener

`nakom.is/plane/<identifier>` opens a project's work items and `nakom.is/plane/<identifier> <n>` one work item. The shortener's Lambda never calls Plane: it reads a row per project from the `ticket-projects` DynamoDB table (nakom.is repo), and this MCP writes that row. `create_project` registers each new project; `sync_shortener_projects` upserts every project. Both use `UpdateItem`, so they never remove hand-added aliases such as `nako` → NAKIS, and a failed write is reported rather than failing the Plane operation.

AWS access is through IAM Roles Anywhere with a client certificate whose CN is `plane-mcp`, assuming the `plane-mcp-sync` role (nakom.is `LambdaStack`), which may only `UpdateItem` on that table. Setup:

1. Request and approve a `roles-anywhere` certificate with CN `plane-mcp` in the cert-portal
2. `home-servers/scripts/collect-cert.sh --role plane-mcp --url '<presigned-url>'` installs it, adds the `plane-mcp` AWS profile and proves it can assume the role

3. `cert-refresh/install.sh` loads a daily LaunchAgent that installs renewed certificates (below)

### Certificate renewal

The certificate lasts a year, and renews itself (HOME-389). Thirty days before it expires, the home cert portal issues a new one with no approval needed — `plane-mcp` is on its auto-renew allow-list — publishes it to SSM (`/plane-mcp/prod/client-cert` and `client-key`), and sends a push to Martin's phone. The LaunchAgent (`cert-refresh/refresh-cert.sh`, daily at 09:30 and at login) fetches it using the current certificate, checks the CN, that the key matches and that it lasts longer, proves it can assume the role, and only then swaps it into `~/.config/plane-mcp/`, keeping the previous pair in `previous/`. Log: `~/Library/Logs/plane-mcp-cert-refresh.log`.

If the Mac is off for the whole month before expiry, the old certificate lapses and can't fetch its successor. That is deliberate: re-issue it by hand with `collect-cert.sh --role plane-mcp`.

Overrides: `SHORTENER_AWS_PROFILE` (default `plane-mcp`), `SHORTENER_AWS_REGION` (`eu-west-2`), `SHORTENER_TABLE` (`ticket-projects`).

## Plane CE notes

- No epic type in CE: epics are items labelled `epic`, with their stories as sub-items.
- Project names can't contain `- . & + , : ; $ ^ { } * = ? @ # | ' < > ( ) % !`.
- New projects open as a list for everyone; the board default for Martin on
  migrated projects was set by `home-infra/taiga/migration/fixups.py`.
- No public API for Pages or for archiving work items.
