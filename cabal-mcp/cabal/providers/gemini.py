"""Google AI Studio (Gemini) provider — uses the genuine free-tier API.

Secrets:
  - `gemini-api-key`   from https://aistudio.google.com/apikey
"""

from __future__ import annotations

import time

import anyio

from .. import secrets, tracelog
from .base import Reply, error_reply, ok_reply, resolve_max_tokens

MODELS = {
    # Short ID → AI Studio model name. Verify against the live ListModels
    # endpoint before changing (gemini-3-pro-preview and gemini-2.0-flash
    # were retired; replaced 2026-07-02).
    "gemini-3.1-pro":   "gemini-3.1-pro-preview",
    "gemini-3.5-flash": "gemini-3.5-flash",
}

# Maximum output tokens (ListModels `output_token_limit`, 2026-10-17). The
# API silently accepts larger values, but this is the documented limit.
# Thinking tokens count against it: Gemini Pro needs ~16–32k to leave room
# for a visible answer after its hidden reasoning.
MAX_OUTPUT_TOKENS = {
    "gemini-3.1-pro":   65_536,
    "gemini-3.5-flash": 65_536,
}


def _invoke_sync(model: str, prompt: str, system: str | None, max_tokens: int):
    from google import genai
    from google.genai import types

    api_key = secrets.get("gemini-api-key").value
    client = genai.Client(api_key=api_key)

    config = types.GenerateContentConfig(
        max_output_tokens=max_tokens,
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
    # Any of these can be None (e.g. when the whole budget went on thinking
    # and no candidate text was produced).
    in_tok = getattr(usage, "prompt_token_count", None) if usage else None
    out_tok = getattr(usage, "candidates_token_count", None) if usage else None
    thoughts = getattr(usage, "thoughts_token_count", None) if usage else None
    # Thinking tokens are billed as output but not included in
    # candidates_token_count, so add them in.
    if thoughts:
        out_tok = (out_tok or 0) + thoughts
    return text, in_tok, out_tok


async def ask(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    max_tokens: int | None = None,
) -> Reply:
    full_id = f"gemini:{model}"
    if model not in MODELS:
        return error_reply(
            full_id, f"unknown gemini model '{model}'; known: {sorted(MODELS)}",
        )
    t0 = time.monotonic()
    notes: list[str] = []
    try:
        mt, note = resolve_max_tokens(max_tokens, MAX_OUTPUT_TOKENS.get(model), full_id)
        if note:
            notes.append(note)
        text, in_tok, out_tok = await anyio.to_thread.run_sync(
            _invoke_sync, model, prompt, system, mt,
        )
        return ok_reply(full_id, text, in_tok, out_tok, t0, notes)
    except Exception as e:
        tracelog.current().exception("gemini.ask.failed", e, model=model)
        return error_reply(full_id, f"{type(e).__name__}: {e}", t0, notes)
