"""Azure AI Foundry provider — unified endpoint for GPT-4o and Grok.

Secrets (looked up via `cabal.secrets`):
  - `azure-foundry-endpoint`   e.g. https://<resource>.services.ai.azure.com/models
  - `azure-foundry-key`        the inference API key
  - optional `azure-foundry-<model>-endpoint` / `-key` to override per model

Foundry's inference SDK presents an OpenAI-compatible chat-completions
interface for every model in the catalogue, so the code path is the same
for GPT-4o and Grok.
"""

from __future__ import annotations

import time

import anyio

from .. import secrets
from ..pricing import cost
from .base import Reply

# Canonical short IDs → Foundry deployment names. The deployment name on
# the right is set up in the Azure AI Foundry portal; if you've named
# yours differently, edit this map or override via the `deployment`
# argument.
MODELS = {
    "gpt-4o": "gpt-4o",
    "grok":   "grok-4",
}


def _credential_for(model: str) -> tuple[str, str]:
    """Return (endpoint, key). Per-model override if present, else the global pair."""
    ep = secrets.get_optional(f"azure-foundry-{model}-endpoint")
    k  = secrets.get_optional(f"azure-foundry-{model}-key")
    if ep and k:
        return ep, k
    return secrets.get("azure-foundry-endpoint").value, secrets.get("azure-foundry-key").value


def _invoke_sync(model: str, prompt: str, system: str | None, deployment: str | None):
    from azure.ai.inference import ChatCompletionsClient
    from azure.ai.inference.models import SystemMessage, UserMessage
    from azure.core.credentials import AzureKeyCredential

    endpoint, key = _credential_for(model)
    client = ChatCompletionsClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key),
    )
    messages = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(UserMessage(content=prompt))
    deployment_name = deployment or MODELS[model]
    resp = client.complete(
        messages=messages,
        model=deployment_name,
        max_tokens=4096,
        temperature=0.7,
    )
    text = resp.choices[0].message.content
    usage = resp.usage
    in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
    out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
    return text, in_tok, out_tok


async def ask(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    deployment: str | None = None,
) -> Reply:
    full_id = f"azure:{model}"
    if model not in MODELS:
        return Reply(
            provider=full_id, response="", input_tokens=0, output_tokens=0,
            cost_usd=None, latency_ms=0,
            error=f"unknown azure model '{model}'; known: {sorted(MODELS)}",
        )
    t0 = time.monotonic()
    try:
        text, in_tok, out_tok = await anyio.to_thread.run_sync(
            _invoke_sync, model, prompt, system, deployment,
        )
    except Exception as e:
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
