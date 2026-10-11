"""
MCP server for ScreenEye, the iPhone app that points a camera at a screen so
Claude can read it.

The app serves the back camera over HTTP on port 8080 and advertises itself
over Bonjour as ``_screeneye._tcp``:

  GET /snapshot.jpg    latest frame (4K where the phone supports it)
  GET /set?...         zoom, focus=x,y, lock=0|1, ev
  GET /status          JSON: zoom, maxZoom, lens, focusMode, lensPosition, ev

Snapshots are written to disk and returned as a path rather than as image
content: meta-mcp flattens every result to text, so an inline image would
arrive as base64. Read the path to see the picture.

Environment variables:
  SCREENEYE_URL   Base URL of the phone, e.g. http://172.29.0.40:8080.
                  Skips Bonjour discovery when set.
  SCREENEYE_DIR   Where snapshots go (default: /tmp/screeneye)
"""

import io
import os
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from PIL import Image
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

mcp = FastMCP("screeneye-mcp")

_SERVICE = "_screeneye._tcp.local."
_DIR = Path(os.environ.get("SCREENEYE_DIR", "/tmp/screeneye"))
_TIMEOUT = httpx.Timeout(10.0, connect=3.0)

_base_url: str | None = os.environ.get("SCREENEYE_URL") or None


# ── Discovery ──────────────────────────────────────────────────────────────────

class _Found(ServiceListener):
    def __init__(self) -> None:
        self.url: str | None = None
        self.event = threading.Event()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=2000)
        if info is None:
            return
        v4 = [a for a in info.parsed_addresses() if ":" not in a]
        if v4:
            self.url = f"http://{v4[0]}:{info.port}"
            self.event.set()

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def _discover(timeout: float = 5.0) -> str | None:
    zc = Zeroconf()
    try:
        found = _Found()
        ServiceBrowser(zc, _SERVICE, found)
        found.event.wait(timeout)
        return found.url
    finally:
        zc.close()


def _url(rediscover: bool = False) -> str:
    """The phone's base URL: SCREENEYE_URL, else Bonjour (cached)."""
    global _base_url
    if os.environ.get("SCREENEYE_URL"):
        return os.environ["SCREENEYE_URL"].rstrip("/")
    if _base_url is None or rediscover:
        _base_url = _discover()
    if _base_url is None:
        raise RuntimeError(
            "ScreenEye not found over Bonjour (_screeneye._tcp). Is the app open "
            "on the phone, on the same network, with Local Network access allowed? "
            "Or set SCREENEYE_URL."
        )
    return _base_url


def _get(path: str, params: dict | None = None) -> httpx.Response:
    """GET from the phone, rediscovering once if the cached address went stale."""
    for attempt in (0, 1):
        try:
            r = httpx.get(_url(rediscover=attempt == 1) + path, params=params, timeout=_TIMEOUT)
            r.raise_for_status()
            return r
        except (httpx.ConnectError, httpx.ConnectTimeout, socket.gaierror):
            if attempt == 1 or os.environ.get("SCREENEYE_URL"):
                raise
    raise AssertionError("unreachable")


def _settings(zoom, focus_x, focus_y, lock, ev) -> dict:
    p: dict[str, str] = {}
    if zoom is not None:
        p["zoom"] = str(zoom)
    if focus_x is not None and focus_y is not None:
        p["focus"] = f"{focus_x},{focus_y}"
    if lock is not None:
        p["lock"] = "1" if lock else "0"
    if ev is not None:
        p["ev"] = str(ev)
    return p


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def status() -> dict:
    """Camera state (zoom as a multiple of the 1x lens, max zoom, active lens,
    focus mode, lens position, exposure bias) and the phone's URL."""
    return {"url": _url(), **_get("/status").json()}


@mcp.tool()
def set_camera(
    zoom: float | None = None,
    focus_x: float | None = None,
    focus_y: float | None = None,
    lock: bool | None = None,
    ev: float | None = None,
) -> dict:
    """Adjust the camera. Only the arguments given are changed.

    zoom: multiple of the 1x lens (0.5 is the ultra-wide where available).
    focus_x, focus_y: focus/exposure point, 0..1 in the sensor's native
        landscape frame ((0,0) top-left with the phone's home side to the right).
    lock: true locks focus where it is; false returns to continuous autofocus.
    ev: exposure bias in stops; negative helps with bright screens.
    """
    return _get("/set", _settings(zoom, focus_x, focus_y, lock, ev)).json()


@mcp.tool()
def snapshot(
    zoom: float | None = None,
    focus_x: float | None = None,
    focus_y: float | None = None,
    lock: bool | None = None,
    ev: float | None = None,
    crop: list[float] | None = None,
    max_dim: int = 2560,
    settle_ms: int = 800,
) -> dict:
    """Take a picture and save it as a JPEG; returns its path. Read the path to
    see it.

    Camera arguments are as for set_camera and are applied first; the camera
    then gets settle_ms to refocus before the frame is taken.
    crop: [x, y, w, h] as fractions of the frame, applied before scaling, to
        read a small part of the screen at full sensor resolution.
    max_dim: longest side of the saved image, in pixels (0 = no scaling).
    """
    params = _settings(zoom, focus_x, focus_y, lock, ev)
    cam = None
    if params:
        cam = _get("/set", params).json()
        time.sleep(max(settle_ms, 0) / 1000)

    img = Image.open(io.BytesIO(_get("/snapshot.jpg").content))
    img.load()
    if crop:
        if len(crop) != 4:
            raise ValueError("crop is [x, y, w, h] as fractions of the frame")
        x, y, w, h = crop
        W, H = img.size
        img = img.crop((round(x * W), round(y * H), round((x + w) * W), round((y + h) * H)))
    if max_dim and max(img.size) > max_dim:
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)

    _DIR.mkdir(parents=True, exist_ok=True)
    path = _DIR / f"snap-{datetime.now():%Y%m%d-%H%M%S-%f}.jpg"
    img.convert("RGB").save(path, "JPEG", quality=90)
    out = {"path": str(path), "width": img.width, "height": img.height}
    if cam is not None:
        out["camera"] = cam
    return out


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
