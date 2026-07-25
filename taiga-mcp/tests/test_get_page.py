"""Tests for _get_page — the single-page Taiga fetch used by paged list_* calls."""

from taiga_mcp import server


class _FakeResponse:
    def __init__(self, items, headers=None, status_code=200):
        self._items = items
        self.headers = headers or {}
        self.status_code = status_code

    def json(self):
        return self._items

    def raise_for_status(self):
        pass


def test_get_page_parses_pagination_headers(monkeypatch):
    calls = []

    def fake_get(url, headers=None, params=None, timeout=None):
        calls.append({"url": url, "params": params})
        return _FakeResponse(
            [{"id": 1}, {"id": 2}],
            headers={
                "x-pagination-count": "42",
                "x-paginated": "true",
                "x-pagination-current": "3",
            },
        )

    monkeypatch.setattr(server.httpx, "get", fake_get)

    items, info = server._get_page("/userstories", page=3, page_size=2, project=7)

    assert items == [{"id": 1}, {"id": 2}]
    assert info == {"total_count": 42, "is_paginated": True, "current_page": 3}
    assert calls[0]["params"]["page"] == 3
    assert calls[0]["params"]["page_size"] == 2
    assert calls[0]["params"]["project"] == 7


def test_get_page_handles_missing_pagination_headers(monkeypatch):
    def fake_get(url, headers=None, params=None, timeout=None):
        return _FakeResponse([{"id": 1}], headers={})

    monkeypatch.setattr(server.httpx, "get", fake_get)

    items, info = server._get_page("/epics", page=1, page_size=50, project=1)

    assert items == [{"id": 1}]
    assert info["total_count"] is None
    assert info["is_paginated"] is False
    # falls back to the requested page when Taiga doesn't echo one back
    assert info["current_page"] == 1


def test_get_page_retries_once_on_401(monkeypatch):
    responses = [_FakeResponse([], status_code=401), _FakeResponse([{"id": 9}])]

    def fake_get(url, headers=None, params=None, timeout=None):
        return responses.pop(0)

    monkeypatch.setattr(server.httpx, "get", fake_get)
    monkeypatch.setattr(server, "_authenticate", lambda: "fresh-token")

    items, _ = server._get_page("/tasks", page=1, page_size=50, project=1)

    assert items == [{"id": 9}]
