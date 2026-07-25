"""Tests for _list_with_guard — the shared engine behind list_user_stories,
list_issues, list_tasks, and list_epics (TAIG-22).

These mock server._get_page / server._get_all directly rather than the HTTP
layer, since the behaviour under test is the guard/paging/filtering logic,
not the HTTP plumbing (covered separately in test_get_page.py).
"""

import pytest

from taiga_mcp import server


def _full(item):
    return {"ref": item["ref"], "subject": item["subject"], "is_closed": item["is_closed"]}


def _summary(item):
    return {"ref": item["ref"], "subject": item["subject"]}


def _story(ref, is_closed=False):
    return {"ref": ref, "subject": f"story {ref}", "is_closed": is_closed}


def test_defaults_to_open_only(monkeypatch):
    items = [_story(1), _story(2, is_closed=True), _story(3)]
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([], {"total_count": len(items)}))
    monkeypatch.setattr(server, "_get_all", lambda *a, **k: items)

    result = server._list_with_guard(
        "/userstories", _full, _summary, {"project": 1},
        include_closed=False, summary=False, page=None, page_size=50,
    )

    assert [i["ref"] for i in result["items"]] == [1, 3]
    assert result["summary"] is False
    assert result["total_count"] == 3
    assert "note" not in result


def test_include_closed_true_keeps_everything(monkeypatch):
    items = [_story(1), _story(2, is_closed=True)]
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([], {"total_count": len(items)}))
    monkeypatch.setattr(server, "_get_all", lambda *a, **k: items)

    result = server._list_with_guard(
        "/userstories", _full, _summary, {"project": 1},
        include_closed=True, summary=False, page=None, page_size=50,
    )

    assert [i["ref"] for i in result["items"]] == [1, 2]


def test_explicit_summary_uses_summary_shape(monkeypatch):
    items = [_story(1)]
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([], {"total_count": 1}))
    monkeypatch.setattr(server, "_get_all", lambda *a, **k: items)

    result = server._list_with_guard(
        "/userstories", _full, _summary, {"project": 1},
        include_closed=False, summary=True, page=None, page_size=50,
    )

    assert result["items"] == [{"ref": 1, "subject": "story 1"}]
    assert result["summary"] is True


def test_auto_downgrades_to_summary_above_threshold(monkeypatch):
    total = server._SUMMARY_AUTO_THRESHOLD + 1
    items = [_story(n) for n in range(total)]
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([], {"total_count": total}))
    monkeypatch.setattr(server, "_get_all", lambda *a, **k: items)

    result = server._list_with_guard(
        "/userstories", _full, _summary, {"project": 1},
        include_closed=False, summary=False, page=None, page_size=50,
    )

    assert result["summary"] is True
    assert len(result["items"]) == total
    assert result["items"][0] == {"ref": 0, "subject": "story 0"}
    assert "note" in result and "auto-switched" in result["note"].lower()


def test_hard_cap_refuses_to_enumerate(monkeypatch):
    total = server._HARD_CAP + 1
    called_get_all = []
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([], {"total_count": total}))
    monkeypatch.setattr(server, "_get_all", lambda *a, **k: called_get_all.append(1))

    result = server._list_with_guard(
        "/userstories", _full, _summary, {"project": 1},
        include_closed=False, summary=False, page=None, page_size=50,
    )

    assert result["items"] == []
    assert result["total_count"] == total
    assert result["has_more"] is True
    assert "hard cap" in result["note"].lower()
    # must not have fallen through to a full fetch
    assert called_get_all == []


def test_paged_call_uses_get_page_and_reports_has_more(monkeypatch):
    page_items = [_story(1), _story(2)]

    def fake_get_page(path, page, page_size, **params):
        assert page == 2
        assert page_size == 2
        return page_items, {"total_count": 10}

    monkeypatch.setattr(server, "_get_page", fake_get_page)
    monkeypatch.setattr(server, "_get_all", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("paged calls must not use _get_all")))

    result = server._list_with_guard(
        "/userstories", _full, _summary, {"project": 1},
        include_closed=False, summary=False, page=2, page_size=2,
    )

    assert result["page"] == 2
    assert result["page_size"] == 2
    assert result["total_count"] == 10
    assert result["has_more"] is True  # page 2 of size 2 = 4 seen, 10 total
    assert len(result["items"]) == 2


def test_paged_call_last_page_has_more_false(monkeypatch):
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: ([_story(1)], {"total_count": 5}))

    result = server._list_with_guard(
        "/userstories", _full, _summary, {"project": 1},
        include_closed=False, summary=False, page=3, page_size=2,
    )

    assert result["has_more"] is False  # page 3 of size 2 = 6 seen >= 5 total


def test_paged_call_filters_closed_client_side(monkeypatch):
    page_items = [_story(1), _story(2, is_closed=True)]
    monkeypatch.setattr(server, "_get_page", lambda *a, **k: (page_items, {"total_count": 2}))

    result = server._list_with_guard(
        "/userstories", _full, _summary, {"project": 1},
        include_closed=False, summary=False, page=1, page_size=50,
    )

    assert [i["ref"] for i in result["items"]] == [1]
    assert result["returned_count"] == 1
