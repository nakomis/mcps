"""Anthropic (Claude) provider — direct Anthropic API.

Cabal originally excluded Claude on the grounds that the orchestrating agent
is itself Claude. Claude is included here as an explicit, opt-in choice: an
independent Claude *review* is a different role from Claude *orchestrating*,
and Opus is genuinely best-in-field for editorial and technical critique.

Direct Anthropic API. (Bedrock Anthropic access was granted 2026-07-02 —
`bedrock:fable-5` is the Bedrock-side Claude seat; this provider keeps the
direct-API seat for the Opus tier.)

Secrets:
  - `anthropic-api-key`  from https://console.anthropic.com/settings/keys
"""

from __future__ import annotations

import time

import anyio

from .. import secrets, tracelog
from .base import Reply, error_reply, ok_reply, resolve_max_tokens

MODELS = {
    # Short ID → Anthropic API model name. Verify against
    # https://docs.anthropic.com/en/docs/about-claude/models before quoting.
    "opus-4.7": "claude-opus-4-7",
    "opus-4-8": "claude-opus-4-8",
    "fable-5":  "claude-fable-5",
}

# Hard maximum output tokens per model (Models API `max_tokens`, checked
# 2026-10-17). Fable 5 thinks on every request and its hidden reasoning
# counts against this, so it needs a far higher max_tokens than 4096.
MAX_OUTPUT_TOKENS = {
    "opus-4.7": 128_000,
    "opus-4-8": 128_000,
    "fable-5":  128_000,
}


def _invoke_sync(model: str, prompt: str, system: str | None, max_tokens: int):
    import anthropic

    api_key = secrets.get("anthropic-api-key").value
    client = anthropic.Anthropic(api_key=api_key)

    # Note: `temperature` is deliberately omitted — recent Opus models reject
    # it as deprecated. The API default is used.
    kwargs: dict = {
        "model": MODELS[model],
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    # Streamed, because the SDK refuses non-streaming requests whose
    # max_tokens implies a run longer than its 10-minute timeout.
    with client.messages.stream(**kwargs) as stream:
        msg = stream.get_final_message()
    if getattr(msg, "stop_reason", None) == "refusal":
        # Fable-family safety classifiers can decline with HTTP 200 and an
        # empty content array. Surface it rather than returning "".
        raise RuntimeError(
            f"{MODELS[model]} declined the request (stop_reason=refusal); "
            "rephrase or use another model"
        )
    text = "".join(
        block.text for block in msg.content
        if getattr(block, "type", None) == "text"
    )
    usage = getattr(msg, "usage", None)
    in_tok = getattr(usage, "input_tokens", None)
    out_tok = getattr(usage, "output_tokens", None)
    return text, in_tok, out_tok


async def ask(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    max_tokens: int | None = None,
) -> Reply:
    full_id = f"anthropic:{model}"
    if model not in MODELS:
        return error_reply(
            full_id, f"unknown anthropic model '{model}'; known: {sorted(MODELS)}",
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
        tracelog.current().exception("anthropic.ask.failed", e, model=model)
        return error_reply(full_id, f"{type(e).__name__}: {e}", t0, notes)
