"""Exercises the tools against a fake ScreenEye: a local HTTP server that
speaks the app's three endpoints, advertised over Bonjour like the phone."""

import io
import json
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image
from zeroconf import ServiceInfo, Zeroconf

from screeneye_mcp import server

FRAME = (3840, 2160)


class FakePhone(BaseHTTPRequestHandler):
    sets: ClassVar[list[dict]] = []

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/snapshot.jpg":
            buf = io.BytesIO()
            Image.new("RGB", FRAME, "white").save(buf, "JPEG")
            body, ctype = buf.getvalue(), "image/jpeg"
        elif u.path in ("/status", "/set"):
            if u.path == "/set":
                FakePhone.sets.append({k: v[0] for k, v in parse_qs(u.query).items()})
            body, ctype = json.dumps({"zoom": 1.0, "maxZoom": 15.0, "lens": "Back Camera",
                                      "focusMode": 2, "lensPosition": 0.5, "ev": 0}).encode(), "application/json"
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def phone(tmp_path, monkeypatch):
    httpd = HTTPServer(("127.0.0.1", 0), FakePhone)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    FakePhone.sets = []
    monkeypatch.setattr(server, "_DIR", tmp_path)
    yield httpd.server_address[1]
    httpd.shutdown()


def test_snapshot_full_frame_is_scaled(phone, monkeypatch):
    monkeypatch.setenv("SCREENEYE_URL", f"http://127.0.0.1:{phone}")
    out = server.snapshot()
    assert (out["width"], out["height"]) == (2560, 1440)
    assert Image.open(out["path"]).size == (2560, 1440)
    assert FakePhone.sets == []  # no camera arguments, no /set call


def test_snapshot_crop_keeps_sensor_resolution(phone, monkeypatch):
    monkeypatch.setenv("SCREENEYE_URL", f"http://127.0.0.1:{phone}")
    out = server.snapshot(crop=[0.25, 0.25, 0.5, 0.5], max_dim=0)
    assert (out["width"], out["height"]) == (1920, 1080)


def test_snapshot_applies_settings_first(phone, monkeypatch):
    monkeypatch.setenv("SCREENEYE_URL", f"http://127.0.0.1:{phone}")
    out = server.snapshot(zoom=3, focus_x=0.4, focus_y=0.6, lock=True, ev=-1, settle_ms=0)
    assert FakePhone.sets == [{"zoom": "3", "focus": "0.4,0.6", "lock": "1", "ev": "-1"}]
    assert out["camera"]["maxZoom"] == 15.0


def test_bad_crop_is_refused(phone, monkeypatch):
    monkeypatch.setenv("SCREENEYE_URL", f"http://127.0.0.1:{phone}")
    with pytest.raises(ValueError):
        server.snapshot(crop=[0, 0, 1])


def test_status_via_bonjour(phone, monkeypatch):
    monkeypatch.delenv("SCREENEYE_URL", raising=False)
    monkeypatch.setattr(server, "_base_url", None)
    zc = Zeroconf()
    info = ServiceInfo("_screeneye._tcp.local.", "ScreenEye-test._screeneye._tcp.local.",
                       addresses=[socket.inet_aton("127.0.0.1")], port=phone,
                       server="screeneye-test.local.")
    zc.register_service(info)
    try:
        st = server.status()
        assert st["url"] == f"http://127.0.0.1:{phone}"
        assert st["lens"] == "Back Camera"
    finally:
        zc.unregister_service(info)
        zc.close()


MAP = "/mnt/print-recordings=/Volumes/print-recordings"


@pytest.mark.parametrize("spec,path,want", [
    (MAP, "/mnt/print-recordings/screeneye/snapshots/a.jpg",
     "/Volumes/print-recordings/screeneye/snapshots/a.jpg"),
    (MAP + "/", "/mnt/print-recordings/x.jpg", "/Volumes/print-recordings/x.jpg"),
    (MAP, "/mnt/print-recordings-other/x.jpg", "/mnt/print-recordings-other/x.jpg"),
    (MAP, "/tmp/screeneye/x.jpg", "/tmp/screeneye/x.jpg"),
    ("", "/tmp/x.jpg", "/tmp/x.jpg"),
    # The longest prefix wins, whatever the order.
    ("/mnt=/A, /mnt/print-recordings=/B", "/mnt/print-recordings/x.jpg", "/B/x.jpg"),
    ("/mnt=/A, /mnt/print-recordings=/B", "/mnt/other/x.jpg", "/A/other/x.jpg"),
])
def test_map_path(spec, path, want):
    assert server.map_path(path, spec) == want


@pytest.mark.parametrize("spec", ["/mnt", "=/Volumes", "/mnt="])
def test_bad_path_map_is_refused(spec):
    with pytest.raises(ValueError):
        server.map_path("/mnt/x", spec)


def test_snapshot_returns_mapped_path(phone, tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENEYE_URL", f"http://127.0.0.1:{phone}")
    monkeypatch.setenv("SCREENEYE_PATH_MAP", f"{tmp_path}=/Volumes/share")
    out = server.snapshot(max_dim=640)
    assert out["path"].startswith("/Volumes/share/")
    assert out["host_path"].startswith(str(tmp_path) + "/")
    assert out["path"][len("/Volumes/share"):] == out["host_path"][len(str(tmp_path)):]
    assert Image.open(out["host_path"]).size == (640, 360)


def test_snapshot_goes_in_a_dated_folder(phone, tmp_path, monkeypatch):
    monkeypatch.setenv("SCREENEYE_URL", f"http://127.0.0.1:{phone}")
    monkeypatch.delenv("SCREENEYE_PATH_MAP", raising=False)
    out = server.snapshot(max_dim=64)
    p = Path(out["path"])
    assert p.parent.parent == tmp_path
    assert datetime.strptime(p.parent.name, "%Y-%m-%d")
    assert "host_path" not in out


def test_discovery_from_inside_an_event_loop(phone, monkeypatch):
    """The MCP server calls tools on its asyncio loop's thread; discovery must
    not adopt that loop and deadlock on it."""
    import asyncio

    monkeypatch.delenv("SCREENEYE_URL", raising=False)
    monkeypatch.setattr(server, "_base_url", None)
    zc = Zeroconf()
    info = ServiceInfo("_screeneye._tcp.local.", "ScreenEye-loop._screeneye._tcp.local.",
                       addresses=[socket.inet_aton("127.0.0.1")], port=phone,
                       server="screeneye-loop.local.")
    zc.register_service(info)

    async def in_loop():
        return server.status()

    try:
        assert asyncio.run(in_loop())["url"] == f"http://127.0.0.1:{phone}"
    finally:
        zc.unregister_service(info)
        zc.close()
