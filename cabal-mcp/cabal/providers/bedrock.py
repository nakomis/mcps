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

from .. import tracelog
from .base import Reply, error_reply, ok_reply, resolve_max_tokens

# Map our canonical short IDs to Bedrock's model IDs.
MODELS = {
    # eu-west-2 only has the Feb-2024 release; us-east-1 has -2407.
    # Update this if you move regions and want the newer release.
    "mistral-large": "mistral.mistral-large-2402-v1:0",
    "llama3-70b":    "meta.llama3-70b-instruct-v1:0",
    "nova-pro":      "amazon.nova-pro-v1:0",
    # Claude Fable 5 — model agreement accepted 2026-07-02, but InvokeModel
    # still returns AccessDenied ("not available for this account ... contact
    # AWS Sales") in eu-west-2 and us-east-1: account-level gating beyond the
    # normal access grant. Kept here for when AWS unlocks it; use
    # anthropic:fable-5 (direct API) meanwhile. No eu.* inference profile
    # exists yet, so this uses the global profile.
    "fable-5":       "global.anthropic.claude-fable-5",
}

# Hard maximum output tokens per model, probed against eu-west-2 on
# 2026-10-17 (anything above is a ValidationException). Llama 3 70B's schema
# allows 8192 but the service rejects anything over 4096. Fable 5 thinks on
# every request and its hidden reasoning counts against the limit.
MAX_OUTPUT_TOKENS = {
    "mistral-large": 8_192,
    "llama3-70b":    4_096,
    "nova-pro":      10_240,
    "fable-5":       128_000,
}

# Long answers (large max_tokens, thinking models) easily outlast botocore's
# default 60 s read timeout.
READ_TIMEOUT_S = 900


def _build_request(
    model_short: str, prompt: str, system: str | None, max_tokens: int,
) -> dict:
    """Bedrock's body format varies by model family. Return the right one."""
    if model_short == "mistral-large":
        # Mistral uses an OpenAI-ish chat-completions body.
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return {"messages": messages, "max_tokens": max_tokens, "temperature": 0.7}
    if model_short == "llama3-70b":
        sys_block = f"<|start_header_id|>system<|end_header_id|>\n{system}<|eot_id|>" if system else ""
        return {
            "prompt": (
                f"<|begin_of_text|>{sys_block}"
                f"<|start_header_id|>user<|end_header_id|>\n{prompt}<|eot_id|>"
                f"<|start_header_id|>assistant<|end_header_id|>\n"
            ),
            "max_gen_len": max_tokens,
            "temperature": 0.7,
        }
    if model_short == "nova-pro":
        body = {
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0.7},
        }
        if system:
            body["system"] = [{"text": system}]
        return body
    if model_short == "fable-5":
        # Anthropic messages body. Fable 5 has thinking always on — sending a
        # `thinking` param or `temperature` gets a 400, so send neither.
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        return body
    raise ValueError(f"unknown Bedrock model: {model_short}")


def _parse_response(model_short: str, body: dict) -> tuple[str, int, int]:
    """Return (text, input_tokens, output_tokens)."""
    if model_short == "mistral-large":
        text = body["choices"][0]["message"]["content"]
        usage = body.get("usage") or {}
        return text, usage.get("prompt_tokens"), usage.get("completion_tokens")
    if model_short == "llama3-70b":
        text = body.get("generation", "")
        return text, body.get("prompt_token_count"), body.get("generation_token_count")
    if model_short == "nova-pro":
        text = body["output"]["message"]["content"][0]["text"]
        usage = body.get("usage") or {}
        return text, usage.get("inputTokens"), usage.get("outputTokens")
    if model_short == "fable-5":
        if body.get("stop_reason") == "refusal":
            # Safety-classifier decline. Bedrock has no server-side fallback
            # for this; surface it as an error so the caller can rephrase or
            # route to another seat.
            raise RuntimeError(
                "Fable 5 declined the request (stop_reason=refusal); "
                "rephrase or use another model"
            )
        text = "".join(
            block.get("text", "") for block in body.get("content", [])
            if block.get("type") == "text"
        )
        usage = body.get("usage") or {}
        return text, usage.get("input_tokens"), usage.get("output_tokens")
    raise ValueError(f"unknown Bedrock model: {model_short}")


def _invoke_sync(
    model_short: str, prompt: str, system: str | None, region: str | None,
    max_tokens: int,
):
    import boto3
    from botocore.config import Config

    config = Config(read_timeout=READ_TIMEOUT_S)
    client = boto3.client("bedrock-runtime", region_name=region, config=config) if region \
        else boto3.client("bedrock-runtime", config=config)
    body = _build_request(model_short, prompt, system, max_tokens)
    resp = client.invoke_model(
        modelId=MODELS[model_short],
        body=json.dumps(body),
        contentType="application/json",
        accept="application/json",
    )
    headers = (resp.get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    return json.loads(resp["body"].read()), headers


def _header_count(headers: dict, name: str) -> int | None:
    try:
        return int(headers[name])
    except (KeyError, TypeError, ValueError):
        return None


async def ask(
    prompt: str,
    *,
    model: str,
    system: str | None = None,
    region: str | None = None,
    max_tokens: int | None = None,
) -> Reply:
    full_id = f"bedrock:{model}"
    if model not in MODELS:
        return error_reply(
            full_id, f"unknown bedrock model '{model}'; known: {sorted(MODELS)}",
        )
    t0 = time.monotonic()
    notes: list[str] = []
    try:
        mt, note = resolve_max_tokens(max_tokens, MAX_OUTPUT_TOKENS.get(model), full_id)
        if note:
            notes.append(note)
        parsed, headers = await anyio.to_thread.run_sync(
            _invoke_sync, model, prompt, system, region, mt,
        )
        text, in_tok, out_tok = _parse_response(model, parsed)
        # Mistral's body carries no usage; Bedrock reports counts for every
        # model in response headers, so fall back to those.
        if in_tok is None:
            in_tok = _header_count(headers, "x-amzn-bedrock-input-token-count")
        if out_tok is None:
            out_tok = _header_count(headers, "x-amzn-bedrock-output-token-count")
        return ok_reply(full_id, text, in_tok, out_tok, t0, notes)
    except Exception as e:
        tracelog.current().exception("bedrock.ask.failed", e, model=model)
        return error_reply(full_id, f"{type(e).__name__}: {e}", t0, notes)
