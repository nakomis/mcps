"""Tests for module support (HOME-424).

Like the taiga-mcp tests, these mock the server's own helpers (_get_all,
_request, _project, _project_meta) rather than the HTTP layer, since what is
under test is the lookup, validation and payload logic.
"""

import pytest

from plane_mcp import server

P = {"id": "proj-1", "identifier": "HOME", "name": "Home"}
META = {"states": [{"id": "s1", "name": "Todo", "group": "unstarted"}], "labels": [],
        "members": [{"id": "u1", "email": "martin@nakomis.com", "display_name": "martin"}]}


def _mod(mid, name, **kw):
    return {"id": mid, "name": name, "status": "planned", "lead": None, "start_date": None,
            "target_date": None, "total_issues": 0, "completed_issues": 0, "archived_at": None, **kw}


def _item(iid, seq):
    return {"id": iid, "sequence_id": seq, "name": f"item {seq}", "state": "s1"}


@pytest.fixture
def plane(monkeypatch):
    """A fake Plane: one project, a module list, per-module items, recorded writes."""
    state = {
        "modules": [_mod("m1", "Conversation-memory", total_issues=2, lead="u1"), _mod("m2", "Other")],
        "archived": [_mod("m3", "Old", archived_at="2026-01-01")],
        "items": {"m1": [_item("i1", 414), _item("i2", 416)], "m2": [], "m3": []},
        "calls": [],
    }
    by_ref = {f"HOME-{n}": _item(f"i{k}", n) for k, n in ((1, 414), (2, 416), (3, 424))}

    def get_all(path, **params):
        if path.endswith("/archived-modules/"):
            return list(state["archived"])
        if path.endswith("/modules/"):
            return list(state["modules"])
        if path.endswith("/module-issues/"):
            return list(state["items"][path.split("/")[-3]])
        if path.endswith("/work-items/"):
            return [_item("i1", 414), _item("i2", 416), _item("i3", 424)]
        raise AssertionError(path)

    def request(method, path, *, params=None, json=None):
        state["calls"].append((method, path, json))
        if method == "POST" and path.endswith("/modules/"):
            return _mod("new", json["name"], **{k: v for k, v in json.items() if k != "name"})
        if method == "PATCH":
            return dict(json)  # Plane's PATCH response is partial: no id, no counts

    monkeypatch.setattr(server, "_project", lambda ident: P)
    monkeypatch.setattr(server, "_project_meta", lambda p: META)
    monkeypatch.setattr(server, "_get_all", get_all)
    monkeypatch.setattr(server, "_request", request)
    monkeypatch.setattr(server, "_item_by_ref", lambda ref: (P, by_ref[ref.upper()]))
    monkeypatch.setattr(server, "_me", lambda: {"id": "u1"})
    return state


def test_list_modules_shape_and_archived(plane):
    out = server.list_modules("HOME")
    assert [m["name"] for m in out["modules"]] == ["Conversation-memory", "Other"]
    cm = out["modules"][0]
    assert (cm["status"], cm["lead"], cm["total_issues"], cm["completed_issues"]) == ("planned", "martin", 2, 0)
    with_old = server.list_modules("HOME", include_archived=True)
    assert [m["name"] for m in with_old["modules"]] == ["Conversation-memory", "Old", "Other"]
    assert with_old["modules"][1]["archived"] is True


def test_get_module_by_name_case_insensitive_and_by_id(plane):
    by_name = server.get_module("HOME", "conversation-MEMORY")
    assert by_name["id"] == "m1"
    assert [i["ref"] for i in by_name["work_items"]] == ["HOME-414", "HOME-416"]
    assert set(by_name["work_items"][0]) == {"ref", "name", "state", "state_group"}
    assert server.get_module("HOME", "m1")["name"] == "Conversation-memory"


def test_get_module_unknown_lists_modules(plane):
    with pytest.raises(ValueError, match="No module 'nope'.*Conversation-memory"):
        server.get_module("HOME", "nope")


def test_ambiguous_name_is_an_error(plane):
    plane["modules"].append(_mod("m9", "other"))
    with pytest.raises(ValueError, match="2 modules"):
        server.get_module("HOME", "OTHER")


def test_create_module_payload(plane):
    out = server.create_module("HOME", "New", description="d", status="in-progress", lead="me",
                               start_date="2026-10-01", target_date="2026-11-01")
    method, path, body = plane["calls"][-1]
    assert (method, path) == ("POST", "/projects/proj-1/modules/")
    assert body == {"name": "New", "description": "d", "status": "in-progress", "lead": "u1",
                    "start_date": "2026-10-01", "target_date": "2026-11-01"}
    assert out["name"] == "New"


def test_create_module_minimal_sends_only_name(plane):
    server.create_module("HOME", "Bare")
    assert plane["calls"][-1][2] == {"name": "Bare"}


def test_invalid_status_rejected_before_any_request(plane):
    with pytest.raises(ValueError, match="status must be one of"):
        server.create_module("HOME", "X", status="done")
    assert plane["calls"] == []


def test_update_module_only_provided_fields(plane):
    out = server.update_module("HOME", "Conversation-memory", status="paused", lead="martin@nakomis.com")
    assert plane["calls"][-1] == ("PATCH", "/projects/proj-1/modules/m1/", {"status": "paused", "lead": "u1"})
    assert (out["id"], out["name"], out["status"], out["total_issues"]) == ("m1", "Conversation-memory", "paused", 2)


def test_update_module_with_nothing_makes_no_request(plane):
    out = server.update_module("HOME", "Other")
    assert plane["calls"] == [] and out["id"] == "m2"


def test_add_to_module_posts_only_new_items(plane):
    out = server.add_to_module("conversation-memory", ["HOME-414", "HOME-424"])
    assert plane["calls"] == [("POST", "/projects/proj-1/modules/m1/module-issues/", {"issues": ["i3"]})]
    assert out == {"module": "Conversation-memory", "added": ["HOME-424"], "already_in_module": ["HOME-414"]}


def test_add_to_module_all_present_makes_no_write(plane):
    out = server.add_to_module("m1", ["HOME-414"])
    assert plane["calls"] == [] and out["added"] == []


def test_refs_must_share_a_project(plane):
    with pytest.raises(ValueError, match="one project"):
        server.add_to_module("m1", ["HOME-414", "NAKIS-1"])
    with pytest.raises(ValueError, match="empty"):
        server.remove_from_module("m1", [])
    assert plane["calls"] == []


def test_remove_from_module_deletes_each_member(plane):
    out = server.remove_from_module("Conversation-memory", ["HOME-414", "HOME-424"])
    assert plane["calls"] == [("DELETE", "/projects/proj-1/modules/m1/module-issues/i1/", None)]
    assert out == {"module": "Conversation-memory", "removed": ["HOME-414"], "not_in_module": ["HOME-424"]}


def test_list_work_items_module_filter(plane):
    out = server.list_work_items("HOME", module="Conversation-memory")
    assert [i["ref"] for i in out["items"]] == ["HOME-414", "HOME-416"]
    assert [i["ref"] for i in server.list_work_items("HOME")["items"]] == ["HOME-414", "HOME-416", "HOME-424"]


def test_item_module_names_skips_empty_modules(plane, monkeypatch):
    seen = []
    real = server._module_items
    monkeypatch.setattr(server, "_module_items", lambda p, m: seen.append(m["id"]) or real(p, m))
    assert server._item_module_names(P, "i1") == ["Conversation-memory"]
    assert "m2" not in seen  # total_issues == 0, never fetched
