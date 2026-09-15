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
| `list_projects` | Identifiers, names, archived flag |
| `get_project` | States (with group), labels, members |
| `create_project` | New project; adds martin@ and plane@ as admins |
| `list_work_items` | Open items by default; filter by state, label, assignee (`me`) |
| `search_work_items` | Text search across the workspace or one project |
| `get_story` | One item by ref, with description, comments, parent and project context. Read-only |
| `pick_up_story` | Move to "In progress", assign to Claude, return the item |
| `create_work_item` | Name, markdown description, state, labels, assignees, priority, parent ref, `epic` |
| `update_work_item` | Any of the above by ref; `add_labels` / `remove_labels` |
| `add_comment` | Markdown comment by ref |
| `list_epics` | Items labelled `epic`, with sub-item counts |
| `link_work_items` | Attach a URL (e.g. a PR) to an item |

## Plane CE notes

- No epic type in CE: epics are items labelled `epic`, with their stories as sub-items.
- Project names can't contain `- . & + , : ; $ ^ { } * = ? @ # | ' < > ( ) % !`.
- New projects open as a list for everyone; the board default for Martin on
  migrated projects was set by `home-infra/taiga/migration/fixups.py`.
- No public API for Pages or for archiving work items.
