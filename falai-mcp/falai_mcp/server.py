#!/usr/bin/env python3
"""fal.ai MCP server — hosted image generation and editing.

Replaces the retired draw-things-mcp. Everything runs on fal.ai, so nothing
needs to be installed or running locally.

Every tool takes a `model` argument. The default is Seedream 5.0 Pro as of
2026-08-06; it was FLUX.2 [pro] before that, which was the correct choice when
this server was written eleven days earlier and last of six by the time it was
changed. See the dated measurement beside GENERATE_MODELS, and treat the
default as something to re-check rather than inherit.

The API key is read from the macOS keychain (service=fal.ai, account=api-key),
matching blog-content/review-pipeline/falai_batch.py. Set FAL_KEY to override.

fal's editing endpoints take image *URLs*, not uploads, so a local file has to
be reachable over HTTPS for the duration of one call. Images are staged in
S3 (nak-sandbox-falai-uploads, see ../infra) behind a short-lived presigned GET
and deleted as soon as the call returns. The bucket also has a 24-hour
lifecycle rule as a backstop for when that delete does not happen.

Images are uploaded at their original resolution. Pass max_dimension to
downscale first.
"""

import io
import mimetypes
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import boto3
import httpx
from botocore.config import Config as BotoConfig
from botocore.exceptions import (
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    ProfileNotFound,
    SSOError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageFilter
from pydantic import BaseModel, Field

mcp = FastMCP("falai-mcp")

# ── Models ────────────────────────────────────────────────────────────────────
#
# **The default below is perishable. Re-measure before trusting it.**
#
# Until 2026-08-06 this server hardcoded flux-2-pro, which was the right choice
# when it was written on 2026-07-26. Eleven days later it came last of six on a
# prompt needing eight distinct gilt titles on eight book spines:
#
#     seedream v5 pro   8/8 titles correct
#     gpt-image-2       8/8
#     nano-banana-pro   7/8
#     flux-2-pro        3/8   ("MOBY THE BEAGLE", "MOBY DI DICK")
#     ideogram v3       3/8   and ignored the composition constraints
#     qwen-image        2/8   ("SPEGIES", "SHESEHOLD MANAGEMENT")
#
# Nothing about FLUX got worse; the field moved underneath it. A constant that
# looks deliberate tells you nothing about its age, so the lesson is not "pick
# a better constant" — it is that the choice has to be overridable and dated.
# Hence `model` on every tool, and the date on every claim above.
#
# Endpoint ids are exactly as fal lists them. Note that the Seedream ones carry
# no `fal-ai/` prefix — adding one out of habit yields a 404.
GENERATE_MODELS = {
    "seedream": "https://fal.run/bytedance/seedream/v5/pro/text-to-image",
    "gpt-image": "https://fal.run/openai/gpt-image-2",
    "flux": "https://fal.run/fal-ai/flux-2-pro",
}
EDIT_MODELS = {
    "seedream": "https://fal.run/bytedance/seedream/v5/pro/edit",
    "gpt-image": "https://fal.run/openai/gpt-image-2/edit",
    "flux": "https://fal.run/fal-ai/flux-2-pro/edit",
}
# Object removal is a segment-and-inpaint operation rather than a general edit,
# and fal's purpose-built endpoint stays far more faithful to the rest of the
# frame than asking a general editor to delete something. There is no Seedream
# equivalent, so this tool keeps its own default.
REMOVE_MODELS = {
    "object-removal": "https://fal.run/fal-ai/object-removal",
}

# Image → 3D mesh, for printing. Like the image default above, this one is
# dated and perishable. Measured 2026-10-07 on the NakTV icon (a cream retro
# TV on white, 1024px), each model at the settings in _payload_3d. Prices are
# fal's per-generation list prices at those settings, on the same date:
#
#     hunyuan  $0.225  110s  one watertight body, 500k faces; the finest detail
#                            (all three dots, grille slats), and the only one to
#                            give the TV a proper CRT back the image never showed
#     trellis  $0.30    62s  faithful and well proportioned, not watertight: one
#                            main body plus ~330 loose fragments
#     tripo    $0.40   181s  good detail, shallowest depth; came back facing +X
#                            until orientation=align_image was set
#     pixal3d  $0.30   265s  1.5x deeper than wide, antennae lost, screen detail
#                            mostly gone
#
# Hunyuan is the default because it was the only one a slicer would take
# without repair, and it is also the cheapest. Its price rises by $0.15 each
# for PBR, multi-view input, or a custom face count — so _payload_3d sets none
# of them. Tripo's $0.40 is $0.20 untextured plus $0.20 for detailed geometry.
#
# TripoSR, the name most guides still give, has been withdrawn from fal — its
# endpoint 404s.
MODELS_3D = {
    "hunyuan": "fal-ai/hunyuan-3d/v3.1/pro/image-to-3d",
    "trellis": "fal-ai/trellis-2",
    "tripo": "tripo3d/h3.1/image-to-3d",
    "pixal3d": "fal-ai/pixal3d",
}

# Icons. Both styles are generated, then cut out with BiRefNet. A "pixel" one
# then goes through image2pixel, which snaps it to a true grid and quantises
# the palette. image2pixel does not resize to a requested size — it detects the
# grid the image already has — so the prompt has to ask for pixel art first.
#
# image2pixel's own background removal flood-fills from the corners, and
# Seedream's "solid" backgrounds are faint gradients: on 2026-10-13 it left a
# magenta slab behind. BiRefNet first, then image2pixel, is reliable.
#
# The two return different shapes: BiRefNet a single `image`, image2pixel an
# `images` list holding the snapped art at full size *and* at its native grid
# size (2037px and 97px on that run) — pick by size, not by position.
BG_REMOVE_ENDPOINT = "https://fal.run/fal-ai/birefnet/v2"
PIXELATE_ENDPOINT = "https://fal.run/fal-ai/image2pixel"
ICON_STYLES = {"flat", "pixel", "glyph"}

DEFAULT_MODEL = "seedream"
DEFAULT_REMOVE_MODEL = "object-removal"
DEFAULT_3D_MODEL = "hunyuan"

# Per-model "keep the input's dimensions" token for edit_image. Seedream v5
# edit rejects plain "auto" with a 422 and wants auto_1K/auto_2K instead,
# which keep the input's aspect ratio at a fixed pixel budget.
AUTO_SIZE = {"seedream": "auto_2K"}

# Which models accept a seed at all. Seedream v5 pro and gpt-image-2 have no
# seed field, so a caller asking for one gets told rather than quietly ignored
# — silently dropping it would let someone believe a run was reproducible.
SUPPORTS_SEED = {"flux"}

# nano-banana-pro is deliberately absent. It takes `aspect_ratio` and
# `resolution` instead of `image_size`, so it cannot share the payload shape
# below, and wiring it in without translating those would silently ignore every
# size argument. Worth adding, but as its own change.

DEFAULT_SAVE_DIR = Path.home() / "Pictures" / "falai-mcp"

BUCKET = os.environ.get("FALAI_BUCKET", "nak-sandbox-falai-uploads")
REGION = os.environ.get("AWS_REGION", "eu-west-2")
AWS_PROFILE = os.environ.get("AWS_PROFILE", "")

# botocore raises a different class depending on where the SSO session gives
# out — token cache missing, token present but expired, refresh refused.
_CREDENTIAL_EXCEPTIONS = (
    NoCredentialsError,
    CredentialRetrievalError,
    ProfileNotFound,
    SSOError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)

# ...and when the credentials were valid at client-construction time but have
# since lapsed, it surfaces as a ClientError with one of these codes instead.
_EXPIRED_ERROR_CODES = {
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidClientTokenId",
    "RequestExpired",
    "UnrecognizedClientException",
    "InvalidAccessKeyId",
    "AccessDenied",
}

# Long enough for fal to fetch the image, short enough that a leaked URL is
# worthless by the time anyone finds it.
URL_TTL_SECONDS = 900

# fal-ai/flux-2-pro/edit accepts at most 4 reference images.
MAX_INPUT_IMAGES = 4

ASPECTS = {
    "square_hd", "square",
    "portrait_4_3", "portrait_16_9",
    "landscape_4_3", "landscape_16_9",
}

_TIMEOUT = httpx.Timeout(300.0, connect=15.0)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _api_key() -> str:
    """fal.ai key from FAL_KEY, else the macOS keychain."""
    if key := os.environ.get("FAL_KEY", "").strip():
        return key
    try:
        return subprocess.run(
            ["security", "find-generic-password", "-s", "fal.ai", "-a", "api-key", "-w"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            "No fal.ai API key. Add one to the keychain with:\n"
            '  security add-generic-password -s "fal.ai" -a "api-key" -w <KEY>\n'
            "or set the FAL_KEY environment variable."
        ) from e


def _read_image(image_path: str, max_dimension: int) -> tuple[bytes, str]:
    """Image bytes and content type, downscaled only if explicitly asked."""
    path = Path(image_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"No such image: {path}")

    content_type = mimetypes.guess_type(path.name)[0] or "image/png"

    if max_dimension <= 0:
        # Original bytes, untouched — no re-encode, no quality loss.
        return path.read_bytes(), content_type

    with Image.open(path) as img:
        img.load()
        if max(img.size) <= max_dimension:
            return path.read_bytes(), content_type
        img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")

    return buf.getvalue(), "image/png"


class AwsLoginRequired(RuntimeError):
    """Raised when the SSO session has lapsed and only the user can fix it."""


# Addressed at the calling model, not at a human reading a log: it has to know
# to stop and relay this rather than retry, and that it cannot run the command
# itself (aws sso login opens a browser and blocks on interactive sign-in).
_LOGIN_MESSAGE = """\
AWS SSO credentials have expired or are missing{detail}.

STOP and tell the user to run this in their terminal, then retry:

    aws sso login

Do not attempt to run it yourself — it opens a browser for interactive
sign-in and will hang. Every profile shares one IAM Identity Center session,
so no --profile flag is needed.

Only the editing tools need AWS; generate_image still works without it.\
"""


def _login_required(exc: Exception) -> AwsLoginRequired:
    detail = f" for profile {AWS_PROFILE!r}" if AWS_PROFILE else ""
    return AwsLoginRequired(_LOGIN_MESSAGE.format(detail=detail) + f"\n\n({exc})")


@contextmanager
def _aws_credentials():
    """Translate every flavour of expired-credential error into one clear ask."""
    try:
        yield
    except _CREDENTIAL_EXCEPTIONS as e:
        raise _login_required(e) from e
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in _EXPIRED_ERROR_CODES:
            raise _login_required(e) from e
        raise


@contextmanager
def _staged_urls(
    image_paths: list[str], max_dimension: int = 0, ttl: int = URL_TTL_SECONDS
):
    """Upload images, yield presigned GET URLs, then delete them again.

    The delete runs in a finally block so a failed fal call still cleans up.
    """
    # Pin the regional endpoint. The default global s3.amazonaws.com host
    # answers with a 307 to the regional one, and fal's fetcher does not
    # follow redirects — the call comes back as an opaque 500.
    with _aws_credentials():
        s3 = boto3.client(
            "s3",
            region_name=REGION,
            endpoint_url=f"https://s3.{REGION}.amazonaws.com",
            config=BotoConfig(
                signature_version="s3v4", s3={"addressing_style": "virtual"}
            ),
        )
    keys: list[str] = []
    try:
        urls = []
        for image_path in image_paths:
            data, content_type = _read_image(image_path, max_dimension)
            key = f"{uuid.uuid4().hex}{Path(image_path).suffix or '.png'}"
            with _aws_credentials():
                s3.put_object(
                    Bucket=BUCKET, Key=key, Body=data, ContentType=content_type
                )
                keys.append(key)
                urls.append(
                    s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": BUCKET, "Key": key},
                        ExpiresIn=ttl,
                    )
                )
        yield urls
    finally:
        for key in keys:
            try:
                s3.delete_object(Bucket=BUCKET, Key=key)
            except Exception:  # noqa: BLE001 — lifecycle rule is the backstop
                pass


def _size_param(width: int, height: int, aspect: str, default: str) -> object:
    """Custom {width, height} when both are given, else a named preset."""
    if width > 0 and height > 0:
        return {"width": width, "height": height}
    if aspect:
        if aspect not in ASPECTS:
            raise ValueError(
                f"Unknown aspect {aspect!r}. Use one of: {', '.join(sorted(ASPECTS))} "
                "— or pass explicit width and height."
            )
        return aspect
    return default


def _save(image_bytes: bytes, prompt: str, save_dir: str, suffix: str) -> Path:
    out_dir = Path(save_dir).expanduser() if save_dir else DEFAULT_SAVE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\s-]", "", prompt)[:40].strip().replace(" ", "_") or "image"
    path = out_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slug}.{suffix}"
    path.write_bytes(image_bytes)
    return path


BALANCE_URL = "https://rest.fal.ai/billing/user_balance"
LOW_BALANCE = float(os.environ.get("FALAI_LOW_BALANCE", "5"))


def _balance_warning() -> list[str]:
    """Warn when the fal.ai balance is running low. Never fails a call.

    Checked after the image has already been fetched, so a billing endpoint
    that is slow, down, or has changed shape can only cost a warning — never
    the picture the caller actually asked for. Every failure path here is
    swallowed deliberately: an unreadable balance is not a reason to lose work
    that has already been paid for.

    The endpoint returns a bare JSON number, e.g. `29.0459475`, not an object.
    """
    try:
        with httpx.Client(timeout=httpx.Timeout(8.0, connect=4.0)) as client:
            r = client.get(BALANCE_URL, headers={"Authorization": f"Key {_api_key()}"})
            if r.status_code != 200:
                return []
            balance = float(r.text.strip())
    except Exception:  # noqa: BLE001 — see docstring
        return []

    if balance >= LOW_BALANCE:
        return []
    return [
        f"fal.ai balance is ${balance:.2f}, below the ${LOW_BALANCE:.2f} warning "
        f"threshold. Tell the user — top up at https://fal.ai/dashboard/billing. "
        f"Raise FALAI_LOW_BALANCE to change when this fires."
    ]


def _endpoint(table: dict[str, str], model: str, default: str) -> str:
    """Resolve a model name to its fal endpoint, or explain what is valid."""
    name = (model or default).strip().lower()
    try:
        return table[name]
    except KeyError:
        raise ValueError(
            f"Unknown model {model!r}. Use one of: {', '.join(sorted(table))}."
        ) from None


def _apply_seed(payload: dict, model: str, seed: int) -> list[str]:
    """Add the seed if the model takes one; otherwise say so out loud."""
    if seed < 0:
        return []
    name = (model or DEFAULT_MODEL).strip().lower()
    if name in SUPPORTS_SEED:
        payload["seed"] = seed
        return []
    return [
        f"seed={seed} was ignored: {name} has no seed parameter, so this image "
        f"cannot be reproduced. Use model='flux' if you need a reproducible seed."
    ]


def _call(url: str, payload: dict) -> dict:
    headers = {"Authorization": f"Key {_api_key()}", "Content-Type": "application/json"}
    with httpx.Client(timeout=_TIMEOUT) as client:
        r = client.post(url, json=payload, headers=headers)
        if r.status_code == 401:
            raise RuntimeError("fal.ai rejected the API key (401). Check the keychain entry.")
        if r.status_code >= 400:
            raise RuntimeError(f"fal.ai returned {r.status_code}: {r.text[:500]}")
        return r.json()


class ImageResult(BaseModel):
    """What a tool hands back. Typed so callers can inspect it, not parse it."""

    path: str = Field(description="Absolute path to the saved image. Read it to view.")
    width: int = Field(description="Actual width in pixels")
    height: int = Field(description="Actual height in pixels")
    size_kb: int = Field(description="File size in kilobytes")
    seed: int | None = Field(default=None, description="Seed used — reuse to reproduce")
    requested_width: int = Field(default=0, description="Width asked for, 0 if unspecified")
    requested_height: int = Field(default=0, description="Height asked for, 0 if unspecified")
    size_honoured: bool = Field(
        default=True,
        description="False when the returned image is not the size requested",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Anything the caller should relay to the user. Usually empty.",
    )


def _fetch_result(
    result: dict,
    prompt: str,
    save_dir: str,
    fmt: str,
    requested: tuple[int, int] = (0, 0),
    extra_warnings: list[str] | None = None,
) -> ImageResult:
    images = result.get("images") or []
    if not images:
        raise RuntimeError("fal.ai returned no images.")

    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        data = client.get(images[0]["url"]).content
    path = _save(data, prompt, save_dir, fmt)

    # Measure the bytes rather than trusting the response. Seedream returns
    # `width` and `height` as explicit nulls — the keys are present, so a
    # `.get(k, 0)` default never fires and int(None) raises. FLUX returns real
    # integers, so this only surfaced when the default model changed.
    #
    # The file is the ground truth in any case: it is what the caller will
    # open, and it is what size_honoured below should be judged against.
    meta = images[0]
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
    except Exception:  # noqa: BLE001 — fall back to whatever fal reported
        width = int(meta.get("width") or 0)
        height = int(meta.get("height") or 0)
    req_w, req_h = requested

    warnings: list[str] = list(extra_warnings or [])
    warnings += _balance_warning()
    honoured = True
    # fal silently clamps below ~256px and rounds odd dimensions to its own
    # grid — no error, no warning, just a different size in the response. Say
    # so, rather than letting the caller assume it got what it asked for.
    if req_w > 0 and req_h > 0 and (req_w, req_h) != (width, height):
        honoured = False
        warnings.append(
            f"Requested {req_w}x{req_h} but fal.ai returned {width}x{height}. "
            "It clamps below 256px and snaps to its own size grid. "
            "Tell the user, and downscale locally if the exact size matters."
        )

    return ImageResult(
        path=str(path),
        width=width,
        height=height,
        size_kb=len(data) // 1024,
        seed=result.get("seed"),
        requested_width=req_w,
        requested_height=req_h,
        size_honoured=honoured,
        warnings=warnings,
    )


# ── 3D helpers ────────────────────────────────────────────────────────────────

# Generation takes one to five minutes — Pixal3D took 265s — which is too close
# to fal.run's synchronous limit, so 3D calls go through the queue instead.
QUEUE_POLL_SECONDS = 5
QUEUE_TIMEOUT_SECONDS = 15 * 60


def _payload_3d(name: str, image_url: str, geometry_only: bool) -> dict:
    """Each endpoint names its fields differently; this is the translation."""
    if name == "hunyuan":
        return {
            "input_image_url": image_url,
            # Geometry is the untextured "white model" — all a printer needs.
            "generate_type": "Geometry" if geometry_only else "Normal",
        }
    if name == "tripo":
        return {
            "image_url": image_url,
            "geometry_quality": "detailed",
            "texture": not geometry_only,
            "pbr": not geometry_only,
            # Unset, it returned 2M faces (a 100 MB STL). 500k matches Hunyuan.
            "face_limit": 500_000,
            # Without this it came back side-on, facing +X.
            "orientation": "align_image",
        }
    # trellis and pixal3d always texture; there is no geometry-only switch.
    return {"image_url": image_url}


def _queue_call(endpoint: str, payload: dict) -> dict:
    """Submit to fal's queue, poll until done, return the result.

    A job is paid for once submitted, so a flaky poll must not throw it away:
    transient status errors are retried until the deadline, and every error
    raised after submission carries the request id so the result can still be
    collected by hand.
    """
    headers = {"Authorization": f"Key {_api_key()}"}
    with httpx.Client(timeout=_TIMEOUT) as client:
        r = client.post(f"https://queue.fal.run/{endpoint}", json=payload, headers=headers)
        if r.status_code == 401:
            raise RuntimeError("fal.ai rejected the API key (401). Check the keychain entry.")
        if r.status_code >= 400:
            raise RuntimeError(f"fal.ai returned {r.status_code}: {r.text[:500]}")
        job = r.json()
        try:
            request_id = job["request_id"]
            status_url, response_url = job["status_url"], job["response_url"]
        except KeyError:
            raise RuntimeError(f"Unexpected fal.ai queue response: {str(job)[:300]}") from None

        deadline = time.monotonic() + QUEUE_TIMEOUT_SECONDS
        failures = 0
        while True:
            try:
                r = client.get(status_url, headers=headers)
                r.raise_for_status()  # fal answers 202 while waiting, 200 when done
                status = r.json().get("status")
                failures = 0
            except (httpx.HTTPError, ValueError) as e:
                failures += 1
                if failures >= 6:
                    raise RuntimeError(
                        f"Lost contact with fal.ai job {request_id} ({e}). It may still "
                        f"finish; its result will be at {response_url}"
                    ) from e
                status = None
            # Anything other than the two waiting states is final: COMPLETED,
            # or an error state whose detail is in the result.
            if status not in (None, "IN_QUEUE", "IN_PROGRESS"):
                break
            if time.monotonic() > deadline:
                try:
                    client.put(status_url.removesuffix("/status") + "/cancel", headers=headers)
                except httpx.HTTPError:
                    pass  # the timeout below is the error worth reporting
                raise RuntimeError(
                    f"fal.ai job {request_id} still running after "
                    f"{QUEUE_TIMEOUT_SECONDS // 60} minutes; asked fal to cancel it."
                )
            time.sleep(QUEUE_POLL_SECONDS)

        r = client.get(response_url, headers=headers)
        if r.status_code >= 400:
            raise RuntimeError(
                f"fal.ai job {request_id} failed ({status}, {r.status_code}): {r.text[:500]}"
            )
        return r.json()


def _to_stl(glb_path: Path, width_mm: float):
    """Write a print-ready STL beside the GLB: Z-up, width_mm wide, on z=0.

    Width is the model's own X extent, i.e. side to side as seen in the input
    image — every generator here faces the camera along +Z.
    """
    # Imported here so that a broken numpy/scipy wheel can only break this
    # tool, not image generation.
    import numpy as np
    import trimesh

    mesh = trimesh.load(glb_path, force="mesh")
    # Textured meshes split vertices along every UV seam, so the raw mesh looks
    # like thousands of loose bodies. Weld on position alone.
    mesh = trimesh.Trimesh(mesh.vertices, mesh.faces, process=False)
    mesh.merge_vertices(merge_tex=True, merge_norm=True)
    # glTF is Y-up; slicers are Z-up.
    mesh.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))
    # Size and seat the model by its largest piece. Loose fragments (Trellis
    # left ~330) can sit outside it, and measuring them would scale the wrong
    # thing and leave the main body floating above the bed.
    # Connected components over face adjacency, not mesh.split(): split
    # probes each piece for holes, which needs networkx and is slow.
    pieces = trimesh.graph.connected_components(
        mesh.face_adjacency, nodes=np.arange(len(mesh.faces)), min_len=1
    )
    largest = max(pieces, key=len)
    main = mesh.vertices[np.unique(mesh.faces[largest])]
    low, high = main.min(axis=0), main.max(axis=0)
    scale = width_mm / (high[0] - low[0])
    mesh.apply_translation(-low)
    mesh.apply_scale(scale)
    stl_path = glb_path.with_suffix(".stl")
    mesh.export(stl_path)
    return stl_path, mesh, len(pieces)


class Model3DResult(BaseModel):
    """What generate_3d hands back."""

    glb_path: str = Field(description="The model as fal returned it (GLB, Y-up)")
    stl_path: str = Field(description="Print-ready STL: Z-up, scaled, sitting on z=0")
    preview_path: str | None = Field(
        default=None, description="A render of the model, when the endpoint supplies one"
    )
    model: str = Field(description="Which generator made it")
    seconds: int = Field(description="Wall-clock time, including the queue")
    size_mm: list[float] = Field(description="STL extents: width, depth, height in mm")
    faces: int = Field(description="Triangle count")
    watertight: bool = Field(description="True when the mesh is closed and slicer-ready")
    bodies: int = Field(description="Separate pieces. 1 is ideal; more means loose fragments")
    warnings: list[str] = Field(default_factory=list)


# ── Icon helpers ──────────────────────────────────────────────────────────────

# Chrome draws a tab favicon at 16 CSS px — 32 physical on a Retina display —
# so the icon has to survive both. 48 is the Windows desktop size; 180 is the
# Apple touch icon; 192 and 512 are what a web manifest asks for.
DEFAULT_ICON_SIZES = [16, 32, 48, 180, 192, 512]
# A menu bar extra is 22pt tall at most and 16-18pt is the usual glyph, at
# 1x and 2x. The conversation-memory tray icons are 44x44 (22pt @2x).
DEFAULT_GLYPH_SIZES = [16, 18, 22, 32, 36, 44]
ICO_SIZES = [16, 32, 48]

# Chrome's own tab-strip colours, so the preview shows the icon where it will
# actually live, in both themes.
PREVIEW_BACKGROUNDS = [(241, 243, 244, 255), (53, 54, 58, 255)]
# ...and the macOS menu bar's, for glyphs. A template glyph is shown black on
# the light bar; the dark row shows it as macOS draws it there, in white.
MENU_BAR_BACKGROUNDS = [(236, 236, 236, 255), (30, 30, 30, 255)]
PREVIEW_ZOOM = 8

# Small icons fail on detail, not on colour, so the template insists on bold
# shapes.
ICON_PROMPTS = {
    "flat": (
        "{subject}, as a flat vector app icon. Bold, simple silhouette made of "
        "thick shapes with no fine detail, so it stays recognisable at 16 pixels. "
        "At most four flat colours{colours}. No gradients, no text, no glow, no "
        "drop shadow. Centred and filling most of the frame, on a plain pure "
        "white background."
    ),
    "pixel": (
        "{subject}, as a 32x32 pixel-art sprite. Chunky square pixels on a clear "
        "grid, limited palette{colours}, hard edges, no anti-aliasing, no text, "
        "no shadow. Centred, on a plain pure white background."
    ),
    # Details as cut-outs, not as lines of another colour: a glyph has one
    # colour, so a dark lens drawn on a dark head would vanish into it.
    "glyph": (
        "{subject}, as a monochrome glyph icon in the style of an SF Symbol. "
        "Solid black filled shapes only, bold and simple. Only a few large "
        "interior details, such as lenses, shown as white cut-outs; no thin "
        "panel lines. No outlines-only drawing, no grey, no shading, no text. "
        "Centred, on a plain pure white background."
    ),
}

# Luminance thresholds for turning a black-on-white glyph into alpha. Below
# INK is fully opaque, above PAPER fully transparent, linear in between so
# anti-aliased edges stay smooth. PAPER sits below pure white because
# generated "white" backgrounds are never quite 255.
GLYPH_INK, GLYPH_PAPER = 40, 230


def _square_on_alpha(img: Image.Image, margin: float) -> tuple[Image.Image, list[str]]:
    """Crop to the visible pixels and centre them on a transparent square.

    margin is a fraction of the content's longest side, added on every side.
    """
    img = img.convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox is None:
        raise ValueError(
            "The cut-out is empty: background removal took the whole image. "
            "Try a different prompt or a plainer source image."
        )
    warnings = []
    if bbox == (0, 0, *img.size):
        warnings.append(
            "Nothing was trimmed: the icon touches every edge, which usually "
            "means the background was not removed. Check the preview."
        )
    content = img.crop(bbox)
    side = max(content.size)
    pad = round(side * margin)
    canvas = Image.new("RGBA", (side + 2 * pad,) * 2, (0, 0, 0, 0))
    canvas.paste(content, (pad + (side - content.width) // 2, pad + (side - content.height) // 2))
    return canvas, warnings


def _glyph_from_luminance(img: Image.Image) -> Image.Image:
    """Black glyph whose alpha is the source's darkness: ink opaque, paper gone."""
    lum = img.convert("L")
    span = GLYPH_PAPER - GLYPH_INK
    alpha = lum.point(lambda v: 255 if v <= GLYPH_INK else 0 if v >= GLYPH_PAPER
                      else round(255 * (GLYPH_PAPER - v) / span))
    glyph = Image.new("RGBA", img.size, (0, 0, 0, 0))
    glyph.putalpha(alpha)
    return glyph


def _render_glyph(img: Image.Image, size: int) -> Image.Image:
    """Downscale a glyph, first filling cut-outs too thin to survive the size.

    A white panel line narrower than an output pixel does not vanish when
    downscaled; it averages into grey and the glyph looks mottled. So the
    alpha is taken to 4x the target, closed (dilate then erode) with a 5px
    kernel, which fills any gap under ~1 output pixel and leaves bigger holes
    alone, and only then brought down to size.
    """
    work = img.getchannel("A").resize((size * 4,) * 2, Image.LANCZOS)
    work = work.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(5))
    alpha = work.resize((size, size), Image.LANCZOS)
    # A glyph is one colour throughout, so its RGB survives any resampling.
    out = img.convert("RGB").resize((size, size), Image.NEAREST).convert("RGBA")
    out.putalpha(alpha)
    return out


def _tint(img: Image.Image, colour: str) -> Image.Image:
    """The same shape, every pixel recoloured to colour; alpha untouched."""
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", colour.strip()):
        raise ValueError(f"Tint colours must be #RRGGBB (got {colour!r}).")
    rgb = tuple(int(colour.strip()[i:i + 2], 16) for i in (1, 3, 5))
    tinted = Image.new("RGBA", img.size, rgb + (0,))
    tinted.putalpha(img.convert("RGBA").getchannel("A"))
    return tinted


def _pixel_size_warnings(native: int, sizes: list[int]) -> list[str]:
    """Sizes a pixel sprite cannot be scaled to without dropping pixels."""
    uneven = [s for s in sizes if s % native]
    if not uneven:
        return []
    return [
        f"The sprite is {native}x{native}, so sizes {', '.join(map(str, uneven))} "
        "are not whole multiples of it: nearest-neighbour scaling drops or doubles "
        "some pixels there. Check them in the preview."
    ]


def _preview_sheet(
    icons: dict[int, Image.Image], backgrounds=PREVIEW_BACKGROUNDS, invert_on_dark=False
) -> Image.Image:
    """Each small size zoomed, then every size at 1x, on light and dark tabs.

    invert_on_dark draws black glyphs white on the dark row, as macOS does
    with a template image.
    """
    pad = 16
    zoomed = [s for s in sorted(icons) if s <= 48] or [min(icons)]
    zoom_w = sum(s * PREVIEW_ZOOM + pad for s in zoomed)
    actual_w = sum(s + pad for s in sorted(icons))
    panel_w = max(zoom_w, actual_w) + pad
    panel_h = pad + max(zoomed) * PREVIEW_ZOOM + pad + max(icons) + pad

    sheet = Image.new("RGBA", (panel_w, panel_h * len(backgrounds)))
    for row, colour in enumerate(backgrounds):
        panel = Image.new("RGBA", (panel_w, panel_h), colour)
        if invert_on_dark and sum(colour[:3]) < 384:
            icons = {s: _tint(i, "#FFFFFF") for s, i in icons.items()}
        x = pad
        for s in zoomed:
            big = icons[s].resize((s * PREVIEW_ZOOM,) * 2, Image.NEAREST)
            panel.alpha_composite(big, (x, pad))
            x += s * PREVIEW_ZOOM + pad
        x, y = pad, pad + max(zoomed) * PREVIEW_ZOOM + pad
        for s in sorted(icons):
            panel.alpha_composite(icons[s], (x, y))
            x += s + pad
        sheet.paste(panel, (0, row * panel_h))
    return sheet


class _IconSet(BaseModel):
    files: dict[int, Path]
    ico: Path
    preview: Path
    warnings: list[str]


def _export_icon_set(
    img: Image.Image, out_dir: Path, sizes: list[int], resample: int, margin: float,
    glyph: bool = False, template: bool = False,
) -> _IconSet:
    """Write icon-<size>.png for each size, favicon.ico and preview.png."""
    square, warnings = _square_on_alpha(img, margin)
    out_dir.mkdir(parents=True, exist_ok=True)

    render = (lambda s: _render_glyph(square, s)) if glyph else (
        lambda s: square.resize((s, s), resample))
    rendered = {s: render(s) for s in set(sizes) | set(ICO_SIZES)}
    files = {}
    for s in sorted(set(sizes)):
        files[s] = out_dir / f"icon-{s}.png"
        rendered[s].save(files[s])

    # Hand Pillow each ICO size already rendered; left to itself it would
    # resample them all from one image with its own filter.
    ico = out_dir / "favicon.ico"
    largest = max(ICO_SIZES)
    rendered[largest].save(
        ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES],
        append_images=[rendered[s] for s in ICO_SIZES if s != largest],
    )

    preview = out_dir / "preview.png"
    shown = {s: rendered[s] for s in sorted(set(sizes))}
    if glyph:
        _preview_sheet(shown, MENU_BAR_BACKGROUNDS, invert_on_dark=template).save(preview)
    else:
        _preview_sheet(shown).save(preview)
    return _IconSet(files=files, ico=ico, preview=preview, warnings=warnings)


def _download(url: str) -> bytes:
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        r = client.get(url)
        r.raise_for_status()
        return r.content


class IconResult(BaseModel):
    """What generate_icon hands back."""

    dir: str = Field(description="Folder holding everything below")
    preview_path: str = Field(
        description="Every size on light and dark tab colours, small ones zoomed 8x. "
        "Read this first to judge the icon."
    )
    ico_path: str = Field(description="favicon.ico containing 16, 32 and 48")
    files: dict[int, str] = Field(description="PNG path for each requested size")
    source_path: str = Field(description="The full-size image the icon was made from")
    cutout_path: str = Field(description="The source after background removal or pixel snapping")
    style: str
    native_size: int | None = Field(
        default=None, description="pixel only: the sprite's own size in pixels"
    )
    palette: list[str] = Field(default_factory=list, description="pixel only: hex colours")
    tints: dict[str, str] = Field(
        default_factory=dict,
        description="glyph only: tint name -> folder holding that colour's PNGs",
    )
    warnings: list[str] = Field(default_factory=list)


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def generate_image(
    prompt: str,
    width: int = 0,
    height: int = 0,
    aspect: str = "",
    output_format: str = "png",
    seed: int = -1,
    save_dir: str = "",
    model: str = DEFAULT_MODEL,
) -> ImageResult:
    """
    Generate an image from a text prompt, using Seedream 5.0 Pro on fal.ai.

    Saves the image and returns its absolute path — use the Read tool on that
    path to view it.

    Args:
        prompt:        What to generate. Detailed prompts work better.
        width:         Exact width in pixels. Pass with height for a custom size.
        height:        Exact height in pixels. Pass with width for a custom size.
        aspect:        Named size instead of width/height — one of square_hd,
                       square, portrait_4_3, portrait_16_9, landscape_4_3,
                       landscape_16_9. Ignored when width and height are given.
        output_format: "png" (default) or "jpeg".
        seed:          Seed for reproducibility (-1 = random). Only "flux"
                       supports this; other models report it as ignored rather
                       than pretending the result is reproducible.
        save_dir:      Directory to save into (default ~/Pictures/falai-mcp).
        model:         "seedream" (default), "gpt-image", or "flux".

                       Prefer the default for anything containing text.
                       Measured 2026-08-06 on eight book-spine titles: seedream
                       and gpt-image got 8/8, flux 3/8. See the note beside
                       GENERATE_MODELS — that measurement has a date on it for
                       a reason, and this default is expected to go stale.
    """
    url = _endpoint(GENERATE_MODELS, model, DEFAULT_MODEL)
    payload = {
        "prompt": prompt,
        "image_size": _size_param(width, height, aspect, "landscape_4_3"),
        "output_format": output_format,
    }
    notes = _apply_seed(payload, model, seed)

    return _fetch_result(
        _call(url, payload), prompt, save_dir, output_format, (width, height), notes
    )


@mcp.tool()
def edit_image(
    prompt: str,
    image_paths: list[str],
    width: int = 0,
    height: int = 0,
    aspect: str = "",
    max_dimension: int = 0,
    output_format: str = "png",
    seed: int = -1,
    save_dir: str = "",
    model: str = DEFAULT_MODEL,
) -> ImageResult:
    """
    Edit an existing image with a natural-language instruction, using
    Seedream 5.0 Pro edit on fal.ai.

    Note this re-renders the whole image, so untouched areas shift slightly.
    To delete an object while leaving the rest of the photo alone, prefer
    remove_object — it is purpose-built and far more faithful.

    Describe the change you want, not the whole scene — e.g. "make the sky
    overcast", "change the car to red", "delete the text on the sign".
    The original file is never modified; the result is saved alongside it and
    the new path returned. Use the Read tool on that path to view it.

    Phrasing matters: the model tends to ignore soft requests. "Remove the
    dog from the lawn" left the dog untouched, while "Delete the golden
    retriever completely. The lawn must be entirely empty grass where the dog
    was" worked. Be explicit and state the required end result.

    By default the output keeps the input's dimensions.

    Args:
        prompt:        The edit to make, in plain language.
        image_paths:   Absolute path(s) to the input image(s). The first is the
                       image being edited; any others act as extra references
                       (style, or an object to insert). Maximum 4.
        width:         Exact output width. Pass with height to resize.
        height:        Exact output height. Pass with width to resize.
        aspect:        Named output size — see generate_image. Ignored when
                       width and height are given.
        max_dimension: Downscale inputs to this longest edge before upload.
                       0 (default) uploads at full resolution.
        output_format: "png" (default) or "jpeg".
        seed:          Seed for reproducibility (-1 = random). Only "flux"
                       supports this; other models report it as ignored.
        save_dir:      Directory to save into (default: alongside the input).
        model:         "seedream" (default), "gpt-image", or "flux".

                       Seedream is region-precise — it changes what you asked
                       for and leaves the rest of the frame alone — and it is
                       far better at text. Adding eight author names beneath
                       eight existing gilt titles came back with every name
                       correct and every original title intact.
    """
    if not image_paths:
        raise ValueError("edit_image needs at least one input image path.")
    if len(image_paths) > MAX_INPUT_IMAGES:
        raise ValueError(
            f"At most {MAX_INPUT_IMAGES} input images (got {len(image_paths)})."
        )

    # Default to writing next to the source image rather than the shared
    # gallery — an edit belongs with what it was edited from.
    target = save_dir or str(Path(image_paths[0]).expanduser().parent)

    url = _endpoint(EDIT_MODELS, model, DEFAULT_MODEL)
    with _staged_urls(image_paths, max_dimension) as urls:
        payload = {
            "prompt": prompt,
            "image_urls": urls,
            # "auto" preserves the input's dimensions unless asked otherwise.
            "image_size": _size_param(
                width, height, aspect,
                AUTO_SIZE.get((model or DEFAULT_MODEL).strip().lower(), "auto"),
            ),
            "output_format": output_format,
        }
        notes = _apply_seed(payload, model, seed)
        result = _call(url, payload)

    return _fetch_result(result, prompt, target, output_format, (width, height), notes)


@mcp.tool()
def remove_object(
    image_path: str,
    object_description: str,
    mask_expansion: int = 15,
    quality: str = "best_quality",
    max_dimension: int = 0,
    output_format: str = "png",
    save_dir: str = "",
    model: str = DEFAULT_REMOVE_MODEL,
) -> ImageResult:
    """
    Delete an object from a photo, leaving the rest of the image intact.

    This is the tool for "take the dog out of this photo" — it segments the
    named object, erases it, and fills the gap with plausible background.
    Unlike edit_image it does not re-render the whole scene, so everything
    else stays pixel-faithful.

    The original file is never modified; the result is saved alongside it and
    the new path returned. Use the Read tool on that path to view it.

    Known limits, from testing rather than the vendor's blurb:
      - Cast shadows usually survive. Removing a dog leaves its shadow on the
        grass. There is no setting that fixes this — see below.
      - Naming the shadow makes things worse, not better. "the dog and its
        shadow" confused the segmenter into mangling the whole lawn.
      - mask_expansion above ~25 degrades badly; 15 (the default) was best.
        Raising it did not capture the shadow, it just smeared more.
      - Faint artefacts can appear on whatever was directly behind the object.
    If a leftover shadow matters, follow up with edit_image on the result and
    ask explicitly for the shadow to go — at the cost of a full re-render.

    Args:
        image_path:         Absolute path to the photo.
        object_description: What to remove, in plain words — "the dog",
                            "the person on the left", "the bin".
        mask_expansion:     Pixels to grow the detected mask by (0-50,
                            default 15). Raise it if traces are left behind.
        quality:            low_quality, medium_quality, high_quality, or
                            best_quality (default).
        max_dimension:      Downscale the input to this longest edge before
                            upload. 0 (default) uploads at full resolution.
        output_format:      "png" (default) or "jpeg".
        save_dir:           Directory to save into (default: alongside input).
        model:              Which removal endpoint to use. Only
                            "object-removal" exists today, and it is the
                            default; the argument is here so the other two
                            tools' `model` means the same thing everywhere.

                            Note this is NOT the endpoint's own `model` field,
                            which fal uses for the quality tier — that one is
                            set by `quality` above. Same word, two meanings,
                            hence this paragraph.

                            To delete something with Seedream instead, call
                            edit_image and say so explicitly; it re-renders the
                            whole frame, which is the trade this tool exists to
                            avoid.
    """
    if not 0 <= mask_expansion <= 50:
        raise ValueError(f"mask_expansion must be 0-50 (got {mask_expansion}).")

    target = save_dir or str(Path(image_path).expanduser().parent)

    url = _endpoint(REMOVE_MODELS, model, DEFAULT_REMOVE_MODEL)
    with _staged_urls([image_path], max_dimension) as urls:
        result = _call(url, {
            "image_url": urls[0],
            "prompt": object_description,
            "model": quality,
            "mask_expansion": mask_expansion,
        })

    return _fetch_result(result, f"removed_{object_description}", target, output_format)


@mcp.tool()
def generate_3d(
    image_path: str,
    model: str = DEFAULT_3D_MODEL,
    width_mm: float = 80.0,
    geometry_only: bool = True,
    max_dimension: int = 1024,
    save_dir: str = "",
) -> Model3DResult:
    """
    Turn an image into a 3D model for printing, on fal.ai. Defaults to
    Hunyuan3D v3.1 Pro. Takes one to five minutes.

    Saves the GLB as fal returns it, plus an STL that is ready to slice:
    Z-up, scaled to width_mm wide, sitting on the bed. Returns both paths and
    a printability report (watertight, bodies, size).

    Get the input right first; it matters more than the model. Use a single
    object, centred, on a plain white background. A logo on a coloured tile
    gets the tile modelled as a slab — run edit_image first to put the object
    on white. The models invent whatever the image cannot show, such as the
    back and the depth.

    Args:
        image_path:    Absolute path to the input image.
        model:         "hunyuan" (default), "trellis", "tripo", or "pixal3d".

                       Measured 2026-10-07 on a retro-TV logo: hunyuan was the
                       only one to return a single watertight body, had the
                       finest detail, and gave the TV a believable back. trellis was faithful but left ~330 loose
                       fragments; tripo was good but shallow; pixal3d got the
                       proportions badly wrong (far too deep). See MODELS_3D. Like every
                       default here, re-check it rather than trusting it.
        width_mm:      Width of the STL in millimetres (default 80). The models
                       have no real-world scale, so this sets it.
        geometry_only: Skip textures (default True) — a printer can't use them,
                       and it is faster. Only hunyuan and tripo honour this;
                       trellis and pixal3d always texture.
        max_dimension: Downscale the input to this longest edge before upload
                       (default 1024). 0 uploads at full resolution.
        save_dir:      Directory to save into (default: alongside the input).
    """
    if width_mm <= 0:
        raise ValueError(f"width_mm must be positive (got {width_mm}).")
    name = (model or DEFAULT_3D_MODEL).strip().lower()
    endpoint = _endpoint(MODELS_3D, name, DEFAULT_3D_MODEL)

    started = time.monotonic()
    # The URL must outlive the queue: fal fetches the image only when the job
    # starts, which on a busy queue can be minutes after submission.
    with _staged_urls([image_path], max_dimension, ttl=QUEUE_TIMEOUT_SECONDS + 300) as urls:
        result = _queue_call(endpoint, _payload_3d(name, urls[0], geometry_only))
    seconds = int(time.monotonic() - started)

    mesh_file = result.get("model_glb") or result.get("model_mesh")
    if not mesh_file:
        raise RuntimeError(f"fal.ai returned no model: {str(result)[:300]}")

    target = save_dir or str(Path(image_path).expanduser().parent)
    # Model first, so the slug's 40-character cut never drops it.
    label = f"{name}_{Path(image_path).stem}"
    warnings: list[str] = []
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        r = client.get(mesh_file["url"])
        if r.status_code >= 400:
            raise RuntimeError(
                f"Could not download the model ({r.status_code}). It was paid for and "
                f"is at {mesh_file['url']} for a while yet."
            )
        glb_path = _save(r.content, label, target, "glb")
        preview = result.get("thumbnail") or result.get("rendered_image")
        preview_path = None
        if preview:
            try:
                r = client.get(preview["url"])
                r.raise_for_status()
                preview_path = str(_save(r.content, label, target, "png"))
            except httpx.HTTPError as e:
                warnings.append(f"The preview image could not be downloaded ({e}).")

    if not (mesh_file.get("file_name") or "x.glb").lower().endswith(".glb"):
        warnings.append(
            f"{name} returned {mesh_file.get('file_name')!r}, not a GLB; the STL "
            "conversion may be wrong."
        )

    stl_path, mesh, bodies = _to_stl(glb_path, width_mm)
    if not mesh.is_watertight:
        warnings.append(
            "The mesh is not watertight. PrusaSlicer will try to repair it on "
            "import; check the sliced preview for holes before printing."
        )
    if bodies > 1:
        warnings.append(
            f"The mesh is {bodies} separate pieces. Small ones are usually loose "
            "fragments that print as debris; delete them in the slicer. size_mm "
            "spans every piece; the largest was used for scale and bed placement."
        )
    warnings += _balance_warning()

    return Model3DResult(
        glb_path=str(glb_path),
        stl_path=str(stl_path),
        preview_path=preview_path,
        model=name,
        seconds=seconds,
        size_mm=[round(float(x), 1) for x in mesh.extents],
        faces=len(mesh.faces),
        watertight=bool(mesh.is_watertight),
        bodies=int(bodies),
        warnings=warnings,
    )


@mcp.tool()
def generate_icon(
    subject: str = "",
    image_path: str = "",
    style: str = "flat",
    colours: str = "",
    model: str = DEFAULT_MODEL,
    sizes: list[int] | None = None,
    margin: float = 0.06,
    max_colors: int = 16,
    tints: dict[str, str] | None = None,
    reference_paths: list[str] | None = None,
    save_dir: str = "",
) -> IconResult:
    """
    Make a favicon / app icon set: transparent PNGs at every size, a
    favicon.ico (16, 32, 48), and a preview sheet. Read preview_path to judge
    it — it shows each size at 1x on Chrome's light and dark tab colours, with
    the small ones zoomed 8x.

    Either describe a subject and the image is generated for you, or pass
    image_path to iconify an existing image (a logo, say). Give one or the other.

    Styles:
      flat  (default) A bold, flat-colour icon, cut out with BiRefNet and
            downscaled smoothly. What most favicons look like.
      pixel A pixel-art sprite, snapped to a true grid and palette-reduced by
            fal-ai/image2pixel, then scaled with nearest-neighbour so pixels
            stay crisp. image2pixel cleans up pixel art; it cannot make it, so
            with image_path the input must already look like pixel art.
      glyph A single-colour silhouette for the macOS menu bar (or anywhere a
            monochrome icon is wanted). Generated black on white, then the
            darkness becomes the alpha, so light details become holes. The
            black PNGs work as macOS template images, which the system
            recolours for light and dark menu bars. No background-removal
            call, so with image_path no AWS is needed either.

    Args:
        subject:    What the icon shows, e.g. "BD-1, the droid from Star Wars
                    Jedi: Fallen Order". Wrapped in a style-specific prompt that
                    asks for bold shapes and no fine detail.
        image_path: An existing image to iconify instead of generating one.
        style:      "flat" (default), "pixel" or "glyph".
        colours:    Optional colour hint, e.g. "white body with blue markings".
        model:      Generator for the source image: "seedream" (default),
                    "gpt-image" or "flux". Ignored with image_path.
        sizes:      PNG sizes to write (default 16, 32, 48, 180, 192, 512; for
                    glyph 16, 18, 22, 32, 36, 44).
        margin:     Transparent padding on each side, as a fraction of the
                    icon's longest side (default 0.06).
        reference_paths: Images of the subject (up to 4) to draw it from. Use
                    these for anything the model may not know by sight: asked
                    for "BD-1" by name, Seedream drew a generic droid every
                    time (2026-10-13). With references, the source comes from
                    the edit endpoint instead and needs AWS for staging.
        max_colors: pixel only: palette size (default 16).
        tints:      glyph only: extra coloured copies, name -> "#RRGGBB", each
                    written to its own subfolder. Apple's status colours are
                    {"warn": "#FF9F0A", "down": "#FF453A"}. Tinted copies are
                    not templates; mark only the black one as a template.
        save_dir:   Parent folder (default ~/Pictures/falai-mcp/icons). Each run
                    gets its own subfolder.
    """
    style = style.strip().lower()
    if style not in ICON_STYLES:
        raise ValueError(f"Unknown style {style!r}. Use one of: {', '.join(sorted(ICON_STYLES))}.")
    if reference_paths and image_path:
        raise ValueError("reference_paths is for generating; it does nothing with image_path.")
    if bool(subject.strip()) == bool(image_path.strip()):
        raise ValueError("Pass either subject (to generate) or image_path (to iconify), not both.")
    sizes = sorted(set(sizes or (DEFAULT_GLYPH_SIZES if style == "glyph" else DEFAULT_ICON_SIZES)))
    tints = tints or {}
    if tints and style != "glyph":
        raise ValueError("tints only applies to style='glyph'.")
    for colour in tints.values():
        _tint(Image.new("RGBA", (1, 1)), colour)  # validate before paying for anything
    if any(s <= 0 or s > 1024 for s in sizes):
        raise ValueError(f"Sizes must be 1-1024 (got {sizes}).")
    if not 0 <= margin < 0.5:
        raise ValueError(f"margin must be 0 to 0.5 (got {margin}).")

    label = subject or Path(image_path).stem
    slug = re.sub(r"[^\w\s-]", "", label)[:40].strip().replace(" ", "_") or "icon"
    root = Path(save_dir).expanduser() if save_dir else DEFAULT_SAVE_DIR / "icons"
    out_dir = root / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{style}_{slug}"
    out_dir.mkdir(parents=True, exist_ok=True)

    def clean_up(source_url: str) -> dict:
        cut = _call(BG_REMOVE_ENDPOINT, {
            "image_url": source_url,
            "model": "General Use (Heavy)",
            "output_format": "png",
        })
        if style == "flat":
            return cut
        if not cut.get("image"):
            raise RuntimeError(f"Background removal returned no image: {str(cut)[:300]}")
        return _call(PIXELATE_ENDPOINT, {
            "image_url": cut["image"]["url"],
            "max_colors": max_colors,
            "cleanup_jaggy": True,
            "trim_borders": True,
        })

    source_path = out_dir / "source.png"
    if image_path:
        source_path.write_bytes(_read_image(image_path, 0)[0])
    else:
        hint = f", mainly {colours.strip()}" if colours.strip() else ""
        prompt = ICON_PROMPTS[style].format(subject=subject.strip(), colours=hint)
        payload = {"prompt": prompt, "image_size": "square_hd", "output_format": "png"}
        if reference_paths:
            if len(reference_paths) > MAX_INPUT_IMAGES:
                raise ValueError(f"At most {MAX_INPUT_IMAGES} reference images.")
            payload["prompt"] = (
                "Redraw the subject shown in the reference image, keeping its "
                "distinctive shape, proportions and features, but in this style: "
                + prompt
            )
            with _staged_urls(reference_paths, max_dimension=2048) as urls:
                payload["image_urls"] = urls
                generated = _call(_endpoint(EDIT_MODELS, model, DEFAULT_MODEL), payload)
        else:
            generated = _call(_endpoint(GENERATE_MODELS, model, DEFAULT_MODEL), payload)
        images = generated.get("images") or []
        if not images:
            raise RuntimeError("fal.ai returned no images.")
        source_url = images[0]["url"]
        source_path.write_bytes(_download(source_url))

    cutout_path = out_dir / "cutout.png"
    cleaned: dict = {}
    if style == "glyph":
        # Done locally: the source is already black on white.
        with Image.open(source_path) as src:
            _glyph_from_luminance(src).save(cutout_path)
    else:
        if image_path:
            with _staged_urls([image_path], max_dimension=2048) as urls:
                cleaned = clean_up(urls[0])
        else:
            # fal's own URL feeds straight into the clean-up step; no S3 needed.
            cleaned = clean_up(source_url)
        if style == "flat":
            out_file = cleaned.get("image")
        else:
            out_file = min(cleaned.get("images") or [None], key=lambda f: (f or {}).get("width") or 0)
        if not out_file:
            raise RuntimeError(f"fal.ai returned no image from the clean-up step: {str(cleaned)[:300]}")
        cutout_path.write_bytes(_download(out_file["url"]))

    warnings: list[str] = []
    native_size = None
    with Image.open(cutout_path) as cutout:
        cutout.load()
        if style == "pixel":
            native_size = max(_square_on_alpha(cutout, margin)[0].size)
            warnings += _pixel_size_warnings(native_size, sizes)
        resample = Image.NEAREST if style == "pixel" else Image.LANCZOS
        is_glyph = style == "glyph"
        icon_set = _export_icon_set(cutout, out_dir, sizes, resample, margin, is_glyph, is_glyph)
        tint_dirs = {}
        for name, colour in tints.items():
            tint_dirs[name] = str(out_dir / name)
            # Tinted copies are not templates: macOS shows their colour as-is.
            _export_icon_set(_tint(cutout, colour), out_dir / name, sizes, resample, margin, True)
    warnings += icon_set.warnings
    warnings += _balance_warning()

    return IconResult(
        dir=str(out_dir),
        preview_path=str(icon_set.preview),
        ico_path=str(icon_set.ico),
        files={s: str(p) for s, p in icon_set.files.items()},
        source_path=str(source_path),
        cutout_path=str(cutout_path),
        style=style,
        native_size=native_size,
        palette=cleaned.get("palette") or [],
        tints=tint_dirs,
        warnings=warnings,
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
