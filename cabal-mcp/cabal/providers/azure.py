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

from .. import secrets, tracelog
from .base import Reply, error_reply, ok_reply, resolve_max_tokens

# Canonical short IDs → Foundry deployment names. The deployment name on
# the right is set up in the Azure AI Foundry portal; if you've named
# yours differently, edit this map or override via the `deployment`
# argument.
MODELS = {
    # Short ID → Foundry deployment name. Edit the right-hand side if your
    # deployment names differ from these. `gpt-5.4-pro` still returns
    # DeploymentNotFound as of 2026-07-02 — the Azure OpenAI quota request /
    # deployment has not landed; it is excluded from the default cabal until
    # the deployment exists in the Foundry portal.
    "gpt-5.4-pro": "gpt-5.4-pro",
    "grok-4.3":    "grok-4.3",
}

# Hard maximum output tokens, where known. Foundry accepted max_tokens up to
# 1,000,000 for grok-4.3 without complaint (2026-10-17), so no clamp is
# applied; gpt-5.4-pro has no live deployment to check against.
MAX_OUTPUT_TOKENS: dict[str, int] = {}


def _credential_for(model: str) -> tuple[str, str]:
    """Return (endpoint, key). Per-model override if present, else the global pair."""
    ep = secrets.get_optional(f"azure-foundry-{model}-endpoint")
    k  = secrets.get_optional(f"azure-foundry-{model}-key")
    if ep and k:
        return ep, k
    return secrets.get("azure-foundry-endpoint").value, secrets.get("azure-foundry-key").value


def _invoke_sync(
    model: str, prompt: str, system: str | None, deployment: str | None,
    max_tokens: int,
):
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
        max_tokens=max_tokens,
        temperature=0.7,
    )
    text = resp.choices[0].message.content
    usage = resp.usage
    in_tok = getattr(usage, "prompt_tokens", None) if usage else None
    out_tok = getattr(usage, "completion_tokens", None) if usage else None
    return text, in_tok, out_tok


async def ask(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    deployment: str | None = None,
    max_tokens: int | None = None,
) -> Reply:
    full_id = f"azure:{model}"
    if model not in MODELS:
        return error_reply(
            full_id, f"unknown azure model '{model}'; known: {sorted(MODELS)}",
        )
    t0 = time.monotonic()
    notes: list[str] = []
    try:
        mt, note = resolve_max_tokens(max_tokens, MAX_OUTPUT_TOKENS.get(model), full_id)
        if note:
            notes.append(note)
        text, in_tok, out_tok = await anyio.to_thread.run_sync(
            _invoke_sync, model, prompt, system, deployment, mt,
        )
        return ok_reply(full_id, text, in_tok, out_tok, t0, notes)
    except Exception as e:
        tracelog.current().exception("azure.ask.failed", e, model=model)
        return error_reply(full_id, f"{type(e).__name__}: {e}", t0, notes)
