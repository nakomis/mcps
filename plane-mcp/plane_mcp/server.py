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


# ── Lookups (cached per process; projects and their states change rarely) ─────

_cache: dict = {}


def _projects(include_archived: bool = False) -> list[dict]:
    if "projects" not in _cache:
        _cache["projects"] = _get_all("/projects/")
    return [p for p in _cache["projects"] if include_archived or not p.get("archived_at")]


def _project(identifier: str) -> dict:
    ident = identifier.strip().upper()
    for refresh in (False, True):
        if refresh:
            _cache.clear()
        p = next((p for p in _projects(include_archived=True) if p["identifier"] == ident), None)
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
def list_projects(include_archived: bool = False) -> list[dict]:
    """List Plane projects: identifier (the ref prefix, e.g. HOME), name, and whether archived."""
    return [{"identifier": p["identifier"], "name": p["name"], "archived": bool(p.get("archived_at")),
             "description": (p.get("description") or "")[:200]}
            for p in sorted(_projects(include_archived), key=lambda p: p["identifier"])]


@mcp.tool()
def get_project(identifier: str) -> dict:
    """Get a project's states (with their group), labels, and members. Use the
    names from here in create_work_item / update_work_item."""
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
    Martin (martin@nakomis.com) and plane@nakomis.com are added as admins."""
    ident = identifier.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{1,12}", ident):
        raise ValueError("identifier must be 1-12 letters or digits")
    if re.search(r"[&+,:;$^}{*=?@#|'<>.()%!-]", name):
        raise ValueError("Plane rejects project names containing - . & + , : ; $ ^ { } * = ? @ # | ' < > ( ) % !")
    p = _request("POST", "/projects/", json={
        "name": name, "identifier": ident, "description": description, "network": 0,
        "module_view": True, "cycle_view": True, "issue_views_view": True, "page_view": True,
    })
    _cache.clear()
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
    return get_project(ident)


# ── Work items ────────────────────────────────────────────────────────────────

@mcp.tool()
def list_work_items(project: str, state: str = None, label: str = None, assignee: str = None,
                    include_closed: bool = False, summary: bool = True, limit: int = 200) -> dict:
    """
    List work items in a project (e.g. project="HOME").

    Defaults to open items (state group not completed/cancelled), summary shape
    {ref, name, state, state_group}. Filter by state name, label name, or
    assignee (email, display name, or "me"). Pass summary=False for labels,
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


def main():
    mcp.run()


if __name__ == "__main__":
    main()
