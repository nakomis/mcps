"""Token pricing for cost estimation.

Prices in USD per 1M tokens. **These move; treat as best-recent-recall and
re-verify against the cloud's published pricing page if a number matters.**
The structured format makes it easy to bulk-update.

Keys are the canonical model IDs used inside this MCP (see providers/*.py
for the mapping from these IDs to the provider-specific API model names).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    input_per_1m: float
    output_per_1m: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens / 1_000_000) * self.input_per_1m \
             + (output_tokens / 1_000_000) * self.output_per_1m


# Last-checked: see README "Pricing" section for date and how to refresh.
# All figures USD per 1M tokens.
PRICING: dict[str, Price] = {
    # AWS Bedrock
    "bedrock:mistral-large":      Price(input_per_1m=2.00,  output_per_1m=6.00),
    "bedrock:llama3-70b":         Price(input_per_1m=0.72,  output_per_1m=0.72),
    "bedrock:nova-pro":           Price(input_per_1m=0.80,  output_per_1m=3.20),

    # Azure Foundry
    "azure:gpt-4o":               Price(input_per_1m=2.50,  output_per_1m=10.00),
    "azure:grok":                 Price(input_per_1m=5.00,  output_per_1m=15.00),

    # Google AI Studio
    "gemini:gemini-2-pro":        Price(input_per_1m=1.25,  output_per_1m=5.00),
    "gemini:gemini-2-flash":      Price(input_per_1m=0.075, output_per_1m=0.30),
}


def cost(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    p = PRICING.get(model_id)
    if p is None:
        return None
    return p.cost(input_tokens, output_tokens)
