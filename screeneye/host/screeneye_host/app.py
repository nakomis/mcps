"""
ScreenEye host: receives frames pushed by the app's Save button and files them
on the share.

  POST /api/frames   multipart/form-data: `image` (JPEG) and `meta` (a JSON
                     object: zoom, lens, ev, focus, lensPosition, note, device,
                     taken). Writes
                       <root>/screeneye/YYYY-MM-DD/HHMMSS[-note].jpg
                     and a .json sidecar beside it, never overwriting. Replies
                     201 {"name", "path"} (path relative to <root>), or
                     {"error"} with a 4xx/5xx.
  GET  /health       {"ok": true, ...}

Environment variables:
  SCREENEYE_ROOT       The share's mount point (default /mnt/print-recordings)
  SCREENEYE_HOST_PORT  Port to listen on (default 8090)
  SCREENEYE_MAX_BYTES  Largest request accepted (default 30 MiB)
"""

import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger("screeneye-host")

SUBDIR = "screeneye"
DEFAULT_MAX_BYTES = 30 * 1024 * 1024
_JPEG_MAGIC = b"\xff\xd8\xff"
_SLUG_MAX = 60


def slugify(note: str | None) -> str:
    """A filename-safe, lower-case, ASCII form of the note ('' if nothing is left)."""
    if not note:
        return ""
    ascii_ = unicodedata.normalize("NFKD", note).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")
    return slug[:_SLUG_MAX].rstrip("-")


def save_frame(root: Path, image: bytes, sidecar: dict, note: str | None,
               now: datetime) -> Path:
    """Write the JPEG and its sidecar under root/screeneye/<date>/ and return the
    JPEG's path. An existing name gets a -2, -3, ... suffix; nothing is ever
    overwritten (O_EXCL, so two requests in the same second can't collide)."""
    day = root / SUBDIR / now.strftime("%Y-%m-%d")
    day.mkdir(parents=True, exist_ok=True)
    base = now.strftime("%H%M%S")
    slug = slugify(note)
    if slug:
        base = f"{base}-{slug}"
    n = 1
    while True:
        stem = base if n == 1 else f"{base}-{n}"
        jpg, meta = day / f"{stem}.jpg", day / f"{stem}.json"
        n += 1
        if meta.exists():
            continue
        try:
            with open(jpg, "xb") as f:
                f.write(image)
        except FileExistsError:
            continue
        break
    # Only the request that won the JPEG's name writes this sidecar.
    with open(meta, "x", encoding="utf-8") as f:
        json.dump({**sidecar, "name": jpg.name}, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return jpg


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The whole body, or None once it passes the limit."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


async def post_frame(request: Request) -> JSONResponse:
    cfg = request.app.state
    if not request.headers.get("content-type", "").startswith("multipart/form-data"):
        return _error(415, "expected multipart/form-data with `image` and `meta`")
    body = await _read_capped(request, cfg.max_bytes)
    if body is None:
        return _error(413, f"request larger than {cfg.max_bytes} bytes")

    async def replay():
        return {"type": "http.request", "body": body, "more_body": False}

    try:
        form = await Request(request.scope, replay).form(max_files=2, max_fields=16)
    except (HTTPException, ValueError) as e:  # malformed multipart, too many parts
        return _error(400, f"bad multipart body: {getattr(e, 'detail', e)}")

    try:
        upload = form.get("image")
        if upload is None or isinstance(upload, str):
            return _error(400, "missing `image` file part")
        image = await upload.read()
        ctype = (upload.content_type or "").lower()
        if ctype not in ("image/jpeg", "image/jpg") or not image.startswith(_JPEG_MAGIC):
            return _error(415, "`image` must be a JPEG (image/jpeg)")

        raw = form.get("meta")
        if raw is None:
            return _error(400, "missing `meta` field")
        if not isinstance(raw, str):
            raw = (await raw.read()).decode("utf-8", "replace")
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError as e:
            return _error(400, f"`meta` is not valid JSON: {e}")
        if not isinstance(meta, dict):
            return _error(400, "`meta` must be a JSON object")
    finally:
        await form.close()

    now = datetime.now().astimezone()
    sidecar = {
        **meta,
        "received": now.isoformat(timespec="seconds"),
        "client": request.client.host if request.client else None,
        "bytes": len(image),
    }
    note = meta.get("note") if isinstance(meta.get("note"), str) else None
    try:
        jpg = save_frame(cfg.root, image, sidecar, note, now)
    except OSError as e:
        log.exception("could not save frame")
        return _error(503, f"could not save the frame: {e.strerror or e}")
    rel = jpg.relative_to(cfg.root).as_posix()
    log.info("saved %s (%d bytes) from %s", rel, len(image), sidecar["client"])
    return JSONResponse({"name": jpg.name, "path": rel}, status_code=201)


async def health(request: Request) -> JSONResponse:
    cfg = request.app.state
    target = cfg.root / SUBDIR
    if not target.exists():  # the first frame creates it
        target = cfg.root
    return JSONResponse({"ok": True, "root": str(cfg.root),
                         "writable": os.access(target, os.W_OK)})


def create_app(root: Path | str | None = None, max_bytes: int | None = None) -> Starlette:
    app = Starlette(routes=[
        Route("/api/frames", post_frame, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ])
    app.state.root = Path(root or os.environ.get("SCREENEYE_ROOT", "/mnt/print-recordings"))
    app.state.max_bytes = max_bytes or int(os.environ.get("SCREENEYE_MAX_BYTES", DEFAULT_MAX_BYTES))
    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(create_app(), host=os.environ.get("SCREENEYE_HOST_BIND", "0.0.0.0"),
                port=int(os.environ.get("SCREENEYE_HOST_PORT", "8090")),
                proxy_headers=False, access_log=False)


if __name__ == "__main__":
    main()
