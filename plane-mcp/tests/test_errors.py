"""Error text and per-project labels.

mcp 2.x withholds the text of any exception other than ToolError from the
client, so the server raises ToolError for every failure it anticipates. Labels
are per project in Plane: filtering by one a project lacks is an empty result,
not an error.
"""

import asyncio

import pytest
from mcp.server.mcpserver.exceptions import UnexpectedToolError

from plane_mcp import server

P = {"id": "proj-1", "identifier": "HOME", "name": "Home"}
META = {"states": [{"id": "s1", "name": "Todo", "group": "unstarted"}],
        "labels": [{"id": "l1", "name": "BAU"}], "members": []}


def _item(iid, seq, labels):
    return {"id": iid, "sequence_id": seq, "name": f"item {seq}", "state": "s1", "labels": labels}


@pytest.fixture
def plane(monkeypatch):
    calls = []
    items = [_item("i1", 1, ["l1"]), _item("i2", 2, [])]

    def request(method, path, *, params=None, json=None):
        calls.append((method, path, json))
        return {**items[0], **(json or {})}

    monkeypatch.setattr(server, "_project", lambda ident: P)
    monkeypatch.setattr(server, "_project_meta", lambda p: META)
    monkeypatch.setattr(server, "_get_all", lambda path, **params: list(items))
    monkeypatch.setattr(server, "_request", request)
    monkeypatch.setattr(server, "_item_by_ref", lambda ref: (P, items[0]))
    monkeypatch.setattr(server, "get_story", lambda ref: {"ref": ref})
    return calls


def test_label_filter_matches_case_insensitively(plane):
    out = server.list_work_items("HOME", label="bau")
    assert [i["ref"] for i in out["items"]] == ["HOME-1"]


def test_label_missing_from_project_is_an_empty_result(plane):
    out = server.list_work_items("HOME", label="no-such-label")
    assert out["total_count"] == 0 and out["items"] == []
    assert plane == []  # filtering never creates the label


def test_removing_a_label_the_project_lacks_is_a_no_op(plane):
    server.update_work_item("HOME-1", remove_labels=["no-such-label"])
    assert plane == [("PATCH", "/projects/proj-1/work-items/i1/", {"labels": ["l1"]})]


def test_anticipated_failure_text_reaches_the_client(plane):
    with pytest.raises(server.ToolError) as exc:
        asyncio.run(server.mcp.call_tool("list_work_items", {"project": "HOME", "state": "Nope"}))
    assert not isinstance(exc.value, UnexpectedToolError)
    assert "No state 'Nope' in HOME. States: ['Todo']" in str(exc.value)
