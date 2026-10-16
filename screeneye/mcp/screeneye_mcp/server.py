"""
MCP server for ScreenEye, the iPhone app that points a camera at a screen so
Claude can read it.

The app serves the back camera over HTTP on port 8080 and advertises itself
over Bonjour as ``_screeneye._tcp``:

  GET /snapshot.jpg    the picture: the visible region (viewport) of the
                       latest frame, raw: in the sensor's landscape, whichever
                       way up the phone is. ?full=1 for the lens's whole frame.
                       Headers X-ScreenEye-Orientation, X-ScreenEye-Rotation
                       and X-ScreenEye-Viewport (raw fractions of the frame).
  GET /set?...         zoom, pan=x,y, focus=x,y, lock=0|1, ev; upright=1 means
                       pan and focus are in the upright frame
  GET /status          JSON: zoom, maxZoom, opticalZoom, lens, focusMode,
                       lensPosition, ev, orientation, rotation (the clockwise
                       degrees that turn a raw frame upright), pan, viewport;
                       ?upright=1 gives pan and viewport upright
Apps from before EYE-3 and EYE-4 lack the newer fields and parameters.

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
from PIL.Image import Transpose
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


# Clockwise degrees -> the PIL transpose that turns an image that far
# (PIL's ROTATE_* are anticlockwise).
_CLOCKWISE = {90: Transpose.ROTATE_270, 180: Transpose.ROTATE_180, 270: Transpose.ROTATE_90}


def rotation_of(value) -> int:
    """A rotation as the phone reports it, as 0, 90, 180 or 270; anything
    missing or unrecognised is 0 (leave the frame as it is)."""
    try:
        r = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return r if r in _CLOCKWISE else 0


def rotate_upright(img: Image.Image, rotation: int) -> Image.Image:
    """The image turned `rotation` degrees clockwise (0 leaves it alone)."""
    t = _CLOCKWISE.get(rotation)
    return img.transpose(t) if t is not None else img


def turned_rect(r: list[float], rotation: int) -> list[float]:
    """A rectangle [x, y, w, h] (fractions, top-left origin) of a raw frame,
    in the frame turned `rotation` degrees clockwise."""
    x, y, w, h = r
    corners = [(x, y), (x + w, y + h)]
    if rotation == 90:
        corners = [(1 - b, a) for a, b in corners]
    elif rotation == 180:
        corners = [(1 - a, 1 - b) for a, b in corners]
    elif rotation == 270:
        corners = [(b, 1 - a) for a, b in corners]
    (ax, ay), (bx, by) = corners
    return [round(v, 4) for v in (min(ax, bx), min(ay, by), abs(ax - bx), abs(ay - by))]


def _viewport(snap: httpx.Response, rotation: int) -> list[float] | None:
    """The snapshot's viewport header, turned like the picture, or None."""
    try:
        r = [float(v) for v in snap.headers["x-screeneye-viewport"].split(",")]
    except (KeyError, ValueError):
        return None
    return turned_rect(r, rotation) if len(r) == 4 else None


def _orientation(snap: httpx.Response) -> tuple[str | None, int]:
    """The phone's orientation and rotation for a frame: from the snapshot's
    own headers (taken with the frame), else from /status. Apps from before
    EYE-3 report neither: (None, 0)."""
    if "x-screeneye-rotation" in snap.headers:
        return (snap.headers.get("x-screeneye-orientation"),
                rotation_of(snap.headers["x-screeneye-rotation"]))
    st = _get("/status").json()
    return st.get("orientation"), rotation_of(st.get("rotation"))


def _settings(zoom, focus_x, focus_y, lock, ev, pan=None, upright=True) -> dict:
    p: dict[str, str] = {}
    if zoom is not None:
        p["zoom"] = str(zoom)
    if pan is not None:
        if len(pan) != 2:
            raise ValueError("pan is [x, y], the centre of the view as fractions of the whole frame")
        p["pan"] = f"{pan[0]},{pan[1]}"
    if focus_x is not None and focus_y is not None:
        p["focus"] = f"{focus_x},{focus_y}"
    if upright and ("pan" in p or "focus" in p):
        p["upright"] = "1"
    if lock is not None:
        p["lock"] = "1" if lock else "0"
    if ev is not None:
        p["ev"] = str(ev)
    return p


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def status(upright: bool = True) -> dict:
    """Camera state and the phone's URL: zoom (a multiple of the 1x lens),
    max zoom, opticalZoom (the lens step; zoom beyond it is a crop), active
    lens, focus mode, lens position, exposure bias, orientation, rotation (the
    clockwise turn that makes a raw frame upright), pan (the view's centre)
    and viewport ([x, y, w, h] of the whole frame), in the upright frame
    unless upright is false."""
    return {"url": _url(), **_get("/status", {"upright": "1"} if upright else None).json()}


@mcp.tool()
def set_camera(
    zoom: float | None = None,
    focus_x: float | None = None,
    focus_y: float | None = None,
    lock: bool | None = None,
    ev: float | None = None,
    pan: list[float] | None = None,
    upright: bool = True,
) -> dict:
    """Adjust the camera. Only the arguments given are changed.

    zoom: multiple of the 1x lens (0.5 is the ultra-wide where available).
        Past the lens's own zoom, the view is a crop of its frame, which pan
        moves.
    pan: [x, y], the centre of the view as fractions of the lens's whole
        frame ([0.5, 0.5] recentres). Only moves anything when zoomed past
        the lens; it's kept inside the frame.
    focus_x, focus_y: focus/exposure point, 0..1 across the zoomed view (the
        picture snapshot returns without full_frame; (0,0) top-left).
    upright: pan and focus are in the upright frame, as snapshot shows it
        (the default); false means the raw sensor frame.
    lock: true locks focus where it is; false returns to continuous autofocus.
    ev: exposure bias in stops; negative helps with bright screens.
    """
    return _get("/set", _settings(zoom, focus_x, focus_y, lock, ev, pan, upright)).json()


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
    upright: bool = True,
    pan: list[float] | None = None,
    full_frame: bool = False,
) -> dict:
    """Take a picture and save it as a JPEG; returns its path. Read the path to
    see it. If SCREENEYE_PATH_MAP applies, path is the caller's view of the
    file and host_path is where the server wrote it.

    Camera arguments are as for set_camera and are applied first; the camera
    then gets settle_ms to refocus before the frame is taken.
    upright: turn the frame the right way up, from the phone's orientation
        (the default); false gives the raw sensor frame, in landscape whichever
        way up the phone is. The result's orientation and rotated_deg (the
        clockwise turn applied) say what was done.
    pan: [x, y], where the view is centred when zoomed past the lens, as
        for set_camera. The result's viewport says what the picture covers:
        [x, y, w, h] of the lens's whole frame.
    full_frame: the lens's whole frame rather than the zoomed view, to see
        where to pan.
    crop: [x, y, w, h] as fractions of the picture (upright, unless upright
        is false), applied before scaling, to read a small part of the screen
        at full resolution.
    max_dim: longest side of the saved image, in pixels (0 = no scaling).
    """
    params = _settings(zoom, focus_x, focus_y, lock, ev, pan, upright)
    cam = None
    if params:
        cam = _get("/set", params).json()
        time.sleep(max(settle_ms, 0) / 1000)

    snap = _get("/snapshot.jpg", {"full": "1"} if full_frame else None)
    img = Image.open(io.BytesIO(snap.content))
    img.load()
    orientation, rotation = _orientation(snap) if upright else (
        snap.headers.get("x-screeneye-orientation"), 0)
    img = rotate_upright(img, rotation)
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
    out = {"path": map_path(path), "width": img.width, "height": img.height,
           "orientation": orientation, "rotated_deg": rotation}
    viewport = _viewport(snap, rotation)
    if viewport is not None:
        out["viewport"] = viewport
    if out["path"] != str(path):
        out["host_path"] = str(path)
    if cam is not None:
        out["camera"] = cam
    return out


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
