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
from PIL import Image
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

DEFAULT_MODEL = "seedream"
DEFAULT_REMOVE_MODEL = "object-removal"

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
def _staged_urls(image_paths: list[str], max_dimension: int = 0):
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
                        ExpiresIn=URL_TTL_SECONDS,
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
            "image_size": _size_param(width, height, aspect, "auto"),
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


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
