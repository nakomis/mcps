#!/usr/bin/env python3
"""Taiga MCP Server — read/write access to a self-hosted Taiga instance."""

import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("taiga-mcp")


# ── Config ────────────────────────────────────────────────────────────────────

def _base() -> str:
    url = os.environ.get("TAIGA_URL", "http://localhost:9000")
    return url.rstrip("/") + "/api/v1"


# Optional client certificate for mTLS-gated deployments (e.g. behind nginx that
# requires a client cert). Set TAIGA_CLIENT_CERT (+ TAIGA_CLIENT_KEY for a
# separate PEM key) to enable. Falls back to the plain httpx module otherwise,
# so this is a drop-in: _client.get/post/patch/delete match httpx.* exactly.
def _make_client():
    cert_file = os.environ.get("TAIGA_CLIENT_CERT")
    key_file = os.environ.get("TAIGA_CLIENT_KEY")
    if cert_file and key_file:
        return httpx.Client(cert=(cert_file, key_file))
    if cert_file:
        return httpx.Client(cert=cert_file)
    return httpx


_client = _make_client()


# Module-level token cache — refreshed automatically on 401.
_token: str = ""

# Users that are always added as Product Owner admins on every new project.
_DEFAULT_MEMBERS = ["claude", "admin", "nakomis"]


def _authenticate() -> str:
    username = os.environ.get("TAIGA_USERNAME", "")
    password = os.environ.get("TAIGA_PASSWORD", "")
    if not (username and password):
        raise RuntimeError("TAIGA_USERNAME and TAIGA_PASSWORD environment variables must be set")
    r = _client.post(
        f"{_base()}/auth",
        json={"type": "normal", "username": username, "password": password},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["auth_token"]


def _headers() -> dict:
    global _token
    if not _token:
        _token = os.environ.get("TAIGA_AUTH_TOKEN", "") or _authenticate()
    return {"Authorization": f"Bearer {_token}", "Content-Type": "application/json"}


def _refresh_and_headers() -> dict:
    """Force a fresh token (called after a 401)."""
    global _token
    _token = _authenticate()
    return {"Authorization": f"Bearer {_token}", "Content-Type": "application/json"}


def _get(path: str, **params) -> dict | list:
    kw = {"params": {k: v for k, v in params.items() if v is not None}, "timeout": 10}
    r = _client.get(f"{_base()}{path}", headers=_headers(), **kw)
    if r.status_code == 401:
        r = _client.get(f"{_base()}{path}", headers=_refresh_and_headers(), **kw)
    r.raise_for_status()
    return r.json()


def _get_all(path: str, **params) -> list:
    """GET a list endpoint, following Taiga's page-based pagination automatically.
    Use sparingly — this pulls every matching item into memory. list_* MCP tools
    should go through _list_with_guard instead; _get_all is for internal helpers
    (ref lookups) that genuinely need the whole set."""
    results = []
    page = 1
    while True:
        kw = {
            "params": {k: v for k, v in {**params, "page": page}.items() if v is not None},
            "timeout": 10,
        }
        r = _client.get(f"{_base()}{path}", headers=_headers(), **kw)
        if r.status_code == 401:
            r = _client.get(f"{_base()}{path}", headers=_refresh_and_headers(), **kw)
        r.raise_for_status()
        page_data = r.json()
        if not page_data:
            break
        results.extend(page_data)
        total = r.headers.get("x-pagination-count")
        if total and len(results) >= int(total):
            break
        page += 1
    return results


def _get_page(path: str, page: int, page_size: int, **params) -> tuple[list, dict]:
    """GET a single page from a Taiga list endpoint using its native pagination.

    Returns (items, info) where info carries:
      - total_count: int | None  — from the x-pagination-count header, if the
        endpoint is paginated (None if Taiga didn't send pagination headers)
      - is_paginated: bool       — from the x-paginated header
      - current_page: int        — from x-pagination-current, falling back to
        the requested page
    """
    kw = {
        "params": {
            k: v for k, v in {**params, "page": page, "page_size": page_size}.items()
            if v is not None
        },
        "timeout": 10,
    }
    r = _client.get(f"{_base()}{path}", headers=_headers(), **kw)
    if r.status_code == 401:
        r = _client.get(f"{_base()}{path}", headers=_refresh_and_headers(), **kw)
    r.raise_for_status()
    items = r.json() or []
    total = r.headers.get("x-pagination-count")
    info = {
        "total_count": int(total) if total is not None else None,
        "is_paginated": r.headers.get("x-paginated") == "true",
        "current_page": int(r.headers.get("x-pagination-current", page)),
    }
    return items, info


# ── list_* token-budget guard ────────────────────────────────────────────────
#
# Large Taiga projects can hold thousands of stories/issues/tasks. Returning
# every one of them in full detail can overflow the MCP/LLM response limit,
# making the tool entirely unusable (this happened for real on Home
# Infrastructure). _list_with_guard is the shared engine behind list_user_stories,
# list_issues, list_tasks, and list_epics:
#
#   - page=None (default): behaves like the old "give me everything" call, but
#     open-only by default and protected by two thresholds:
#       * above _SUMMARY_AUTO_THRESHOLD items, auto-downgrades to summary shape
#         (still returns everything, just as {ref, subject, status} triples)
#       * above _HARD_CAP items, refuses to enumerate at all and returns a
#         count + a note telling the caller to page or narrow their filters
#   - page=<n>: real Taiga-native pagination via _get_page — one HTTP call,
#     one page of results, plus total_count/has_more so the caller can walk
#     through a large project a page at a time.
#
# Taiga's list APIs don't support filtering on is_closed server-side, so the
# open-only default is applied client-side after fetching. That means a paged
# response can return fewer than page_size items when closed items are being
# excluded — has_more is still accurate (it's based on Taiga's own total,
# not the post-filter count) but the count on this page may look short.
_DEFAULT_PAGE_SIZE = 50
_SUMMARY_AUTO_THRESHOLD = 200
_HARD_CAP = 500


def _list_with_guard(path: str, full_shape, summary_shape, params: dict, *,
                      include_closed: bool, summary: bool,
                      page: int | None, page_size: int) -> dict:
    """Shared engine for the list_* MCP tools. See module notes above."""

    def apply_open_filter(items: list) -> list:
        if include_closed:
            return items
        return [i for i in items if not i.get("is_closed", False)]

    if page is not None:
        raw_items, info = _get_page(path, page, page_size, **params)
        items = apply_open_filter(raw_items)
        shaped = [summary_shape(i) if summary else full_shape(i) for i in items]
        total = info["total_count"]
        has_more = total is not None and (page * page_size) < total
        return {
            "items": shaped,
            "returned_count": len(shaped),
            "total_count": total,
            "page": page,
            "page_size": page_size,
            "has_more": has_more,
            "summary": summary,
        }

    # No page requested — probe the cheap way first (page_size=1) so we know
    # the total before deciding whether to actually fetch everything.
    _, probe_info = _get_page(path, 1, 1, **params)
    total = probe_info["total_count"]

    if total is not None and total > _HARD_CAP:
        return {
            "items": [],
            "returned_count": 0,
            "total_count": total,
            "page": None,
            "page_size": page_size,
            "has_more": True,
            "summary": summary,
            "note": (
                f"{total} items matched — above the {_HARD_CAP}-item hard cap, "
                "so nothing was enumerated. Use page/page_size to fetch in "
                "batches, or narrow the query with a status/sprint/epic filter."
            ),
        }

    auto_summary = summary or (total is not None and total > _SUMMARY_AUTO_THRESHOLD)
    raw_items = _get_all(path, **params)
    items = apply_open_filter(raw_items)
    shaped = [summary_shape(i) if auto_summary else full_shape(i) for i in items]
    result = {
        "items": shaped,
        "returned_count": len(shaped),
        "total_count": total,
        "page": None,
        "page_size": None,
        "has_more": False,
        "summary": auto_summary,
    }
    if auto_summary and not summary:
        result["note"] = (
            f"Auto-switched to summary mode: {total} items matched, above the "
            f"{_SUMMARY_AUTO_THRESHOLD}-item threshold for full detail. Pass "
            "summary=True explicitly to silence this, or page through with "
            "page/page_size for full detail."
        )
    return result


def _post(path: str, data: dict) -> dict:
    r = _client.post(f"{_base()}{path}", headers=_headers(), json=data, timeout=10)
    if r.status_code == 401:
        r = _client.post(f"{_base()}{path}", headers=_refresh_and_headers(), json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _patch(path: str, data: dict) -> dict:
    r = _client.patch(f"{_base()}{path}", headers=_headers(), json=data, timeout=10)
    if r.status_code == 401:
        r = _client.patch(f"{_base()}{path}", headers=_refresh_and_headers(), json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _versioned_patch(path: str, data: dict, known_version: int = None) -> dict:
    """PATCH with Taiga's required optimistic-locking version field.
    Pass known_version to skip the extra GET when the caller already has it."""
    if known_version is None:
        known_version = _get(path)["version"]
    data["version"] = known_version
    return _patch(path, data)


def _delete(path: str) -> None:
    r = _client.delete(f"{_base()}{path}", headers=_headers(), timeout=10)
    if r.status_code == 401:
        r = _client.delete(f"{_base()}{path}", headers=_refresh_and_headers(), timeout=10)
    r.raise_for_status()


def _extra(obj: dict, key: str, field: str = "name") -> str | None:
    info = obj.get(f"{key}_extra_info")
    return info.get(field) if info else None


def _get_comments(item_type: str, item_id: int) -> list[dict]:
    """Fetch comments from the history endpoint for a story, issue, or task."""
    entries = _get(f"/history/{item_type}/{item_id}")
    return [
        {
            "id": e["id"],
            "author": e.get("user", {}).get("username", ""),
            "author_name": e.get("user", {}).get("name", ""),
            "comment": e["comment"],
            "created_at": e.get("created_at", ""),
        }
        for e in entries
        if e.get("comment") and not e.get("is_hidden", False)
    ]


# ── Projects ──────────────────────────────────────────────────────────────────

@mcp.tool()
def create_project(name: str, description: str = None, is_private: bool = True,
                   prefix: str = None) -> dict:
    """
    Create a new Taiga project (Kanban + Epics + Wiki enabled by default).
    Automatically adds claude, admin, and nakomis as Product Owner admins.
    Pass prefix (e.g. 'HOME') to set the Jira-style ticket prefix immediately.
    Returns the new project's id, name, slug, prefix, and default status IDs.
    """
    data: dict = {
        "name": name,
        "is_private": is_private,
        "is_kanban_activated": True,
        "is_backlog_activated": False,
        "is_epics_activated": True,
        "is_wiki_activated": True,
    }
    if description is not None: data["description"] = description
    result = _post("/projects", data)
    project_id = result["id"]

    # Find the Product Owner role for this project
    roles = _get(f"/roles", project=project_id)
    po_role = next((r for r in roles if r["name"] == "Product Owner"), None)

    # Add default members. Taiga's API has a "valid contact" restriction, so failures are
    # expected when users haven't previously shared a project. _ensure_default_members
    # can be called afterwards to add them via a direct DB path if needed.
    added = []
    if po_role:
        existing_usernames = {m["username"] for m in result.get("members", [])}
        for username in _DEFAULT_MEMBERS:
            if username not in existing_usernames:
                try:
                    _post("/memberships", {
                        "project": project_id,
                        "role": po_role["id"],
                        "username": username,
                    })
                    added.append(username)
                except Exception:
                    pass

    actual_prefix = None
    if prefix:
        tags_colors = dict(result.get("tags_colors") or {})
        tags_colors[f"PREFIX-{prefix.upper()}"] = None
        payload = [[k, v] for k, v in tags_colors.items()]
        _patch(f"/projects/{project_id}", {"tags_colors": payload})
        actual_prefix = prefix.upper()

    return {
        "id": result["id"],
        "name": result["name"],
        "slug": result["slug"],
        "prefix": actual_prefix,
        "description": result.get("description", ""),
        "us_statuses": [{"id": s["id"], "name": s["name"]}
                        for s in result.get("us_statuses", [])],
        "members": [{"id": m["id"], "username": m["username"], "full_name": m.get("full_name", "")}
                    for m in result.get("members", [])],
        "default_members_added": added,
    }


def _extract_prefix(tags_colors: dict) -> str | None:
    """Return the suffix of a PREFIX-xxx tag from a project's tags_colors dict, or None."""
    for tag in (tags_colors or {}):
        if tag.upper().startswith("PREFIX-"):
            return tag[7:].upper()
    return None


@mcp.tool()
def list_projects() -> list[dict]:
    """List all Taiga projects accessible to the authenticated user."""
    projects = _get("/projects")
    return [
        {
            "id": p["id"],
            "name": p["name"],
            "slug": p["slug"],
            "description": p.get("description", ""),
            "is_private": p["is_private"],
            "prefix": _extract_prefix(p.get("tags_colors", {})),
        }
        for p in projects
    ]


@mcp.tool()
def set_project_prefix(project_id: int, prefix: str) -> dict:
    """
    Set the Jira-style prefix for a project (e.g. HOME, BOOT).
    Stored as a PREFIX-xxx tag in the project's tags_colors.
    Replaces any existing PREFIX-xxx tag.
    """
    p = _get(f"/projects/{project_id}")
    tags_colors = dict(p.get("tags_colors") or {})
    for tag in list(tags_colors):
        if tag.upper().startswith("PREFIX-"):
            del tags_colors[tag]
    tags_colors[f"PREFIX-{prefix.upper()}"] = None
    # API expects a list of [name, colour] pairs, not a dict
    payload = [[k, v] for k, v in tags_colors.items()]
    result = _patch(f"/projects/{project_id}", {"tags_colors": payload})
    return {"id": project_id, "prefix": prefix.upper(), "tags_colors_count": len(result.get("tags_colors", {}))}


@mcp.tool()
def get_project(project_id: int) -> dict:
    """
    Get a project with its full status/type/priority lists.
    Use the returned IDs when creating or updating items in this project.
    """
    p = _get(f"/projects/{project_id}")
    return {
        "id": p["id"],
        "name": p["name"],
        "slug": p["slug"],
        "description": p.get("description", ""),
        "prefix": _extract_prefix(p.get("tags_colors", {})),
        "us_statuses": [{"id": s["id"], "name": s["name"], "is_closed": s["is_closed"]}
                        for s in p.get("us_statuses", [])],
        "task_statuses": [{"id": s["id"], "name": s["name"], "is_closed": s["is_closed"]}
                          for s in p.get("task_statuses", [])],
        "issue_statuses": [{"id": s["id"], "name": s["name"], "is_closed": s["is_closed"]}
                           for s in p.get("issue_statuses", [])],
        "issue_types": [{"id": t["id"], "name": t["name"]} for t in p.get("issue_types", [])],
        "priorities": [{"id": pr["id"], "name": pr["name"]} for pr in p.get("priorities", [])],
        "severities": [{"id": sv["id"], "name": sv["name"]} for sv in p.get("severities", [])],
        "members": [{"id": m["id"], "username": m["username"], "full_name": m.get("full_name", "")}
                    for m in p.get("members", [])],
    }


@mcp.tool()
def ensure_project_defaults(project_id: int) -> dict:
    """
    Ensure a project has the standard defaults applied:
    - Kanban, Wiki, and Epics activated
    - claude, admin, and nakomis added as Product Owner admins

    Safe to call on existing projects — idempotent. Use this after create_project
    if the API's contact restriction prevented default members from being added,
    or when picking up a story in a project that claude isn't yet a member of.
    """
    # Enable features
    project = _get(f"/projects/{project_id}")
    feature_updates = {}
    for key in ("is_kanban_activated", "is_wiki_activated", "is_epics_activated"):
        if not project.get(key):
            feature_updates[key] = True
    if feature_updates:
        _patch(f"/projects/{project_id}", feature_updates)

    # Add default members as Product Owners
    roles = _get("/roles", project=project_id)
    po_role = next((r for r in roles if r["name"] == "Product Owner"), None)

    added, skipped = [], []
    if po_role:
        memberships = _get("/memberships", project=project_id)
        existing = {m["user_data"]["username"] for m in memberships if m.get("user_data")}
        for username in _DEFAULT_MEMBERS:
            if username in existing:
                skipped.append(username)
                continue
            try:
                _post("/memberships", {"project": project_id, "role": po_role["id"], "username": username})
                added.append(username)
            except Exception as e:
                skipped.append(f"{username} (error: {e})")

    return {
        "project_id": project_id,
        "features_enabled": list(feature_updates.keys()),
        "members_added": added,
        "members_already_present": skipped,
    }


@mcp.tool()
def update_project(project_id: int, name: str = None, description: str = None) -> dict:
    """Update a project's name and/or description."""
    data: dict = {}
    if name is not None: data["name"] = name
    if description is not None: data["description"] = description
    result = _patch(f"/projects/{project_id}", data)
    return {"id": result["id"], "name": result["name"], "slug": result["slug"],
            "description": result.get("description", "")}


# ── Milestones / Sprints ──────────────────────────────────────────────────────

@mcp.tool()
def list_milestones(project_id: int) -> list[dict]:
    """List all sprints/milestones in a project."""
    milestones = _get("/milestones", project=project_id)
    return [
        {
            "id": m["id"],
            "name": m["name"],
            "estimated_start": m.get("estimated_start"),
            "estimated_finish": m.get("estimated_finish"),
            "closed": m.get("closed", False),
            "total_points": m.get("total_points"),
        }
        for m in milestones
    ]


@mcp.tool()
def create_milestone(project_id: int, name: str,
                     estimated_start: str, estimated_finish: str) -> dict:
    """
    Create a sprint/milestone.
    estimated_start and estimated_finish must be dates in YYYY-MM-DD format.
    """
    result = _post("/milestones", {
        "project": project_id,
        "name": name,
        "estimated_start": estimated_start,
        "estimated_finish": estimated_finish,
    })
    return {"id": result["id"], "name": result["name"]}


# ── User Stories ──────────────────────────────────────────────────────────────

def _us_full(s: dict) -> dict:
    return {
        "id": s["id"],
        "ref": s["ref"],
        "subject": s["subject"],
        "status": _extra(s, "status"),
        "status_id": s.get("status"),
        "sprint": _extra(s, "milestone"),
        "milestone_id": s.get("milestone"),
        "assigned_to": _extra(s, "assigned_to", "username"),
        "points": s.get("total_points"),
        "tags": [t[0] for t in s.get("tags", [])],
        "is_closed": s.get("is_closed", False),
    }


def _us_summary(s: dict) -> dict:
    return {"ref": s["ref"], "subject": s["subject"], "status": _extra(s, "status")}


@mcp.tool()
def list_user_stories(project_id: int, milestone_id: int = None,
                      status_id: int = None, epic_id: int = None,
                      include_closed: bool = False, summary: bool = False,
                      page: int = None, page_size: int = _DEFAULT_PAGE_SIZE) -> dict:
    """
    List user stories in a project.
    Optionally filter by sprint (milestone_id), status (status_id), or epic (epic_id).

    Defaults to open (non-closed) stories only — pass include_closed=True to
    include closed ones too.

    Pass summary=True for a minimal {ref, subject, status} shape per story —
    much cheaper on large projects. Pass page (1-based) with page_size to walk
    through results a page at a time using Taiga's native pagination instead
    of fetching everything at once.

    With no page given, every matching story is still returned by default, but
    very large result sets auto-downgrade to summary shape, and extremely large
    ones are refused outright with a count and a suggestion to page — see the
    "note" field in the response when that happens.

    Returns {items, returned_count, total_count, page, page_size, has_more,
    summary, note?}.
    """
    return _list_with_guard(
        "/userstories", _us_full, _us_summary,
        {"project": project_id, "milestone": milestone_id, "status": status_id, "epic": epic_id},
        include_closed=include_closed, summary=summary, page=page, page_size=page_size,
    )


@mcp.tool()
def get_user_story_by_ref(project_id: int, ref: int) -> dict:
    """
    Get a user story by its project-scoped ref number (e.g. the '43' in /project/home-infrastructure/us/43).
    Use this instead of get_user_story when you have a ref from a URL or the Taiga UI.
    """
    stories = _get_all("/userstories", project=project_id)
    match = next((s for s in stories if s["ref"] == ref), None)
    if match is None:
        raise ValueError(f"No user story with ref {ref} found in project {project_id}")
    return get_user_story(match["id"])


def _fetch_story_context(prefix_ref: str) -> dict:
    """
    Internal: resolve PREFIX-N to project + story data.
    Returns raw objects needed by get_story and pick_up_story, avoiding duplicated
    HTTP calls when the two tools share the same lookup logic.
    """
    prefix_ref = prefix_ref.strip().upper()
    if "-" not in prefix_ref:
        raise ValueError(f"Expected PREFIX-N format (e.g. HOME-42), got: {prefix_ref}")
    prefix, _, ref_str = prefix_ref.partition("-")
    if not ref_str.isdigit():
        raise ValueError(f"Ref must be numeric, got: {ref_str}")
    ref = int(ref_str)

    projects = _get("/projects")
    project_stub = next(
        (p for p in projects if _extract_prefix(p.get("tags_colors", {})) == prefix),
        None,
    )
    if project_stub is None:
        known = sorted({_extract_prefix(p.get("tags_colors", {})) for p in projects} - {None})
        raise ValueError(f"No project with prefix '{prefix}'. Known prefixes: {known}")

    project_id = project_stub["id"]
    full_project = _get(f"/projects/{project_id}")
    all_stories = _get_all("/userstories", project=project_id)

    match = next((s for s in all_stories if s["ref"] == ref), None)
    if match is None:
        raise ValueError(f"No story with ref {ref} in project '{full_project['name']}'")

    full_story = _get(f"/userstories/{match['id']}")
    comments = _get_comments("userstory", match["id"])

    return {
        "prefix": prefix,
        "prefix_ref": prefix_ref,
        "full_project": full_project,
        "match": match,        # list-endpoint object; carries version for versioned PATCH
        "full_story": full_story,
        "comments": comments,
    }


def _format_story_and_project(ctx: dict, status_source: dict = None) -> dict:
    """Format the standard story+project response dict from a _fetch_story_context result."""
    full_story = ctx["full_story"]
    full_project = ctx["full_project"]
    statuses = full_project.get("us_statuses", [])
    src = status_source or full_story  # use updated object for status when available
    return {
        "story": {
            "id": full_story["id"],
            "ref": full_story["ref"],
            "prefix_ref": ctx["prefix_ref"],
            "subject": full_story["subject"],
            "description": full_story.get("description", ""),
            "status": _extra(src, "status"),
            "status_id": src.get("status"),
            "assigned_to": _extra(src, "assigned_to", "username"),
            "tags": [t[0] for t in full_story.get("tags", [])],
            "is_blocked": full_story.get("is_blocked", False),
            "blocked_note": full_story.get("blocked_note", ""),
            "comments": ctx["comments"],
        },
        "project": {
            "id": full_project["id"],
            "name": full_project["name"],
            "slug": full_project["slug"],
            "prefix": ctx["prefix"],
            "us_statuses": [
                {"id": s["id"], "name": s["name"], "is_closed": s.get("is_closed", False)}
                for s in statuses
            ],
            "members": [
                {"id": m["id"], "username": m.get("username", ""), "full_name": m.get("full_name", "")}
                for m in full_project.get("members", [])
            ],
        },
    }


@mcp.tool()
def get_story(prefix_ref: str) -> dict:
    """
    Fetch a story by Jira-style reference (e.g. HOME-42, TAIG-7). Read-only.
    Returns full story details (description, comments, tags, blocked state)
    plus project context (statuses, members) — everything needed to understand
    and discuss a story without side effects.
    Use pick_up_story instead when you are about to start working on the story.
    """
    ctx = _fetch_story_context(prefix_ref)
    return _format_story_and_project(ctx)


@mcp.tool()
def pick_up_story(prefix_ref: str, assignee_id: int = None) -> dict:
    """
    Pick up a story: resolves PREFIX-N, moves it to In Progress (or first non-closed
    status), optionally assigns it, and returns full story + project context.
    Use get_story instead when you only need to read the story without side effects.
    """
    ctx = _fetch_story_context(prefix_ref)
    full_project = ctx["full_project"]
    match = ctx["match"]

    statuses = full_project.get("us_statuses", [])
    open_statuses = [s for s in statuses if not s.get("is_closed", False)]
    in_progress = next(
        (s for s in open_statuses if "progress" in s["name"].lower()),
        open_statuses[0] if open_statuses else None,
    )

    update: dict = {}
    if in_progress:
        update["status"] = in_progress["id"]
    if assignee_id is not None:
        update["assigned_to"] = assignee_id
    updated = _versioned_patch(f"/userstories/{match['id']}", update, known_version=match["version"])

    return _format_story_and_project(ctx, status_source=updated)


@mcp.tool()
def get_user_story(story_id: int) -> dict:
    """Get full details of a user story including description and comments."""
    s = _get(f"/userstories/{story_id}")
    return {
        "id": s["id"],
        "ref": s["ref"],
        "subject": s["subject"],
        "description": s.get("description", ""),
        "status": _extra(s, "status"),
        "status_id": s.get("status"),
        "sprint": _extra(s, "milestone"),
        "milestone_id": s.get("milestone"),
        "assigned_to": _extra(s, "assigned_to", "username"),
        "points": s.get("total_points"),
        "tags": [t[0] for t in s.get("tags", [])],
        "is_closed": s.get("is_closed", False),
        "is_blocked": s.get("is_blocked", False),
        "blocked_note": s.get("blocked_note", ""),
        "comments": _get_comments("userstory", story_id),
    }


@mcp.tool()
def create_user_story(project_id: int, subject: str, description: str = None,
                      status_id: int = None, milestone_id: int = None,
                      assigned_to: int = None, tags: list[str] = None,
                      epic_id: int = None) -> dict:
    """
    Create a new user story. Use get_project to find valid status_id values.
    Pass epic_id to link the story to an epic immediately after creation.
    """
    data: dict = {"project": project_id, "subject": subject}
    if description is not None: data["description"] = description
    if status_id is not None: data["status"] = status_id
    if milestone_id is not None: data["milestone"] = milestone_id
    if assigned_to is not None: data["assigned_to"] = assigned_to
    if tags is not None: data["tags"] = [[t, None] for t in tags]
    result = _post("/userstories", data)
    if epic_id is not None:
        _post(f"/epics/{epic_id}/related_userstories", {"user_story": result["id"], "epic": epic_id})
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"]}


@mcp.tool()
def add_story_to_epic(story_id: int, epic_id: int) -> dict:
    """Link an existing user story to an epic."""
    _post(f"/epics/{epic_id}/related_userstories", {"user_story": story_id, "epic": epic_id})
    return {"story_id": story_id, "epic_id": epic_id, "linked": True}


@mcp.tool()
def update_user_story(story_id: int, subject: str = None, description: str = None,
                      status_id: int = None, milestone_id: int = None,
                      clear_sprint: bool = False, assigned_to: int = None,
                      tags: list[str] = None, is_blocked: bool = None,
                      blocked_note: str = None) -> dict:
    """
    Update a user story. Only provided fields are changed.
    Pass clear_sprint=True to move the story back to the backlog.
    Pass is_blocked=True and blocked_note to mark a story as blocked.
    """
    data: dict = {}
    if subject is not None: data["subject"] = subject
    if description is not None: data["description"] = description
    if status_id is not None: data["status"] = status_id
    if clear_sprint:
        data["milestone"] = None
    elif milestone_id is not None:
        data["milestone"] = milestone_id
    if assigned_to is not None: data["assigned_to"] = assigned_to
    if tags is not None: data["tags"] = [[t, None] for t in tags]
    if is_blocked is not None: data["is_blocked"] = is_blocked
    if blocked_note is not None: data["blocked_note"] = blocked_note
    result = _versioned_patch(f"/userstories/{story_id}", data)
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"],
            "status": _extra(result, "status")}


@mcp.tool()
def add_comment(comment: str, prefix_ref: str = None, item_type: str = None,
                item_id: int = None) -> dict:
    """Add a comment to a user story, issue, or task.
    Either supply prefix_ref (e.g. 'HOME-42') for a user story,
    or supply item_type ('userstory'|'issue'|'task') and item_id.
    """
    _TYPE_PATH = {"userstory": "userstories", "issue": "issues", "task": "tasks"}
    if prefix_ref is not None:
        ctx = _fetch_story_context(prefix_ref)
        item_id = ctx["match"]["id"]
        path = f"/userstories/{item_id}"
    elif item_type is not None and item_id is not None:
        path_segment = _TYPE_PATH.get(item_type)
        if not path_segment:
            raise ValueError(f"Unknown item_type '{item_type}'. Use one of: {list(_TYPE_PATH)}")
        path = f"/{path_segment}/{item_id}"
    else:
        raise ValueError("Provide either prefix_ref or both item_type and item_id")
    _versioned_patch(path, {"comment": comment})
    return {"comment_added": True, "item_id": item_id}


# ── Issues ────────────────────────────────────────────────────────────────────

def _issue_full(i: dict) -> dict:
    return {
        "id": i["id"],
        "ref": i["ref"],
        "subject": i["subject"],
        "status": _extra(i, "status"),
        "status_id": i.get("status"),
        "type": _extra(i, "type"),
        "priority": _extra(i, "priority"),
        "severity": _extra(i, "severity"),
        "assigned_to": _extra(i, "assigned_to", "username"),
        "tags": [t[0] for t in i.get("tags", [])],
        "is_closed": i.get("is_closed", False),
    }


def _issue_summary(i: dict) -> dict:
    return {"ref": i["ref"], "subject": i["subject"], "status": _extra(i, "status")}


@mcp.tool()
def list_issues(project_id: int, status_id: int = None, type_id: int = None,
                priority_id: int = None, include_closed: bool = False,
                summary: bool = False, page: int = None,
                page_size: int = _DEFAULT_PAGE_SIZE) -> dict:
    """
    List issues in a project, optionally filtered by status, type, or priority.

    Defaults to open (non-closed) issues only — pass include_closed=True to
    include closed ones too. Pass summary=True for a minimal
    {ref, subject, status} shape. Pass page/page_size to walk through results
    using Taiga's native pagination instead of fetching everything at once.

    With no page given, very large result sets auto-downgrade to summary
    shape, and extremely large ones are refused outright with a count and a
    suggestion to page — see the "note" field in the response when that
    happens.

    Returns {items, returned_count, total_count, page, page_size, has_more,
    summary, note?}.
    """
    return _list_with_guard(
        "/issues", _issue_full, _issue_summary,
        {"project": project_id, "status": status_id, "type": type_id, "priority": priority_id},
        include_closed=include_closed, summary=summary, page=page, page_size=page_size,
    )


@mcp.tool()
def get_issue(issue_id: int) -> dict:
    """Get full details of an issue including description."""
    i = _get(f"/issues/{issue_id}")
    return {
        "id": i["id"],
        "ref": i["ref"],
        "subject": i["subject"],
        "description": i.get("description", ""),
        "status": _extra(i, "status"),
        "status_id": i.get("status"),
        "type": _extra(i, "type"),
        "priority": _extra(i, "priority"),
        "severity": _extra(i, "severity"),
        "assigned_to": _extra(i, "assigned_to", "username"),
        "tags": [t[0] for t in i.get("tags", [])],
    }


@mcp.tool()
def create_issue(project_id: int, subject: str, description: str = None,
                 type_id: int = None, priority_id: int = None,
                 severity_id: int = None, status_id: int = None,
                 assigned_to: int = None, tags: list[str] = None) -> dict:
    """
    Create a new issue. Use get_project to find valid type_id, priority_id,
    severity_id, and status_id values.
    """
    data: dict = {"project": project_id, "subject": subject}
    if description is not None: data["description"] = description
    if type_id is not None: data["type"] = type_id
    if priority_id is not None: data["priority"] = priority_id
    if severity_id is not None: data["severity"] = severity_id
    if status_id is not None: data["status"] = status_id
    if assigned_to is not None: data["assigned_to"] = assigned_to
    if tags is not None: data["tags"] = [[t, None] for t in tags]
    result = _post("/issues", data)
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"]}


@mcp.tool()
def update_issue(issue_id: int, subject: str = None, description: str = None,
                 status_id: int = None, type_id: int = None,
                 priority_id: int = None, assigned_to: int = None) -> dict:
    """Update an issue. Only provided fields are changed."""
    data: dict = {}
    if subject is not None: data["subject"] = subject
    if description is not None: data["description"] = description
    if status_id is not None: data["status"] = status_id
    if type_id is not None: data["type"] = type_id
    if priority_id is not None: data["priority"] = priority_id
    if assigned_to is not None: data["assigned_to"] = assigned_to
    result = _versioned_patch(f"/issues/{issue_id}", data)
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"],
            "status": _extra(result, "status")}


# ── Tasks ─────────────────────────────────────────────────────────────────────

def _task_full(t: dict) -> dict:
    return {
        "id": t["id"],
        "ref": t["ref"],
        "subject": t["subject"],
        "status": _extra(t, "status"),
        "status_id": t.get("status"),
        "user_story_id": t.get("user_story"),
        "assigned_to": _extra(t, "assigned_to", "username"),
        "is_closed": t.get("is_closed", False),
    }


def _task_summary(t: dict) -> dict:
    return {"ref": t["ref"], "subject": t["subject"], "status": _extra(t, "status")}


@mcp.tool()
def list_tasks(project_id: int, milestone_id: int = None,
               user_story_id: int = None, include_closed: bool = False,
               summary: bool = False, page: int = None,
               page_size: int = _DEFAULT_PAGE_SIZE) -> dict:
    """
    List tasks in a project, optionally filtered by sprint or parent user story.

    Defaults to open (non-closed) tasks only — pass include_closed=True to
    include closed ones too. Pass summary=True for a minimal
    {ref, subject, status} shape. Pass page/page_size to walk through results
    using Taiga's native pagination instead of fetching everything at once.

    With no page given, very large result sets auto-downgrade to summary
    shape, and extremely large ones are refused outright with a count and a
    suggestion to page — see the "note" field in the response when that
    happens.

    Returns {items, returned_count, total_count, page, page_size, has_more,
    summary, note?}.
    """
    return _list_with_guard(
        "/tasks", _task_full, _task_summary,
        {"project": project_id, "milestone": milestone_id, "user_story": user_story_id},
        include_closed=include_closed, summary=summary, page=page, page_size=page_size,
    )


@mcp.tool()
def create_task(project_id: int, subject: str, user_story_id: int = None,
                milestone_id: int = None, status_id: int = None,
                assigned_to: int = None) -> dict:
    """Create a task, optionally linked to a user story and/or sprint."""
    data: dict = {"project": project_id, "subject": subject}
    if user_story_id is not None: data["user_story"] = user_story_id
    if milestone_id is not None: data["milestone"] = milestone_id
    if status_id is not None: data["status"] = status_id
    if assigned_to is not None: data["assigned_to"] = assigned_to
    result = _post("/tasks", data)
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"]}


@mcp.tool()
def get_task(task_id: int) -> dict:
    """Get full details of a task including description."""
    t = _get(f"/tasks/{task_id}")
    return {
        "id": t["id"],
        "ref": t["ref"],
        "subject": t["subject"],
        "description": t.get("description", ""),
        "status": _extra(t, "status"),
        "status_id": t.get("status"),
        "user_story_id": t.get("user_story"),
        "assigned_to": _extra(t, "assigned_to", "username"),
        "is_closed": t.get("is_closed", False),
    }


@mcp.tool()
def update_task(task_id: int, subject: str = None, description: str = None,
                status_id: int = None, assigned_to: int = None) -> dict:
    """Update a task. Only provided fields are changed."""
    data: dict = {}
    if subject is not None: data["subject"] = subject
    if description is not None: data["description"] = description
    if status_id is not None: data["status"] = status_id
    if assigned_to is not None: data["assigned_to"] = assigned_to
    result = _versioned_patch(f"/tasks/{task_id}", data)
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"],
            "status": _extra(result, "status")}


# ── Epics ─────────────────────────────────────────────────────────────────────

def _epic_full(e: dict) -> dict:
    return {
        "id": e["id"],
        "ref": e["ref"],
        "subject": e["subject"],
        "status": _extra(e, "status"),
        "status_id": e.get("status"),
        "color": e.get("color", ""),
        "assigned_to": _extra(e, "assigned_to", "username"),
        "is_closed": e.get("is_closed", False),
    }


def _epic_summary(e: dict) -> dict:
    return {"ref": e["ref"], "subject": e["subject"], "status": _extra(e, "status")}


@mcp.tool()
def list_epics(project_id: int, include_closed: bool = False, summary: bool = False,
               page: int = None, page_size: int = _DEFAULT_PAGE_SIZE) -> dict:
    """
    List all epics in a project.

    Defaults to open (non-closed) epics only — pass include_closed=True to
    include closed ones too. Pass summary=True for a minimal
    {ref, subject, status} shape. Pass page/page_size to walk through results
    using Taiga's native pagination instead of fetching everything at once.

    With no page given, very large result sets auto-downgrade to summary
    shape, and extremely large ones are refused outright with a count and a
    suggestion to page — see the "note" field in the response when that
    happens.

    Returns {items, returned_count, total_count, page, page_size, has_more,
    summary, note?}.
    """
    return _list_with_guard(
        "/epics", _epic_full, _epic_summary,
        {"project": project_id},
        include_closed=include_closed, summary=summary, page=page, page_size=page_size,
    )


@mcp.tool()
def create_epic(project_id: int, subject: str, description: str = None,
                color: str = None) -> dict:
    """Create an epic. color should be a CSS hex string e.g. '#e44057'."""
    data: dict = {"project": project_id, "subject": subject}
    if description is not None: data["description"] = description
    if color is not None: data["color"] = color
    result = _post("/epics", data)
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"]}


# ── Wiki ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_wiki_pages(project_id: int) -> list[dict]:
    """List all wiki pages in a project."""
    pages = _get("/wiki", project=project_id)
    return [{"id": p["id"], "slug": p["slug"]} for p in pages]


@mcp.tool()
def get_wiki_page(project_id: int, slug: str) -> dict:
    """Get the Markdown content of a wiki page by its slug."""
    page = _get("/wiki/by_slug", slug=slug, project=project_id)
    return {"id": page["id"], "slug": page["slug"], "content": page.get("content", "")}


@mcp.tool()
def create_wiki_page(project_id: int, slug: str, content: str) -> dict:
    """Create a new wiki page. slug should be lowercase-kebab-case."""
    page = _post("/wiki", {"project": project_id, "slug": slug, "content": content})
    return {"id": page["id"], "slug": page["slug"]}


@mcp.tool()
def update_wiki_page(project_id: int, slug: str, content: str) -> dict:
    """Update an existing wiki page by slug."""
    existing = _get("/wiki/by_slug", slug=slug, project=project_id)
    page = _patch(f"/wiki/{existing['id']}", {"content": content, "version": existing["version"]})
    return {"id": page["id"], "slug": page["slug"]}


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
