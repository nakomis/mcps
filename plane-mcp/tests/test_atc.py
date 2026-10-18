"""Tests for the ATC integration (ATC-5): required points, module on create, board tools.

As in test_modules.py, the server's own helpers are mocked; `_atc` is replaced by a recorder.
"""

import pytest

from plane_mcp import server
from plane_mcp.server import ToolError

P = {"id": "proj-1", "identifier": "HOME", "name": "Home"}
META = {"states": [{"id": "s1", "name": "Todo", "group": "unstarted"}], "labels": [], "members": []}


class FakeAtc:
    def __init__(self):
        self.calls = []
        self.responses = {}
        self.fail_with = None

    def __call__(self, method, path, *, json=None, params=None):
        self.calls.append((method, path, json))
        if self.fail_with:
            raise self.fail_with
        return self.responses.get(path, {})


@pytest.fixture
def atc(monkeypatch):
    fake = FakeAtc()
    monkeypatch.setattr(server, "_atc", fake)
    return fake


@pytest.fixture
def plane(monkeypatch):
    state = {"calls": [], "modules": [{"id": "m1", "name": "Conversation-memory", "archived_at": None}]}

    def request(method, path, *, params=None, json=None):
        state["calls"].append((method, path, json))
        if method == "POST" and path.endswith("/work-items/"):
            return {"id": "new-id", "sequence_id": 500, "name": json["name"], "state": "s1"}
        if method == "PATCH":
            return {"id": "i1", "sequence_id": 414, "name": "x", "state": "s1"}

    def get_all(path, **params):
        if path.endswith("/modules/"):
            return list(state["modules"])
        if path.endswith("/archived-modules/") or path.endswith("/module-issues/"):
            return []
        raise AssertionError(path)

    monkeypatch.setattr(server, "_project", lambda ident: P)
    monkeypatch.setattr(server, "_project_meta", lambda p: META)
    monkeypatch.setattr(server, "_request", request)
    monkeypatch.setattr(server, "_get_all", get_all)
    item = {"id": "i1", "sequence_id": 414, "name": "x", "state": "s1"}
    monkeypatch.setattr(server, "_item_by_ref", lambda ref: (P, item))
    return state


def test_create_without_points_refuses_before_creating(plane, atc):
    with pytest.raises(ToolError, match="points is required"):
        server.create_work_item("HOME", "Do a thing")
    assert plane["calls"] == [] and atc.calls == []


@pytest.mark.parametrize("bad", [0, 101, -1])
def test_create_points_out_of_range_refuses(plane, atc, bad):
    with pytest.raises(ToolError, match="points is required"):
        server.create_work_item("HOME", "Do a thing", points=bad)
    assert plane["calls"] == []


def test_create_unknown_module_fails_before_creating(plane, atc):
    with pytest.raises(ToolError, match="No module 'nope'"):
        server.create_work_item("HOME", "Do a thing", points=3, module="nope")
    assert plane["calls"] == [] and atc.calls == []


def test_create_with_points_and_module(plane, atc):
    out = server.create_work_item("HOME", "Do a thing", points=3, module="Conversation-memory")
    assert out["points"] == 3 and out["module"] == "Conversation-memory"
    assert atc.calls == [("PUT", f"/api/work-items/{out['id']}/points", {"points": 3, "source": "mcp"})]
    assert ("POST", "/projects/proj-1/modules/m1/module-issues/", {"issues": ["new-id"]}) in plane["calls"]


def test_create_without_module_returns_null_module(plane, atc):
    assert server.create_work_item("HOME", "Do a thing", points=1)["module"] is None


def test_create_keeps_item_when_atc_fails(plane, atc):
    atc.fail_with = ToolError("PUT ... -> 502")
    with pytest.raises(ToolError, match=r"Created HOME-\d+ .* points were not set.*ref='HOME-500'"):
        server.create_work_item("HOME", "Do a thing", points=2)
    assert any(c[0] == "POST" for c in plane["calls"])


@pytest.mark.parametrize("bad", [0, 101, -1])
def test_points_range_on_update(plane, atc, bad):
    with pytest.raises(ToolError, match="points is required"):
        server.update_work_item("HOME-414", points=bad)
    assert atc.calls == []


def test_update_points_puts_to_atc(plane, atc):
    server.update_work_item("HOME-414", points=5)
    assert atc.calls == [("PUT", "/api/work-items/i1/points", {"points": 5, "source": "mcp"})]
    assert plane["calls"] == []  # nothing else to change in Plane


def test_update_without_points_never_calls_atc(plane, atc):
    server.update_work_item("HOME-414", name="y")
    assert atc.calls == []


def test_atc_client_never_sends_plane_token():
    assert "X-API-Key" not in server._atc_client.headers


def test_atc_wraps_http_status_and_connection_errors(monkeypatch):
    import httpx

    def handler(request):
        return httpx.Response(409, text="already set")

    monkeypatch.setattr(server, "_atc_client", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ToolError, match="409.*already set"):
        server._atc("PUT", "/api/x", json={})

    def boom(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(server, "_atc_client", httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(ToolError, match="unreachable"):
        server._atc("GET", "/api/next")


def test_atc_board_is_condensed(atc):
    atc.responses["/api/board"] = {"max_points": 100, "columns": [{
        "key": "next", "label": "Up next", "points": 5, "unestimated": 1,
        "cards": [{"kind": "project", "id": "p", "label": "Home", "points": 5, "unestimated": 1,
                   "modules": [{"id": "m", "name": "Mod", "points": 5, "unestimated": 1, "unsorted": False,
                                "status": None, "items": [{"ref": "HOME-1"}]}]}]}]}
    out = server.atc_board()
    assert out == {"columns": [{"column": "Up next", "points": 5, "unestimated": 1, "cards": [
        {"label": "Home", "points": 5, "unestimated": 1, "modules": [{"name": "Mod", "points": 5}]}]}]}


def test_whats_next_passes_through(atc):
    atc.responses["/api/next"] = {"column": "in_progress", "next": {"ref": "ATC-2"}, "runners_up": []}
    out = server.whats_next()
    assert out["next"]["ref"] == "ATC-2" and "pick_up_story" in out["hint"]


def test_whats_next_no_hint_when_empty(atc):
    atc.responses["/api/next"] = {"column": None, "next": None, "runners_up": []}
    assert "hint" not in server.whats_next()


def test_set_project_meta_sends_only_given_fields(atc):
    server.set_project_meta("atc", repo="nakomis/atc", bau_enabled=True)
    assert atc.calls[-1] == ("PUT", "/api/projects/ATC", {"repo": "nakomis/atc", "bau_enabled": True})


def test_set_project_meta_with_nothing_is_an_error(atc):
    with pytest.raises(ToolError):
        server.set_project_meta("atc")
    assert atc.calls == []
