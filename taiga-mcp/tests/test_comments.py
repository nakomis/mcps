"""Comments must be discoverable from every read tool.

Two guarantees are covered here:

  * The detail tools (get_user_story, get_issue, get_task, get_epic) return the
    comment text itself. Anything add_comment can write to must be readable
    back — an issue or task whose comments only exist in the web UI is how a
    "superseded, don't build this" decision gets missed.
  * The list tools that CAN report a count do, in both full and summary shape,
    so a survey of a board can see where the unread discussion is. Issues and
    epics have no counter on Taiga's endpoints, so their list shapes stay bare
    and the omission is documented instead of faked.
"""

import pytest

from taiga_mcp import server


def _history(comment="a decision", hidden=False):
    return [
        {"id": "h1", "user": {"username": "claude", "name": "Claude"},
         "comment": comment, "created_at": "2026-08-03T14:42:53.877Z",
         "is_hidden": hidden},
        # A plain field-change entry — no comment, must never surface.
        {"id": "h2", "user": {"username": "nakomis", "name": "Martin"},
         "comment": "", "created_at": "2026-08-03T14:43:00.000Z"},
    ]


@pytest.fixture
def stub_get(monkeypatch):
    """Route _get by path: /history/... yields comments, anything else an item."""
    def factory(item, history=None):
        def fake_get(path, **params):
            if path.startswith("/history/"):
                return history if history is not None else _history()
            return item
        monkeypatch.setattr(server, "_get", fake_get)
    return factory


# ── Detail tools return the comment text ──────────────────────────────────────

def test_get_issue_returns_comments(stub_get):
    stub_get({"id": 1, "ref": 309, "subject": "bug", "description": "d",
              "status": 1, "tags": [], "is_closed": False})

    result = server.get_issue(1)

    assert [c["comment"] for c in result["comments"]] == ["a decision"]
    assert result["comments"][0]["author"] == "claude"


def test_get_task_returns_comments(stub_get):
    stub_get({"id": 2, "ref": 20, "subject": "chore", "description": "d",
              "status": 1, "user_story": None, "is_closed": False})

    assert [c["comment"] for c in server.get_task(2)["comments"]] == ["a decision"]


def test_get_epic_returns_comments(stub_get):
    stub_get({"id": 3, "ref": 30, "subject": "epic", "description": "d",
              "status": 1, "color": "#fff", "tags": [], "is_closed": False})

    assert [c["comment"] for c in server.get_epic(3)["comments"]] == ["a decision"]


def test_get_user_story_returns_comments(stub_get):
    stub_get({"id": 4, "ref": 4, "subject": "story", "description": "d",
              "status": 1, "milestone": None, "total_points": None,
              "tags": [], "is_closed": False})

    assert [c["comment"] for c in server.get_user_story(4)["comments"]] == ["a decision"]


def test_hidden_comments_are_excluded(stub_get):
    stub_get({"id": 1, "ref": 309, "subject": "bug", "description": "d",
              "status": 1, "tags": [], "is_closed": False},
             history=_history(hidden=True))

    assert server.get_issue(1)["comments"] == []


# ── List tools surface a count where Taiga provides one ───────────────────────

def test_story_shapes_carry_comment_count():
    story = {"id": 1, "ref": 4, "subject": "s", "status": 1, "milestone": None,
             "total_points": None, "tags": [], "is_closed": False,
             "total_comments": 7}

    assert server._us_full(story)["comment_count"] == 7
    assert server._us_summary(story)["comment_count"] == 7


def test_task_shapes_carry_comment_count():
    task = {"id": 1, "ref": 20, "subject": "t", "status": 1,
            "user_story": None, "is_closed": False, "total_comments": 2}

    assert server._task_full(task)["comment_count"] == 2
    assert server._task_summary(task)["comment_count"] == 2


def test_comment_count_defaults_to_zero_when_taiga_omits_it():
    """Older Taiga payloads may not carry the counter — report 0, never crash."""
    story = {"id": 1, "ref": 4, "subject": "s", "status": 1, "milestone": None,
             "total_points": None, "tags": [], "is_closed": False}

    assert server._us_full(story)["comment_count"] == 0


def test_issue_and_epic_shapes_omit_comment_count():
    """Taiga's issue/epic endpoints have no total_comments field, so claiming a
    count here would mean an N+1 walk of /history. The docstrings say so
    instead — if this ever starts passing with a count, Taiga gained the field
    and the docstrings need revisiting."""
    issue = {"id": 1, "ref": 10, "subject": "bug", "status": 1, "tags": [],
             "is_closed": False}
    epic = {"id": 1, "ref": 30, "subject": "e", "status": 1, "color": "#fff",
            "is_closed": False}

    assert "comment_count" not in server._issue_full(issue)
    assert "comment_count" not in server._issue_summary(issue)
    assert "comment_count" not in server._epic_full(epic)
    assert "comment_count" not in server._epic_summary(epic)
