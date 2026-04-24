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


def _headers() -> dict:
    token = os.environ.get("TAIGA_AUTH_TOKEN", "")
    if not token:
        raise RuntimeError("TAIGA_AUTH_TOKEN environment variable must be set")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _get(path: str, **params) -> dict | list:
    r = httpx.get(f"{_base()}{path}", headers=_headers(),
                  params={k: v for k, v in params.items() if v is not None}, timeout=10)
    r.raise_for_status()
    return r.json()


def _get_all(path: str, **params) -> list:
    """GET a list endpoint, following Taiga's page-based pagination automatically."""
    results = []
    page = 1
    while True:
        r = httpx.get(f"{_base()}{path}", headers=_headers(),
                      params={k: v for k, v in {**params, "page": page}.items()
                              if v is not None},
                      timeout=10)
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


def _post(path: str, data: dict) -> dict:
    r = httpx.post(f"{_base()}{path}", headers=_headers(), json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _patch(path: str, data: dict) -> dict:
    r = httpx.patch(f"{_base()}{path}", headers=_headers(), json=data, timeout=10)
    r.raise_for_status()
    return r.json()


def _versioned_patch(path: str, data: dict) -> dict:
    """PATCH with Taiga's required optimistic-locking version field."""
    current = _get(path)
    data["version"] = current["version"]
    return _patch(path, data)


def _delete(path: str) -> None:
    r = httpx.delete(f"{_base()}{path}", headers=_headers(), timeout=10)
    r.raise_for_status()


def _extra(obj: dict, key: str, field: str = "name") -> str | None:
    info = obj.get(f"{key}_extra_info")
    return info.get(field) if info else None


# ── Projects ──────────────────────────────────────────────────────────────────

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
        }
        for p in projects
    ]


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

@mcp.tool()
def list_user_stories(project_id: int, milestone_id: int = None,
                      status_id: int = None, epic_id: int = None) -> list[dict]:
    """
    List user stories in a project.
    Optionally filter by sprint (milestone_id), status (status_id), or epic (epic_id).
    Handles pagination automatically so all stories are returned.
    """
    stories = _get_all("/userstories", project=project_id,
                       milestone=milestone_id, status=status_id, epic=epic_id)
    return [
        {
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
        for s in stories
    ]


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


@mcp.tool()
def get_user_story(story_id: int) -> dict:
    """Get full details of a user story including description."""
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
    }


@mcp.tool()
def create_user_story(project_id: int, subject: str, description: str = None,
                      status_id: int = None, milestone_id: int = None,
                      assigned_to: int = None, tags: list[str] = None) -> dict:
    """Create a new user story. Use get_project to find valid status_id values."""
    data: dict = {"project": project_id, "subject": subject}
    if description is not None: data["description"] = description
    if status_id is not None: data["status"] = status_id
    if milestone_id is not None: data["milestone"] = milestone_id
    if assigned_to is not None: data["assigned_to"] = assigned_to
    if tags is not None: data["tags"] = [[t, None] for t in tags]
    result = _post("/userstories", data)
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"]}


@mcp.tool()
def update_user_story(story_id: int, subject: str = None, description: str = None,
                      status_id: int = None, milestone_id: int = None,
                      clear_sprint: bool = False, assigned_to: int = None,
                      tags: list[str] = None) -> dict:
    """
    Update a user story. Only provided fields are changed.
    Pass clear_sprint=True to move the story back to the backlog.
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
    result = _versioned_patch(f"/userstories/{story_id}", data)
    return {"id": result["id"], "ref": result["ref"], "subject": result["subject"],
            "status": _extra(result, "status")}


# ── Issues ────────────────────────────────────────────────────────────────────

@mcp.tool()
def list_issues(project_id: int, status_id: int = None, type_id: int = None,
                priority_id: int = None) -> list[dict]:
    """List issues in a project, optionally filtered by status, type, or priority."""
    issues = _get("/issues", project=project_id, status=status_id,
                  type=type_id, priority=priority_id)
    return [
        {
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
        for i in issues
    ]


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

@mcp.tool()
def list_tasks(project_id: int, milestone_id: int = None,
               user_story_id: int = None) -> list[dict]:
    """List tasks in a project, optionally filtered by sprint or parent user story."""
    tasks = _get("/tasks", project=project_id,
                 milestone=milestone_id, user_story=user_story_id)
    return [
        {
            "id": t["id"],
            "ref": t["ref"],
            "subject": t["subject"],
            "status": _extra(t, "status"),
            "status_id": t.get("status"),
            "user_story_id": t.get("user_story"),
            "assigned_to": _extra(t, "assigned_to", "username"),
            "is_closed": t.get("is_closed", False),
        }
        for t in tasks
    ]


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

@mcp.tool()
def list_epics(project_id: int) -> list[dict]:
    """List all epics in a project."""
    epics = _get("/epics", project=project_id)
    return [
        {
            "id": e["id"],
            "ref": e["ref"],
            "subject": e["subject"],
            "status": _extra(e, "status"),
            "color": e.get("color", ""),
            "assigned_to": _extra(e, "assigned_to", "username"),
        }
        for e in epics
    ]


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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
