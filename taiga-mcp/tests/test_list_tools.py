"""Wiring tests: each list_* MCP tool calls _list_with_guard with the right
path/params and shape functions. Mocks server._get_page/_get_all so no
network access is needed.
"""

from taiga_mcp import server


def _stub(monkeypatch, total, items):
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([], {"total_count": total}))
    monkeypatch.setattr(server, "_get_all", lambda *a, **k: items)


def test_list_user_stories_passes_filters_through(monkeypatch):
    captured = {}

    def fake_get_all(path, **params):
        captured["path"] = path
        captured["params"] = params
        return [{"id": 1, "ref": 1, "subject": "s", "status": None, "status_id": 1,
                  "milestone": None, "assigned_to": None, "total_points": None,
                  "tags": [], "is_closed": False}]

    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([], {"total_count": 1}))
    monkeypatch.setattr(server, "_get_all", fake_get_all)

    result = server.list_user_stories(project_id=5, milestone_id=9, status_id=2, epic_id=3)

    assert captured["path"] == "/userstories"
    assert captured["params"] == {"project": 5, "milestone": 9, "status": 2, "epic": 3}
    assert result["items"][0]["ref"] == 1
    assert result["summary"] is False


def test_list_issues_default_shape(monkeypatch):
    issue = {"id": 1, "ref": 10, "subject": "bug", "status": 1,
              "type": None, "priority": None, "severity": None, "assigned_to": None,
              "tags": [], "is_closed": False}
    _stub(monkeypatch, 1, [issue])

    result = server.list_issues(project_id=1)

    assert result["items"] == [
        {"id": 1, "ref": 10, "subject": "bug", "status": None, "status_id": 1,
         "type": None, "priority": None, "severity": None, "assigned_to": None,
         "tags": [], "is_closed": False}
    ]


def test_list_tasks_summary_mode(monkeypatch):
    task = {"id": 1, "ref": 20, "subject": "chore",
            "status": 1, "status_extra_info": {"name": "Open"},
            "user_story": None, "assigned_to": None, "is_closed": False}
    _stub(monkeypatch, 1, [task])

    result = server.list_tasks(project_id=1, summary=True)

    assert result["items"] == [{"ref": 20, "subject": "chore", "status": "Open"}]
    assert result["summary"] is True


def test_list_epics_paged(monkeypatch):
    epic = {"id": 1, "ref": 30, "subject": "epic", "status": "Open", "status_id": 1,
            "color": "#fff", "assigned_to": None, "is_closed": False}
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([epic], {"total_count": 1}))

    result = server.list_epics(project_id=1, page=1, page_size=10)

    assert result["page"] == 1
    assert result["page_size"] == 10
    assert result["has_more"] is False
    assert result["items"][0]["ref"] == 30
