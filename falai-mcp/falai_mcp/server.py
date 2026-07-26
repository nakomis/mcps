#!/usr/bin/env python3
"""fal.ai MCP server — hosted image generation and editing via FLUX.2 [pro].

Replaces the retired draw-things-mcp. Everything runs on fal.ai, so nothing
needs to be installed or running locally.

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

mcp = FastMCP("falai-mcp")

GENERATE_URL = "https://fal.run/fal-ai/flux-2-pro"
EDIT_URL = "https://fal.run/fal-ai/flux-2-pro/edit"
REMOVE_URL = "https://fal.run/fal-ai/object-removal"

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


def _call(url: str, payload: dict) -> dict:
    headers = {"Authorization": f"Key {_api_key()}", "Content-Type": "application/json"}
    with httpx.Client(timeout=_TIMEOUT) as client:
        r = client.post(url, json=payload, headers=headers)
        if r.status_code == 401:
            raise RuntimeError("fal.ai rejected the API key (401). Check the keychain entry.")
        if r.status_code >= 400:
            raise RuntimeError(f"fal.ai returned {r.status_code}: {r.text[:500]}")
        return r.json()


def _fetch_result(result: dict, prompt: str, save_dir: str, fmt: str) -> str:
    images = result.get("images") or []
    if not images:
        return "fal.ai returned no images."
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        data = client.get(images[0]["url"]).content
    path = _save(data, prompt, save_dir, fmt)
    meta = images[0]
    return (
        f"{path}\n"
        f"({meta.get('width')}x{meta.get('height')}, {len(data) // 1024} KB, "
        f"seed {result.get('seed')})"
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
) -> str:
    """
    Generate an image from a text prompt using FLUX.2 [pro] on fal.ai.

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
        seed:          Seed for reproducibility (-1 = random).
        save_dir:      Directory to save into (default ~/Pictures/falai-mcp).
    """
    payload = {
        "prompt": prompt,
        "image_size": _size_param(width, height, aspect, "landscape_4_3"),
        "output_format": output_format,
    }
    if seed >= 0:
        payload["seed"] = seed

    return _fetch_result(_call(GENERATE_URL, payload), prompt, save_dir, output_format)


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
) -> str:
    """
    Edit an existing image with a natural-language instruction, using
    FLUX.2 [pro] edit on fal.ai.

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
        seed:          Seed for reproducibility (-1 = random).
        save_dir:      Directory to save into (default: alongside the input).
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

    with _staged_urls(image_paths, max_dimension) as urls:
        payload = {
            "prompt": prompt,
            "image_urls": urls,
            # "auto" preserves the input's dimensions unless asked otherwise.
            "image_size": _size_param(width, height, aspect, "auto"),
            "output_format": output_format,
        }
        if seed >= 0:
            payload["seed"] = seed
        result = _call(EDIT_URL, payload)

    return _fetch_result(result, prompt, target, output_format)


@mcp.tool()
def remove_object(
    image_path: str,
    object_description: str,
    mask_expansion: int = 15,
    quality: str = "best_quality",
    max_dimension: int = 0,
    output_format: str = "png",
    save_dir: str = "",
) -> str:
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
    """
    if not 0 <= mask_expansion <= 50:
        raise ValueError(f"mask_expansion must be 0-50 (got {mask_expansion}).")

    target = save_dir or str(Path(image_path).expanduser().parent)

    with _staged_urls([image_path], max_dimension) as urls:
        result = _call(REMOVE_URL, {
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
