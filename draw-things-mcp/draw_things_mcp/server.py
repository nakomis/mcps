#!/usr/bin/env python3
"""Draw Things MCP Server — local AI image generation via the Draw Things HTTP API."""

import base64
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("draw-things-mcp")

BASE_URL = "http://localhost:7860"
DEFAULT_SAVE_DIR = Path.home() / "Pictures" / "draw-things-mcp"

_OFFLINE_MSG = (
    "Draw Things is not running. "
    "Please ask the user to open Draw Things and enable the API server "
    "(Settings → API Server), then retry."
)

# job_id → {"status": "pending"|"done"|"error", "path": str|None, "error": str|None, "prompt": str}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def _post(endpoint: str, payload: dict) -> dict:
    with httpx.Client(base_url=BASE_URL, timeout=None) as client:
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


def _current_dt_prompt() -> str | None:
    """Poll / to see what Draw Things is currently configured with."""
    try:
        with httpx.Client(base_url=BASE_URL, timeout=5) as client:
            r = client.get("/")
            r.raise_for_status()
            return r.json().get("prompt")
    except Exception:
        return None


def _run_generation(job_id: str, endpoint: str, payload: dict, prompt: str) -> None:
    try:
        result = _post(endpoint, payload)
        images = result.get("images", [])
        if not images:
            with _jobs_lock:
                _jobs[job_id] = {"status": "error", "path": None, "error": "Draw Things returned no images.", "prompt": prompt}
            return
        save_path = _save_image(base64.b64decode(images[0]), prompt)
        with _jobs_lock:
            _jobs[job_id] = {"status": "done", "path": str(save_path), "error": None, "prompt": prompt}
    except httpx.ConnectError:
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "path": None, "error": _OFFLINE_MSG, "prompt": prompt}
    except Exception as e:
        with _jobs_lock:
            _jobs[job_id] = {"status": "error", "path": None, "error": str(e), "prompt": prompt}


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
        return _OFFLINE_MSG
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
    Start image generation from a text prompt using Draw Things (non-blocking).

    Returns a job ID immediately. Use check_job(job_id) to poll for completion.
    The finished image is saved to ~/Pictures/draw-things-mcp/.

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

    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "path": None, "error": None, "prompt": prompt}

    t = threading.Thread(target=_run_generation, args=(job_id, "/sdapi/v1/txt2img", payload, prompt), daemon=True)
    t.start()

    return f"Job {job_id} started. Call check_job('{job_id}') to poll for completion."


@mcp.tool()
def check_job(job_id: str) -> str:
    """
    Check the status of an image generation job started by generate_image.

    Returns the file path when done, or a status message if still pending.

    Args:
        job_id: The job ID returned by generate_image.
    """
    with _jobs_lock:
        job = _jobs.get(job_id)

    if job is None:
        return f"Unknown job ID: {job_id}"

    if job["status"] == "done":
        return job["path"]

    if job["status"] == "error":
        return f"Generation failed: {job['error']}"

    # pending — check what Draw Things is currently running
    active_prompt = _current_dt_prompt()
    if active_prompt and active_prompt == job["prompt"]:
        return f"Job {job_id} is generating (Draw Things active on matching prompt)."
    return f"Job {job_id} is pending."


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
    Transform an existing image guided by a text prompt (non-blocking).

    Returns a job ID immediately. Use check_job(job_id) to poll for completion.

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

    job_id = uuid.uuid4().hex[:8]
    with _jobs_lock:
        _jobs[job_id] = {"status": "pending", "path": None, "error": None, "prompt": prompt}

    t = threading.Thread(target=_run_generation, args=(job_id, "/sdapi/v1/img2img", payload, prompt), daemon=True)
    t.start()

    return f"Job {job_id} started. Call check_job('{job_id}') to poll for completion."


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
