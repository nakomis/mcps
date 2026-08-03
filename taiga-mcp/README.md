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
| `list_user_stories` | List user stories, optionally filtered by sprint or status. Paged/summary — see below |
| `get_user_story` | Get full details of a user story including description and comments |
| `create_user_story` | Create a user story |
| `update_user_story` | Update subject, description, status, sprint assignment, or tags |
| `add_comment` | Add a comment to a user story, issue, or task |
| `list_issues` | List issues, optionally filtered by status, type, or priority. Paged/summary — see below |
| `get_issue` | Get full details of an issue including description and comments |
| `create_issue` | Create an issue with type, priority, and severity |
| `update_issue` | Update subject, description, status, type, or priority |
| `list_tasks` | List tasks, optionally filtered by sprint or parent user story. Paged/summary — see below |
| `get_task` | Get full details of a task including description and comments |
| `create_task` | Create a task, optionally linked to a user story |
| `update_task` | Update a task's subject, status, or assignee |
| `list_epics` | List all epics in a project. Paged/summary — see below |
| `get_epic` | Get full details of an epic including description and comments |
| `create_epic` | Create an epic with optional colour |
| `list_wiki_pages` | List wiki page slugs for a project |
| `get_wiki_page` | Get the Markdown content of a wiki page |

## Notes

- Create projects via the Taiga UI; this MCP manages content within projects.
- Call `get_project` first when working with a project — it returns all status/type/priority IDs you'll need for filtering and creating items.
- `update_user_story` accepts `clear_sprint=True` to move a story back to the backlog.

### Comments

Real decisions live in Taiga comments, so no read tool is allowed to stay
silent about them:

- Every `get_*` tool — `get_story`, `get_user_story`, `get_user_story_by_ref`,
  `pick_up_story`, `get_issue`, `get_task`, `get_epic` — returns the full
  `comments` array (author, text, timestamp), hidden/deleted entries excluded.
- The `list_*` tools never inline comment text: that would be one extra request
  per item, and Home Infrastructure alone has hundreds of stories.
- `list_user_stories` and `list_tasks` instead return `comment_count` on every
  item, in both full and summary shape. It comes free from Taiga's
  `total_comments` field, so it costs nothing. A non-zero count is the cue to
  go and read before drawing conclusions.
- `list_issues` and `list_epics` return **no** count, because Taiga's issue and
  epic endpoints carry no `total_comments` field at all — there is nothing to
  report cheaply. Assume any issue or epic may have unread discussion and call
  `get_issue`/`get_epic` on the ones that matter.

### Listing large projects (`list_user_stories`, `list_issues`, `list_tasks`, `list_epics`)

These four tools share a common set of extra parameters, added to stop large
projects from overflowing the MCP/LLM response limit (this genuinely happened
on the Home Infrastructure project):

- `include_closed` (default `False`) — only open (non-closed) items are
  returned unless you pass `True`. Filtering happens client-side, since
  Taiga's API doesn't support filtering by closed state server-side, so a
  paged response can return fewer than `page_size` items when closed items
  are being excluded.
- `summary` (default `False`) — when `True`, each item is shaped down to just
  `{ref, subject, status}` instead of the full ~10-field dict. Much cheaper
  for triage across a big backlog.
- `page` / `page_size` (default page_size `50`) — when `page` is given, the
  tool does one real Taiga-paginated request instead of fetching everything,
  and the response includes `total_count`/`has_more` so you can walk through
  results.

All four now return a dict — `{items, returned_count, total_count, page,
page_size, has_more, summary, note?}` — rather than a bare list. This is a
breaking change to the return shape (TAIG-22); the `items` field holds what
used to be the whole response.

When `page` is omitted, the tool still tries to return everything by default,
but two thresholds protect against oversized responses:

- Above **200** matching items, the response auto-downgrades to summary shape
  (still everything, just smaller per-item) and sets a `note` explaining why.
- Above **500** matching items, the tool refuses to enumerate at all and
  returns `total_count` plus a `note` suggesting you page through results or
  narrow the query with a status/sprint/epic filter.
