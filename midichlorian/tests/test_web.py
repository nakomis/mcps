"""The shipped web UI is served unauthenticated and is self-contained."""

import re

import httpx
import pytest
from fastapi.testclient import TestClient

from midichlorian import api
from midichlorian.store import Store
from test_api import FakeCoordinator, settings



def ids_used_by_app(client):
    ids = set(re.findall(r'\$\("([^"]+)"\)', client.get("/app.js").text))
    assert {"banner", "hosts", "queue", "history", "detail", "log-panel", "log-output"} <= ids
    return ids


@pytest.fixture
def client(tmp_path):
    s = settings(tmp_path)
    coord = FakeCoordinator(s, Store(tmp_path / "m.db"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with TestClient(api.create_app(s, coord, http)) as tc:  # the real web dir, no token
        yield tc


def test_index_is_served_without_auth(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert "<title>midichlorian</title>" in html
    for element_id in ids_used_by_app(client):
        assert f'id="{element_id}"' in html, element_id
    assert 'src="app.js"' in html or 'src="/app.js"' in html
    assert 'href="style.css"' in html or 'href="/style.css"' in html


def test_assets_have_the_right_content_types(client):
    js = client.get("/app.js")
    css = client.get("/style.css")
    assert js.status_code == 200 and css.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert css.headers["content-type"].startswith("text/css")
    assert "EventSource" in js.text
    assert "prefers-color-scheme" in css.text


def test_nothing_is_loaded_from_elsewhere(client):
    pages = [client.get(p).text for p in ("/", "/app.js", "/style.css")]
    assert not re.search(r"""<(script|link)[^>]+(src|href)=["']https?://""", pages[0])
    assert "@import" not in pages[2]
    assert not re.search(r"url\(\s*[\"']?https?://", pages[2])
    assert not re.search(r"""\bimport\s*\(|\bimport\s+.*from\s+["']https?://""", pages[1])


def test_server_data_is_never_put_through_innerhtml(client):
    js = client.get("/app.js").text
    assert "innerHTML" not in js
    assert "insertAdjacentHTML" not in js
    assert "document.write" not in js
    assert "window.confirm" not in js and "confirm(" not in js and "alert(" not in js


def test_polling_cannot_pile_up(client):
    js = client.get("/app.js").text
    assert "setInterval" not in js  # a chained setTimeout, so slow polls never overlap
    assert "AbortSignal.timeout" in js
    assert "requestAnimationFrame" in js
