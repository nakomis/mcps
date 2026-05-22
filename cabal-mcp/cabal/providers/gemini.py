"""Google AI Studio (Gemini) provider — uses the genuine free-tier API.

Secrets:
  - `gemini-api-key`   from https://aistudio.google.com/apikey
"""

from __future__ import annotations

import time

import anyio

from .. import secrets, tracelog
from ..pricing import cost
from .base import Reply

MODELS = {
    # Short ID → AI Studio model name. Verify against
    # https://aistudio.google.com/ before quoting capabilities.
    "gemini-3-pro":   "gemini-3-pro-preview",
    "gemini-2-flash": "gemini-2.0-flash",
}


def _invoke_sync(model: str, prompt: str, system: str | None):
    from google import genai
    from google.genai import types

    api_key = secrets.get("gemini-api-key").value
    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        max_output_tokens=4096,
        temperature=0.7,
    )
    if system:
        config.system_instruction = system

    resp = client.models.generate_content(
        model=MODELS[model],
        contents=prompt,
        config=config,
    )
    text = resp.text or ""
    usage = getattr(resp, "usage_metadata", None)
    in_tok = getattr(usage, "prompt_token_count", 0) if usage else 0
    out_tok = getattr(usage, "candidates_token_count", 0) if usage else 0
    return text, in_tok, out_tok


async def ask(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
) -> Reply:
    full_id = f"gemini:{model}"
    if model not in MODELS:
        return Reply(
            provider=full_id, response="", input_tokens=0, output_tokens=0,
            cost_usd=None, latency_ms=0,
            error=f"unknown gemini model '{model}'; known: {sorted(MODELS)}",
        )
    t0 = time.monotonic()
    try:
        text, in_tok, out_tok = await anyio.to_thread.run_sync(
            _invoke_sync, model, prompt, system,
        )
    except Exception as e:
        tracelog.current().exception("gemini.ask.failed", e, model=model)
        return Reply(
            provider=full_id, response="", input_tokens=0, output_tokens=0,
            cost_usd=None, latency_ms=int((time.monotonic() - t0) * 1000),
            error=f"{type(e).__name__}: {e}",
        )
    return Reply(
        provider=full_id,
        response=text,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=cost(full_id, in_tok, out_tok),
        latency_ms=int((time.monotonic() - t0) * 1000),
    )
