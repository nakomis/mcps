#!/usr/bin/env python3
"""Draw Things MCP Server — local AI image generation via the Draw Things HTTP API."""

import base64
import re
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("draw-things-mcp")

BASE_URL = "http://localhost:7860"
DEFAULT_SAVE_DIR = Path.home() / "Pictures" / "draw-things-mcp"


def _post(endpoint: str, payload: dict) -> dict:
    with httpx.Client(base_url=BASE_URL, timeout=300) as client:
        r = client.post(endpoint, json=payload)
        r.raise_for_status()
        return r.json()


def _save_image(image_bytes: bytes, prompt: str, save_dir: Path | None = None) -> Path:
    out_dir = save_dir or DEFAULT_SAVE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^\w\s-]", "", prompt)[:40].strip().replace(" ", "_")
    filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slug}.png"
    path = out_dir / filename
    path.write_bytes(image_bytes)
    return path


# ── Tools ─────────────────────────────────────────────────────────────────────

@mcp.tool()
def check_connection() -> str:
    """
    Check whether the Draw Things API server is running and reachable.
    Enable it in Draw Things → Settings → API Server.
    """
    try:
        with httpx.Client(base_url=BASE_URL, timeout=5) as client:
            r = client.get("/")
            r.raise_for_status()
            data = r.json()
            model = data.get("model", "unknown")
            return f"Draw Things is running. Current model: {model}"
    except httpx.ConnectError:
        return "Cannot reach Draw Things. Make sure the API server is enabled in Draw Things → Settings → API Server."
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def generate_image(
    prompt: str,
    negative_prompt: str = "",
    width: int = 512,
    height: int = 512,
    steps: int = 20,
    guidance_scale: float = 7.5,
    seed: int = -1,
) -> str:
    """
    Generate an image from a text prompt using Draw Things.

    The image is saved to ~/Pictures/draw-things-mcp/ and the full path is returned.
    Use the Read tool on the returned path to view it.

    Args:
        prompt:          What to generate.
        negative_prompt: What to avoid in the image.
        width:           Image width in pixels (default 512).
        height:          Image height in pixels (default 512).
        steps:           Number of diffusion steps (default 20; more = higher quality but slower).
        guidance_scale:  How closely to follow the prompt (default 7.5).
        seed:            Random seed for reproducibility (-1 = random).
    """
    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": guidance_scale,
        "seed": seed,
    }

    result = _post("/sdapi/v1/txt2img", payload)

    images = result.get("images", [])
    if not images:
        return "Draw Things returned no images."

    save_path = _save_image(base64.b64decode(images[0]), prompt)
    return str(save_path)


@mcp.tool()
def image_to_image(
    prompt: str,
    image_path: str,
    negative_prompt: str = "",
    denoising_strength: float = 0.75,
    steps: int = 20,
    guidance_scale: float = 7.5,
    seed: int = -1,
) -> str:
    """
    Transform an existing image guided by a text prompt.

    The result is saved to ~/Pictures/draw-things-mcp/ and the full path is returned.
    Use the Read tool on the returned path to view it.

    Args:
        prompt:              Description of the desired output.
        image_path:          Absolute path to the input image file.
        negative_prompt:     What to avoid.
        denoising_strength:  How much to change the image (0 = no change, 1 = ignore original).
        steps:               Diffusion steps (default 20).
        guidance_scale:      Prompt adherence (default 7.5).
        seed:                Random seed (-1 = random).
    """
    input_bytes = Path(image_path).read_bytes()
    input_b64 = base64.b64encode(input_bytes).decode()

    payload = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "init_images": [input_b64],
        "denoising_strength": denoising_strength,
        "steps": steps,
        "cfg_scale": guidance_scale,
        "seed": seed,
    }

    result = _post("/sdapi/v1/img2img", payload)

    images = result.get("images", [])
    if not images:
        return "Draw Things returned no images."

    save_path = _save_image(base64.b64decode(images[0]), prompt)
    return str(save_path)


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
