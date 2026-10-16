"""Common provider interface, result type and shared helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from ..pricing import PRICING, cost

# The answer limit every provider used before `max_tokens` was configurable.
# Used whenever the caller passes `max_tokens=None`.
DEFAULT_MAX_TOKENS = 4096


@dataclass
class Reply:
    provider: str              # e.g. "bedrock:mistral-large"
    response: str              # the model's text response
    input_tokens: int | None   # None if the provider reported no count
    output_tokens: int | None  # None if the provider reported no count
    cost_usd: float | None     # None if pricing or token counts are unknown
    latency_ms: int
    error: str | None = None   # set on failure; response will be empty
    notes: list[str] = field(default_factory=list)  # e.g. max_tokens clamped, cost unknown


class Provider(Protocol):
    """Each provider module exposes an `ask` function with this shape."""

    async def ask(
        self,
        prompt: str,
        *,
        model: str,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> Reply:
        ...


def resolve_max_tokens(
    requested: int | None, cap: int | None, full_id: str,
) -> tuple[int, str | None]:
    """Return (max_tokens to send, note or None).

    `requested=None` means the historical default. A request above the
    model's hard maximum (`cap`, None if unknown) is clamped to it, with a
    note, rather than letting the API reject the call.
    """
    if requested is None:
        value = DEFAULT_MAX_TOKENS
        if cap is not None and value > cap:
            value = cap
        return value, None
    if isinstance(requested, bool) or not isinstance(requested, int) or requested < 1:
        raise ValueError(f"max_tokens must be a positive integer, got {requested!r}")
    if cap is not None and requested > cap:
        return cap, (
            f"max_tokens {requested} clamped to {cap}, the maximum output "
            f"for {full_id}"
        )
    return requested, None


def error_reply(full_id: str, error: str, t0: float | None = None, notes=None) -> Reply:
    latency = int((time.monotonic() - t0) * 1000) if t0 is not None else 0
    return Reply(
        provider=full_id, response="", input_tokens=None, output_tokens=None,
        cost_usd=None, latency_ms=latency, error=error, notes=list(notes or []),
    )


def ok_reply(
    full_id: str,
    text: str | None,
    in_tok: int | None,
    out_tok: int | None,
    t0: float,
    notes: list[str] | None = None,
) -> Reply:
    """Build a successful Reply, computing cost and noting why it's unknown."""
    notes = list(notes or [])
    c = cost(full_id, in_tok, out_tok)
    if c is None:
        if in_tok is None or out_tok is None:
            notes.append("provider returned no token count; cost unknown")
        elif full_id not in PRICING:
            notes.append("no pricing entry for this model; cost unknown")
    return Reply(
        provider=full_id,
        response=text or "",
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost_usd=c,
        latency_ms=int((time.monotonic() - t0) * 1000),
        notes=notes,
    )
