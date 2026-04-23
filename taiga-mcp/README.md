# taiga-mcp

MCP server providing read/write access to a self-hosted Taiga instance.

## Setup

### 1. Get your auth token

Log in to the Taiga API to retrieve a bearer token:

```sh
curl -s -X POST http://your-taiga-host/api/v1/auth \
  -H "Content-Type: application/json" \
  -d '{"type":"normal","username":"YOUR_USERNAME","password":"YOUR_PASSWORD"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['auth_token'])"
```

Copy the token that's printed. It's persistent — you won't need to regenerate it unless you log out.

### 2. Install dependencies

```sh
cd ~/repos/nakomis/mcps/taiga-mcp
uv sync
```

### 3. Register the MCP server

```sh
claude mcp add taiga \
  -e TAIGA_URL=http://your-taiga-host \
  -e TAIGA_AUTH_TOKEN=your-token-here \
  -- uv --directory ~/repos/nakomis/mcps/taiga-mcp run taiga-mcp
```

Restart Claude Code after running.

## Available Tools

| Tool | Description |
|---|---|
| `list_projects` | List all projects accessible to the authenticated user |
| `get_project` | Get a project with its status, type, priority, and member lists — read these to get IDs for other tools |
| `list_milestones` | List sprints in a project |
| `create_milestone` | Create a sprint with start/finish dates |
| `list_user_stories` | List user stories, optionally filtered by sprint or status |
| `get_user_story` | Get full details of a user story including description |
| `create_user_story` | Create a user story |
| `update_user_story` | Update subject, description, status, sprint assignment, or tags |
| `list_issues` | List issues, optionally filtered by status, type, or priority |
| `get_issue` | Get full details of an issue including description |
| `create_issue` | Create an issue with type, priority, and severity |
| `update_issue` | Update subject, description, status, type, or priority |
| `list_tasks` | List tasks, optionally filtered by sprint or parent user story |
| `create_task` | Create a task, optionally linked to a user story |
| `update_task` | Update a task's subject, status, or assignee |
| `list_epics` | List all epics in a project |
| `create_epic` | Create an epic with optional colour |
| `list_wiki_pages` | List wiki page slugs for a project |
| `get_wiki_page` | Get the Markdown content of a wiki page |

## Notes

- Create projects via the Taiga UI; this MCP manages content within projects.
- Call `get_project` first when working with a project — it returns all status/type/priority IDs you'll need for filtering and creating items.
- `update_user_story` accepts `clear_sprint=True` to move a story back to the backlog.
