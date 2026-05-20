"""Common provider interface and result type."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Reply:
    provider: str           # e.g. "bedrock:mistral-large"
    response: str           # the model's text response
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None if pricing not known for this model
    latency_ms: int
    error: str | None = None  # set on failure; response will be empty


class Provider(Protocol):
    """Each provider module exposes an `ask` function with this shape."""

    async def ask(self, prompt: str, *, model: str, system: str | None = None) -> Reply:
        ...
