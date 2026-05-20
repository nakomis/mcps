"""AWS Bedrock provider — uses the standard AWS credential chain (env vars,
~/.aws/credentials, IAM role, etc.) — no Cabal-specific secrets needed.

Region is read from `AWS_REGION` / `AWS_DEFAULT_REGION`, defaulting to the
region you've already configured for Sinter (typically `us-east-1` or
`eu-west-2`). Override per-call via the `region` parameter if needed.
"""

from __future__ import annotations

import json
import time

import anyio

from ..pricing import cost
from .base import Reply

# Map our canonical short IDs to Bedrock's model IDs.
MODELS = {
    # eu-west-2 only has the Feb-2024 release; us-east-1 has -2407.
    # Update this if you move regions and want the newer release.
    "mistral-large": "mistral.mistral-large-2402-v1:0",
    "llama3-70b":    "meta.llama3-70b-instruct-v1:0",
    "nova-pro":      "amazon.nova-pro-v1:0",
}


def _build_request(model_short: str, prompt: str, system: str | None) -> dict:
    """Bedrock's body format varies by model family. Return the right one."""
    if model_short == "mistral-large":
        # Mistral uses an OpenAI-ish chat-completions body.
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return {"messages": messages, "max_tokens": 4096, "temperature": 0.7}
    if model_short == "llama3-70b":
        sys_block = f"<|start_header_id|>system<|end_header_id|>\n{system}<|eot_id|>" if system else ""
        return {
            "prompt": (
                f"<|begin_of_text|>{sys_block}"
                f"<|start_header_id|>user<|end_header_id|>\n{prompt}<|eot_id|>"
                f"<|start_header_id|>assistant<|end_header_id|>\n"
            ),
            "max_gen_len": 4096,
            "temperature": 0.7,
        }
    if model_short == "nova-pro":
        body = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": 4096, "temperature": 0.7},
        }
        if system:
            body["system"] = [{"text": system}]
        return body
    raise ValueError(f"unknown Bedrock model: {model_short}")


def _parse_response(model_short: str, body: dict) -> tuple[str, int, int]:
    """Return (text, input_tokens, output_tokens)."""
    if model_short == "mistral-large":
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
        return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
    if model_short == "llama3-70b":
        text = body.get("generation", "")
        return text, body.get("prompt_token_count", 0), body.get("generation_token_count", 0)
    if model_short == "nova-pro":
        text = body["output"]["message"]["content"][0]["text"]
        usage = body.get("usage", {})
        return text, usage.get("inputTokens", 0), usage.get("outputTokens", 0)
    raise ValueError(f"unknown Bedrock model: {model_short}")


def _invoke_sync(model_short: str, prompt: str, system: str | None, region: str | None):
    import boto3
    client = boto3.client("bedrock-runtime", region_name=region) if region \
        else boto3.client("bedrock-runtime")
    body = _build_request(model_short, prompt, system)
    resp = client.invoke_model(
        modelId=MODELS[model_short],
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    return json.loads(resp["body"].read())


async def ask(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    region: str | None = None,
) -> Reply:
    full_id = f"bedrock:{model}"
    if model not in MODELS:
        return Reply(
            provider=full_id, response="", input_tokens=0, output_tokens=0,
            cost_usd=None, latency_ms=0,
            error=f"unknown bedrock model '{model}'; known: {sorted(MODELS)}",
        )
    t0 = time.monotonic()
    try:
        parsed = await anyio.to_thread.run_sync(
            _invoke_sync, model, prompt, system, region,
        )
        text, in_tok, out_tok = _parse_response(model, parsed)
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
