"""Anthropic (Claude) provider — direct Anthropic API.

Cabal originally excluded Claude on the grounds that the orchestrating agent
is itself Claude. Claude is included here as an explicit, opt-in choice: an
independent Claude *review* is a different role from Claude *orchestrating*,
and Opus is genuinely best-in-field for editorial and technical critique.

Direct Anthropic API rather than Bedrock — Bedrock model access was declined,
and the direct API has no approval gate.

Secrets:
  - `anthropic-api-key`  from https://console.anthropic.com/settings/keys
"""

from __future__ import annotations

import time

import anyio

from .. import secrets, tracelog
from ..pricing import cost
from .base import Reply

MODELS = {
    # Short ID → Anthropic API model name. Verify against
    # https://docs.anthropic.com/en/docs/about-claude/models before quoting.
    "opus-4.7": "claude-opus-4-7",
}


def _invoke_sync(model: str, prompt: str, system: str | None):
    import anthropic

    api_key = secrets.get("anthropic-api-key").value
    client = anthropic.Anthropic(api_key=api_key)

    kwargs: dict = {
        "model": MODELS[model],
        "max_tokens": 4096,
        "temperature": 0.7,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    msg = client.messages.create(**kwargs)
    text = "".join(
        block.text for block in msg.content
        if getattr(block, "type", None) == "text"
    )
    in_tok = msg.usage.input_tokens
    out_tok = msg.usage.output_tokens
    return text, in_tok, out_tok


async def ask(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
) -> Reply:
    full_id = f"anthropic:{model}"
    if model not in MODELS:
        return Reply(
            provider=full_id, response="", input_tokens=0, output_tokens=0,
            cost_usd=None, latency_ms=0,
            error=f"unknown anthropic model '{model}'; known: {sorted(MODELS)}",
        )
    t0 = time.monotonic()
    try:
        text, in_tok, out_tok = await anyio.to_thread.run_sync(
            _invoke_sync, model, prompt, system,
        )
    except Exception as e:
        tracelog.current().exception("anthropic.ask.failed", e, model=model)
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
