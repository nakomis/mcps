#!/usr/bin/env python3
"""Plane MCP Server — read/write access to the self-hosted Plane at plane.home.nakomis.com.

Replaces taiga-mcp (HOME-377). Work items are addressed the way people write
them, PROJECT-N (e.g. HOME-42): migrated items kept their Taiga numbers, so a
ref from an old commit or conversation resolves to the same item here.

Plane stores descriptions and comments as HTML. Tools take markdown in and give
markdown back, so callers never handle HTML.
"""

import os
import re
import time

import logging

import boto3
import httpx
import markdown as md
from markdownify import markdownify
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("plane-mcp")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per request otherwise

CLOSED_GROUPS = {"completed", "cancelled"}


# ── Config & HTTP ─────────────────────────────────────────────────────────────

def _root() -> str:
    url = os.environ.get("PLANE_URL", "https://plane.home.nakomis.com").rstrip("/")
    return f"{url}/api/v1/workspaces/{_workspace()}"


def _workspace() -> str:
    return os.environ.get("PLANE_WORKSPACE", "nakomis")


def _web(path: str) -> str:
    return os.environ.get("PLANE_URL", "https://plane.home.nakomis.com").rstrip("/") + f"/{_workspace()}{path}"


# Leia's nginx requires a client certificate (mTLS) for *.home.nakomis.com.
def _make_client() -> httpx.Client:
    cert, key = os.environ.get("PLANE_CLIENT_CERT"), os.environ.get("PLANE_CLIENT_KEY")
    token = os.environ.get("PLANE_API_KEY", "")
    return httpx.Client(
        cert=(cert, key) if cert and key else cert or None,
        headers={"X-API-Key": token, "Content-Type": "application/json"},
        timeout=30,
    )


_client = _make_client()


def _request(method: str, path: str, *, params: dict = None, json: dict = None):
    if not os.environ.get("PLANE_API_KEY"):
        raise RuntimeError("PLANE_API_KEY must be set (plane-mcp-launch.sh reads it from the Keychain)")
    params = {k: v for k, v in (params or {}).items() if v is not None}
    for attempt in range(5):
        r = _client.request(method, _root() + path, params=params, json=json)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(int(r.headers.get("Retry-After") or 2 ** attempt))
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content.strip() else None
    raise RuntimeError(f"{method} {path}: gave up after retries ({r.status_code})")


def _get(path: str, **params):
    return _request("GET", path, params=params)


def _get_all(path: str, **params) -> list:
    """GET a list endpoint, following Plane's cursor pagination."""
    params = {"per_page": 1000, **params}
    out = []
    while True:
        page = _get(path, **params)
        if isinstance(page, list):
            return page
        out.extend(page.get("results", []))
        if not page.get("next_page_results"):
            return out
        params["cursor"] = page["next_cursor"]


def _html(text: str | None) -> str | None:
    return None if text is None else (md.markdown(text, extensions=["fenced_code", "tables", "sane_lists"]) if text.strip() else "")


def _text(html: str | None) -> str:
    return markdownify(html or "", heading_style="ATX").strip()


# ── Lookups (cached briefly; projects and their states change rarely) ─────────

# Claude sessions live for days, so the cache expires rather than lasting the
# whole process: a project created from another session shows up within this
# many seconds, or at once with refresh=True.
CACHE_TTL = int(os.environ.get("PLANE_CACHE_TTL", "300"))

_cache: dict = {}
_cache_loaded_at = 0.0


def _refresh_if_stale(force: bool = False) -> None:
    global _cache_loaded_at
    if force or time.monotonic() - _cache_loaded_at > CACHE_TTL:
        _cache.clear()
        _cache_loaded_at = time.monotonic()


def _projects(include_archived: bool = False, refresh: bool = False) -> list[dict]:
    _refresh_if_stale(refresh)
    if "projects" not in _cache:
        _cache["projects"] = _get_all("/projects/")
    return [p for p in _cache["projects"] if include_archived or not p.get("archived_at")]


def _project(identifier: str) -> dict:
    ident = identifier.strip().upper()
    for refresh in (False, True):
        p = next((p for p in _projects(include_archived=True, refresh=refresh) if p["identifier"] == ident), None)
        if p:
            return p
    known = sorted(p["identifier"] for p in _projects())
    raise ValueError(f"No project '{ident}'. Active projects: {known}")


def _project_meta(p: dict) -> dict:
    key = f"meta:{p['id']}"
    if key not in _cache:
        states = _get_all(f"/projects/{p['id']}/states/")
        labels = _get_all(f"/projects/{p['id']}/labels/")
        members = _get_all(f"/projects/{p['id']}/members/")
        _cache[key] = {
            "states": sorted(states, key=lambda s: s.get("sequence", 0)),
            "labels": labels,
            "members": [m.get("member", m) if isinstance(m.get("member"), dict) else m for m in members],
        }
    return _cache[key]


def _me() -> dict:
    if "me" not in _cache:
        r = _client.get(os.environ.get("PLANE_URL", "https://plane.home.nakomis.com").rstrip("/") + "/api/v1/users/me/")
        r.raise_for_status()
        _cache["me"] = r.json()
    return _cache["me"]


def _split_ref(ref: str) -> tuple[str, int]:
    m = re.fullmatch(r"\s*([A-Za-z0-9]+)-(\d+)\s*", ref or "")
    if not m:
        raise ValueError(f"Expected PROJECT-N (e.g. HOME-42), got: {ref!r}")
    return m.group(1).upper(), int(m.group(2))


def _state_id(p: dict, name: str) -> str:
    states = _project_meta(p)["states"]
    s = next((s for s in states if s["name"].lower() == name.lower()), None)
    if not s:
        raise ValueError(f"No state '{name}' in {p['identifier']}. States: {[x['name'] for x in states]}")
    return s["id"]


def _label_ids(p: dict, names: list[str], create: bool = True) -> list[str]:
    labels = _project_meta(p)["labels"]
    out = []
    for name in names:
        lab = next((l for l in labels if l["name"].lower() == name.lower()), None)
        if lab is None:
            if not create:
                raise ValueError(f"No label '{name}' in {p['identifier']}")
            lab = _request("POST", f"/projects/{p['id']}/labels/", json={"name": name})
            labels.append(lab)
        out.append(lab["id"])
    return out


def _user_id(p: dict, who: str) -> str:
    """Accepts an email, a display name, 'me', or a user id."""
    if who == "me":
        return _me()["id"]
    for m in _project_meta(p)["members"]:
        if who in (m.get("id"), m.get("email"), m.get("display_name")) or \
                who.lower() in {(m.get("email") or "").lower(), (m.get("display_name") or "").lower()}:
            return m["id"]
    raise ValueError(f"No member '{who}' in {p['identifier']}")


def _format_item(p: dict, i: dict, *, full: bool = True) -> dict:
    meta = _project_meta(p)
    state = next((s for s in meta["states"] if s["id"] == i.get("state")), {})
    labels = {l["id"]: l["name"] for l in meta["labels"]}
    members = {m["id"]: m.get("display_name") or m.get("email") for m in meta["members"]}
    out = {
        "ref": f"{p['identifier']}-{i['sequence_id']}",
        "name": i["name"],
        "state": state.get("name"),
        "state_group": state.get("group"),
    }
    if full:
        out.update({
            "id": i["id"],
            "priority": i.get("priority"),
            "labels": sorted(labels.get(l, l) for l in i.get("labels", [])),
            "assignees": [members.get(a, a) for a in i.get("assignees", [])],
            "parent_id": i.get("parent"),
            "start_date": i.get("start_date"),
            "target_date": i.get("target_date"),
            "completed_at": i.get("completed_at"),
            "archived": bool(i.get("archived_at")),
            "created_at": i.get("created_at"),
            "url": _web(f"/browse/{p['identifier']}-{i['sequence_id']}/"),
        })
    return out


def _item_by_ref(ref: str) -> tuple[dict, dict]:
    ident, seq = _split_ref(ref)
    p = _project(ident)
    return p, _get(f"/work-items/{ident}-{seq}/")


def _comments(p: dict, item_id: str) -> list[dict]:
    members = {m["id"]: m.get("display_name") or m.get("email") for m in _project_meta(p)["members"]}
    rows = _get_all(f"/projects/{p['id']}/work-items/{item_id}/comments/")
    return [{"author": members.get(c.get("actor") or c.get("created_by"), c.get("created_by")),
             "created_at": c.get("created_at"),
             "comment": _text(c.get("comment_html"))}
            for c in sorted(rows, key=lambda c: c.get("created_at") or "")]


def _story(p: dict, item: dict) -> dict:
    meta = _project_meta(p)
    story = _format_item(p, item)
    story["description"] = _text(item.get("description_html"))
    story["comments"] = _comments(p, item["id"])
    story["modules"] = _item_module_names(p, item["id"])
    if item.get("parent"):
        parent = _get(f"/projects/{p['id']}/work-items/{item['parent']}/")
        story["parent"] = f"{p['identifier']}-{parent['sequence_id']} {parent['name']}"
    return {
        "work_item": story,
        "project": {
            "identifier": p["identifier"],
            "name": p["name"],
            "states": [{"name": s["name"], "group": s["group"]} for s in meta["states"]],
            "members": [{"display_name": m.get("display_name"), "email": m.get("email")} for m in meta["members"]],
        },
    }


# ── Projects ──────────────────────────────────────────────────────────────────

@mcp.tool()
def list_projects(include_archived: bool = False, refresh: bool = False) -> list[dict]:
    """List Plane projects: identifier (the ref prefix, e.g. HOME), name, and whether archived.
    The list is cached for a few minutes; refresh=True re-reads it now."""
    return [{"identifier": p["identifier"], "name": p["name"], "archived": bool(p.get("archived_at")),
             "description": (p.get("description") or "")[:200]}
            for p in sorted(_projects(include_archived, refresh), key=lambda p: p["identifier"])]


@mcp.tool()
def get_project(identifier: str, refresh: bool = False) -> dict:
    """Get a project's states (with their group), labels, and members. Use the
    names from here in create_work_item / update_work_item. Cached for a few
    minutes; refresh=True re-reads it now."""
    _refresh_if_stale(refresh)
    p = _project(identifier)
    meta = _project_meta(p)
    return {
        "identifier": p["identifier"], "name": p["name"], "description": p.get("description", ""),
        "archived": bool(p.get("archived_at")),
        "states": [{"name": s["name"], "group": s["group"], "default": s.get("default", False)} for s in meta["states"]],
        "labels": sorted(l["name"] for l in meta["labels"]),
        "members": [{"display_name": m.get("display_name"), "email": m.get("email")} for m in meta["members"]],
        "url": _web(f"/projects/{p['id']}/issues/"),
    }


@mcp.tool()
def create_project(name: str, identifier: str, description: str = "") -> dict:
    """Create a project. identifier is the ref prefix: up to 12 letters/digits,
    e.g. HOME. Plane rejects names containing - . & + , : ; $ ^ { } * = ? @ # | ' < > ( ) % !
    Martin (martin@nakomis.com) and plane@nakomis.com are added as admins, and
    the project is registered with the nakom.is shortener (see
    sync_shortener_projects). A failed registration is reported under
    "shortener" rather than failing the call."""
    ident = identifier.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}", ident):
        raise ValueError("identifier must be 1-12 letters or digits")
    if re.search(r"[&+,:;$^}{*=?@#|'<>.()%!-]", name):
        raise ValueError("Plane rejects project names containing - . & + , : ; $ ^ { } * = ? @ # | ' < > ( ) % !")
    p = _request("POST", "/projects/", json={
        "name": name, "identifier": ident, "description": description, "network": 0,
        "module_view": True, "cycle_view": True, "issue_views_view": True, "page_view": True,
    })
    _refresh_if_stale(force=True)
    members = {m["member"]["email"] if isinstance(m.get("member"), dict) else m.get("email"): m
               for m in _get_all("/members/")}
    for email in ("martin@nakomis.com", "plane@nakomis.com"):
        m = members.get(email)
        if m:
            uid = m["member"]["id"] if isinstance(m.get("member"), dict) else m["id"]
            try:
                _request("POST", f"/projects/{p['id']}/project-members/", json={"member": uid, "role": 20})
            except RuntimeError:
                pass  # already a member (e.g. the creator)
    result = get_project(ident)
    result["shortener"] = _sync_shortener_project(_project(ident))
    return result


@mcp.tool()
def sync_shortener_projects() -> dict:
    """Register every Plane project (archived ones included) with the nakom.is
    shortener, so nakom.is/plane/<identifier> and nakom.is/plane/<identifier> <n>
    work. create_project does this for new projects; use this to backfill, or
    to recover from a failed registration. Upserts only and never deletes, so
    hand-added aliases (e.g. nako → NAKIS) are left alone."""
    projects = sorted(_projects(include_archived=True, refresh=True), key=lambda p: p["identifier"])
    results = [_sync_shortener_project(p) for p in projects]
    return {
        "table": SHORTENER_TABLE,
        "synced": [r["alias"] for r in results if r["synced"]],
        "failed": [r for r in results if not r["synced"]],
    }


# ── nakom.is shortener ────────────────────────────────────────────────────────
#
# nakom.is/plane/<alias> [<ref>] redirects to a project's work items page, or to
# one of its work items, from a row per project in the ticket-projects DynamoDB
# table. The shortener's Lambda never talks to Plane, so this MCP keeps that
# table in step. It authenticates through IAM Roles Anywhere (AWS profile
# plane-mcp, client certificate CN plane-mcp); the role may only UpdateItem.

SHORTENER_TABLE = os.environ.get("SHORTENER_TABLE", "ticket-projects")

_aws: dict = {}


def _dynamodb():
    if "dynamodb" not in _aws:
        session = boto3.Session(profile_name=os.environ.get("SHORTENER_AWS_PROFILE", "plane-mcp"),
                                region_name=os.environ.get("SHORTENER_AWS_REGION", "eu-west-2"))
        _aws["dynamodb"] = session.client("dynamodb")
    return _aws["dynamodb"]


def _sync_shortener_project(p: dict) -> dict:
    """Upsert one project's row. UpdateItem rather than PutItem, so any other
    attributes on the row survive."""
    alias = p["identifier"].lower()
    try:
        _dynamodb().update_item(
            TableName=SHORTENER_TABLE,
            Key={"alias": {"S": alias}},
            UpdateExpression="SET urlTemplate = :t, projectUrl = :p",
            ExpressionAttributeValues={
                ":t": {"S": _web(f"/browse/{p['identifier']}-{{ref}}/")},
                ":p": {"S": _web(f"/projects/{p['id']}/issues/")},
            },
        )
        return {"alias": alias, "synced": True}
    except Exception as e:  # never fail a Plane operation over the shortener
        return {"alias": alias, "synced": False, "error": f"{type(e).__name__}: {e}"}


# ── Work items ────────────────────────────────────────────────────────────────

@mcp.tool()
def list_work_items(project: str, state: str = None, label: str = None, assignee: str = None,
                    include_closed: bool = False, summary: bool = True, limit: int = 200,
                    module: str = None) -> dict:
    """
    List work items in a project (e.g. project="HOME").

    Defaults to open items (state group not completed/cancelled), summary shape
    {ref, name, state, state_group}. Filter by state name, label name, or
    assignee (email, display name, or "me"), or module (name or id). Pass summary=False for labels,
    assignees, dates and URL. Epics are items labelled "epic". Archived items
    aren't listed; get_story still fetches one by ref.
    """
    p = _project(project)
    items = _get_all(f"/projects/{p['id']}/work-items/")
    meta = _project_meta(p)
    groups = {s["id"]: s["group"] for s in meta["states"]}
    if not include_closed:
        items = [i for i in items if groups.get(i.get("state")) not in CLOSED_GROUPS]
    if state:
        sid = _state_id(p, state)
        items = [i for i in items if i.get("state") == sid]
    if label:
        lid = set(_label_ids(p, [label], create=False))
        items = [i for i in items if lid & set(i.get("labels", []))]
    if assignee:
        uid = _user_id(p, assignee)
        items = [i for i in items if uid in i.get("assignees", [])]
    if module:
        in_module = {i["id"] for i in _module_items(p, _module(p, module))}
        items = [i for i in items if i["id"] in in_module]
    items.sort(key=lambda i: i["sequence_id"])
    return {"project": p["identifier"], "total_count": len(items), "returned_count": min(len(items), limit),
            "items": [_format_item(p, i, full=not summary) for i in items[:limit]]}


@mcp.tool()
def search_work_items(query: str, project: str = None) -> list[dict]:
    """Search work items by text across the workspace (or one project)."""
    params = {"search": query}
    if project:
        params["project_id"] = _project(project)["id"]
    res = _get("/work-items/search/", **params)
    rows = res.get("issues", res) if isinstance(res, dict) else res
    return [{"ref": f"{r.get('project__identifier')}-{r.get('sequence_id')}", "name": r.get("name"),
             "project": r.get("project__identifier")} for r in rows]


@mcp.tool()
def get_story(ref: str) -> dict:
    """
    Fetch a work item by reference (e.g. HOME-42). Read-only.
    Returns the item (description and comments as markdown, labels, assignees,
    parent) plus project context (states, members). Refs from Taiga still work:
    migrated items kept their numbers.
    Use pick_up_story instead when you are about to start working on it.
    """
    p, item = _item_by_ref(ref)
    return _story(p, item)


@mcp.tool()
def pick_up_story(ref: str, assign_to_me: bool = True) -> dict:
    """
    Start work on an item: moves it to "In progress" (or the project's first
    started state), assigns it to the authenticated user (Claude) unless
    assign_to_me=False, and returns the full item + project context.
    """
    p, item = _item_by_ref(ref)
    states = _project_meta(p)["states"]
    started = [s for s in states if s["group"] == "started"]
    target = next((s for s in started if "progress" in s["name"].lower()), started[0] if started else None)
    update = {}
    if target and item.get("state") != target["id"]:
        update["state"] = target["id"]
    if assign_to_me and _me()["id"] not in item.get("assignees", []):
        update["assignees"] = list(item.get("assignees", [])) + [_me()["id"]]
    if update:
        item = _request("PATCH", f"/projects/{p['id']}/work-items/{item['id']}/", json=update)
    return _story(p, item)


@mcp.tool()
def create_work_item(project: str, name: str, description: str = None, state: str = None,
                     labels: list[str] = None, assignees: list[str] = None, priority: str = None,
                     parent: str = None, epic: bool = False) -> dict:
    """
    Create a work item. description is markdown. state is a state name (default:
    the project's default state). labels are names (created if missing).
    assignees are emails, display names or "me". priority: urgent|high|medium|low|none.
    parent is a ref (e.g. HOME-372) to make this a sub-item. epic=True adds the
    "epic" label (Plane CE has no epic type; migrated epics are labelled items).
    Returns the new item, including its ref.
    """
    p = _project(project)
    body = {"name": name}
    if description is not None:
        body["description_html"] = _html(description)
    if state:
        body["state"] = _state_id(p, state)
    names = list(labels or []) + (["epic"] if epic else [])
    if names:
        body["labels"] = _label_ids(p, names)
    if assignees:
        body["assignees"] = [_user_id(p, a) for a in assignees]
    if priority:
        body["priority"] = priority
    if parent:
        body["parent"] = _item_by_ref(parent)[1]["id"]
    item = _request("POST", f"/projects/{p['id']}/work-items/", json=body)
    return _format_item(p, item)


@mcp.tool()
def update_work_item(ref: str, name: str = None, description: str = None, state: str = None,
                     add_labels: list[str] = None, remove_labels: list[str] = None,
                     assignees: list[str] = None, priority: str = None, parent: str = None,
                     clear_parent: bool = False, target_date: str = None) -> dict:
    """
    Update a work item by ref (e.g. HOME-42). Only provided fields change.
    description is markdown and replaces the existing one. state is a state
    name, e.g. "Ready for test" or "Done". assignees replaces the list (emails,
    display names or "me"). target_date is YYYY-MM-DD.
    """
    p, item = _item_by_ref(ref)
    body = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description_html"] = _html(description)
    if state:
        body["state"] = _state_id(p, state)
    if add_labels or remove_labels:
        current = set(item.get("labels", []))
        current |= set(_label_ids(p, add_labels or []))
        current -= set(_label_ids(p, remove_labels or [], create=False)) if remove_labels else set()
        body["labels"] = sorted(current)
    if assignees is not None:
        body["assignees"] = [_user_id(p, a) for a in assignees]
    if priority:
        body["priority"] = priority
    if parent:
        body["parent"] = _item_by_ref(parent)[1]["id"]
    if clear_parent:
        body["parent"] = None
    if target_date:
        body["target_date"] = target_date
    if not body:
        return _format_item(p, item)
    item = _request("PATCH", f"/projects/{p['id']}/work-items/{item['id']}/", json=body)
    return _format_item(p, item)


@mcp.tool()
def add_comment(ref: str, comment: str) -> dict:
    """Add a comment (markdown) to a work item by ref (e.g. HOME-42)."""
    p, item = _item_by_ref(ref)
    c = _request("POST", f"/projects/{p['id']}/work-items/{item['id']}/comments/",
                 json={"comment_html": _html(comment)})
    return {"ref": f"{p['identifier']}-{item['sequence_id']}", "comment_id": c["id"], "created_at": c.get("created_at")}


@mcp.tool()
def list_epics(project: str, include_closed: bool = False) -> dict:
    """List a project's epics (work items labelled "epic"), with their sub-item counts."""
    p = _project(project)
    items = _get_all(f"/projects/{p['id']}/work-items/")
    meta = _project_meta(p)
    epic = next((l["id"] for l in meta["labels"] if l["name"] == "epic"), None)
    groups = {s["id"]: s["group"] for s in meta["states"]}
    epics = [i for i in items if epic in i.get("labels", [])
             and (include_closed or groups.get(i.get("state")) not in CLOSED_GROUPS)]
    children: dict = {}
    for i in items:
        if i.get("parent"):
            children[i["parent"]] = children.get(i["parent"], 0) + 1
    return {"project": p["identifier"],
            "epics": [{**_format_item(p, e, full=False), "sub_items": children.get(e["id"], 0)}
                      for e in sorted(epics, key=lambda i: i["sequence_id"])]}


@mcp.tool()
def link_work_items(ref: str, url: str, title: str = None) -> dict:
    """Attach a link (e.g. a GitHub PR URL) to a work item by ref."""
    p, item = _item_by_ref(ref)
    body = {"url": url}
    if title:
        body["title"] = title
    link = _request("POST", f"/projects/{p['id']}/work-items/{item['id']}/links/", json=body)
    return {"ref": f"{p['identifier']}-{item['sequence_id']}", "link_id": link["id"], "url": url}


# ── Modules ───────────────────────────────────────────────────────────────────
#
# Plane modules group work items (a feature, a theme) outside the parent/sub-item
# tree. Not cached: their counts change with every state change.

MODULE_STATUSES = ("backlog", "planned", "in-progress", "paused", "completed", "cancelled")


def _modules(p: dict, include_archived: bool = False) -> list[dict]:
    rows = _get_all(f"/projects/{p['id']}/modules/")
    if include_archived:
        rows += _get_all(f"/projects/{p['id']}/archived-modules/")
    return rows


def _module(p: dict, ident: str) -> dict:
    """Find a module by id or by name (case-insensitive), archived ones included."""
    modules = _modules(p, include_archived=True)
    wanted = (ident or "").strip()
    by_id = next((m for m in modules if m["id"] == wanted), None)
    if by_id:
        return by_id
    matches = [m for m in modules if m["name"].lower() == wanted.lower()]
    if len(matches) > 1:
        raise ValueError(f"{len(matches)} modules in {p['identifier']} are named '{ident}'; use the id: {[m['id'] for m in matches]}")
    if not matches:
        raise ValueError(f"No module '{ident}' in {p['identifier']}. Modules: {sorted(m['name'] for m in modules)}")
    return matches[0]


def _module_items(p: dict, m: dict) -> list[dict]:
    return _get_all(f"/projects/{p['id']}/modules/{m['id']}/module-issues/")


def _item_module_names(p: dict, item_id: str) -> list[str]:
    """Names of the modules containing an item. Plane's work item payload doesn't
    say, so scan the (few) non-empty modules."""
    return sorted(m["name"] for m in _modules(p, include_archived=True)
                  if m.get("total_issues") != 0 and any(i["id"] == item_id for i in _module_items(p, m)))


def _format_module(p: dict, m: dict) -> dict:
    members = {x["id"]: x.get("display_name") or x.get("email") for x in _project_meta(p)["members"]}
    return {
        "id": m["id"],
        "name": m["name"],
        "status": m.get("status"),
        "lead": members.get(m.get("lead"), m.get("lead")),
        "start_date": m.get("start_date"),
        "target_date": m.get("target_date"),
        "total_issues": m.get("total_issues"),
        "completed_issues": m.get("completed_issues"),
        "archived": bool(m.get("archived_at")),
        "url": _web(f"/projects/{p['id']}/modules/{m['id']}/"),
    }


def _module_body(p: dict, name, description, status, lead, start_date, target_date) -> dict:
    body = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if status is not None:
        if status not in MODULE_STATUSES:
            raise ValueError(f"status must be one of {list(MODULE_STATUSES)}, got {status!r}")
        body["status"] = status
    if lead is not None:
        body["lead"] = _user_id(p, lead)
    if start_date is not None:
        body["start_date"] = start_date
    if target_date is not None:
        body["target_date"] = target_date
    return body


def _items_by_refs(refs: list[str]) -> tuple[dict, list[dict]]:
    """Fetch work items by ref; they must all belong to one project."""
    if not refs:
        raise ValueError("refs must not be empty")
    parsed = [_split_ref(r) for r in refs]
    projects = sorted({ident for ident, _ in parsed})
    if len(projects) > 1:
        raise ValueError(f"All refs must be in one project, got: {projects}")
    p = _project(projects[0])
    return p, [_item_by_ref(r)[1] for r in refs]


@mcp.tool()
def list_modules(project: str, include_archived: bool = False) -> dict:
    """List a project's modules (e.g. project="HOME"): name, id, status
    (backlog|planned|in-progress|paused|completed|cancelled), lead, dates, and
    work item counts (total and completed). Archived modules only with
    include_archived=True."""
    p = _project(project)
    modules = sorted(_modules(p, include_archived), key=lambda m: m["name"].lower())
    return {"project": p["identifier"], "modules": [_format_module(p, m) for m in modules]}


@mcp.tool()
def get_module(project: str, module: str, summary: bool = True) -> dict:
    """Get a module by name (case-insensitive) or id, with its work items in the
    same shape as list_work_items (summary=False for the full shape)."""
    p = _project(project)
    m = _module(p, module)
    items = sorted(_module_items(p, m), key=lambda i: i["sequence_id"])
    return {**_format_module(p, m), "description": m.get("description"),
            "work_items": [_format_item(p, i, full=not summary) for i in items]}


@mcp.tool()
def create_module(project: str, name: str, description: str = None, status: str = None,
                  lead: str = None, start_date: str = None, target_date: str = None) -> dict:
    """Create a module. status: backlog|planned|in-progress|paused|completed|cancelled
    (Plane's default is planned). lead is an email, display name or "me".
    Dates are YYYY-MM-DD. Returns the new module, including its id."""
    p = _project(project)
    body = _module_body(p, name, description, status, lead, start_date, target_date)
    return _format_module(p, _request("POST", f"/projects/{p['id']}/modules/", json=body))


@mcp.tool()
def update_module(project: str, module: str, name: str = None, description: str = None,
                  status: str = None, lead: str = None, start_date: str = None,
                  target_date: str = None) -> dict:
    """Update a module (name or id). Only provided fields change; name renames it.
    status: backlog|planned|in-progress|paused|completed|cancelled. lead is an
    email, display name or "me". Dates are YYYY-MM-DD."""
    p = _project(project)
    m = _module(p, module)
    body = _module_body(p, name, description, status, lead, start_date, target_date)
    if body:
        # Plane's PATCH response is partial (no id, no counts), so merge it over what we had
        m = {**m, **_request("PATCH", f"/projects/{p['id']}/modules/{m['id']}/", json=body)}
    return _format_module(p, m)


@mcp.tool()
def add_to_module(module: str, refs: list[str]) -> dict:
    """Add work items to a module. refs are work item refs, e.g. ["HOME-414",
    "HOME-416"]; they must all be in one project, which is where the module is
    looked up (by name or id). Items already in it are left alone."""
    p, items = _items_by_refs(refs)
    m = _module(p, module)
    present = {i["id"] for i in _module_items(p, m)}
    new = [i for i in items if i["id"] not in present]
    if new:
        _request("POST", f"/projects/{p['id']}/modules/{m['id']}/module-issues/",
                 json={"issues": [i["id"] for i in new]})
    ref = lambda i: f"{p['identifier']}-{i['sequence_id']}"
    return {"module": m["name"], "added": [ref(i) for i in new],
            "already_in_module": [ref(i) for i in items if i["id"] in present]}


@mcp.tool()
def remove_from_module(module: str, refs: list[str]) -> dict:
    """Remove work items from a module (the items themselves are untouched).
    refs are work item refs in one project; the module is a name or id."""
    p, items = _items_by_refs(refs)
    m = _module(p, module)
    present = {i["id"] for i in _module_items(p, m)}
    ref = lambda i: f"{p['identifier']}-{i['sequence_id']}"
    removed = [i for i in items if i["id"] in present]
    for i in removed:
        _request("DELETE", f"/projects/{p['id']}/modules/{m['id']}/module-issues/{i['id']}/")
    return {"module": m["name"], "removed": [ref(i) for i in removed],
            "not_in_module": [ref(i) for i in items if i["id"] not in present]}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
