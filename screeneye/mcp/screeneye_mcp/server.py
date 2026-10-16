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
  SCREENEYE_DIR   Where snapshots go (default: /tmp/screeneye), in a
                  YYYY-MM-DD folder per day
  SCREENEYE_PATH_MAP
                  Rewrites the returned path for the reader, when the MCP runs
                  on another machine that writes to a shared folder:
                  `local_prefix=reader_prefix`, several separated by commas,
                  e.g. /mnt/print-recordings=/Volumes/print-recordings
"""

import io
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


def _path_map(spec: str | None) -> list[tuple[str, str]]:
    """Parse SCREENEYE_PATH_MAP into (local, reader) prefixes, longest first."""
    pairs = []
    for item in (spec or "").split(","):
        if not item.strip():
            continue
        local, sep, reader = item.partition("=")
        if not sep or not local.strip() or not reader.strip():
            raise ValueError(f"SCREENEYE_PATH_MAP entry {item!r} is not local=reader")
        pairs.append((local.strip().rstrip("/"), reader.strip().rstrip("/")))
    return sorted(pairs, key=lambda p: len(p[0]), reverse=True)


def map_path(path: Path | str, spec: str | None = None) -> str:
    """The path as the reader sees it: the longest matching SCREENEYE_PATH_MAP
    prefix replaced, or unchanged when nothing matches."""
    s = str(path)
    if spec is None:
        spec = os.environ.get("SCREENEYE_PATH_MAP")
    for local, reader in _path_map(spec):
        if s == local or s.startswith(local + "/"):
            return reader + s[len(local):]
    return s


# ── Discovery ──────────────────────────────────────────────────────────────────

class _Found(ServiceListener):
    """Collects service names. Resolving happens on the caller's thread: a
    blocking get_service_info inside the callback runs on zeroconf's own event
    loop thread, which can't answer it, so it only ever timed out."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.event = threading.Event()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.names.append(name)
        self.event.set()

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.add_service(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def _discover(timeout: float = 5.0) -> str | None:
    """Browse for the phone on a thread of its own. The tools run on the MCP
    server's event loop thread, and a Zeroconf created there adopts that loop,
    so its blocking calls would wait on a loop they are themselves blocking."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_browse, timeout).result()


def _browse(timeout: float) -> str | None:
    zc = Zeroconf()
    try:
        found = _Found()
        browser = ServiceBrowser(zc, _SERVICE, found)
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                # With a name that hasn't resolved yet, look again shortly
                # rather than wait for the next announcement.
                pause = deadline - time.monotonic()
                found.event.wait(max(min(pause, 0.5) if found.names else pause, 0))
                found.event.clear()
                # Every name, every time round: one that didn't resolve yet
                # (the phone has only just announced itself) is tried again.
                for name in dict.fromkeys(found.names):
                    wait = min(3000, max(int((deadline - time.monotonic()) * 1000), 1))
                    info = zc.get_service_info(_SERVICE, name, timeout=wait)
                    v4 = [a for a in info.parsed_addresses() if ":" not in a] if info else []
                    if v4:
                        return f"http://{v4[0]}:{info.port}"
            return None
        finally:
            browser.cancel()
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
    see it. If SCREENEYE_PATH_MAP applies, path is the caller's view of the
    file and host_path is where the server wrote it.

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

    now = datetime.now()
    day = _DIR / f"{now:%Y-%m-%d}"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"snap-{now:%Y%m%d-%H%M%S-%f}.jpg"
    img.convert("RGB").save(path, "JPEG", quality=90)
    out = {"path": map_path(path), "width": img.width, "height": img.height}
    if out["path"] != str(path):
        out["host_path"] = str(path)
    if cam is not None:
        out["camera"] = cam
    return out


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
