# cabal-mcp

> *"A small group of people involved in secret plans."* — what every good multi-LLM consultation aspires to be.

An MCP server that fans a single prompt out to several LLM providers in
parallel and compiles the responses, so you can cross-reference what
different models say about the same question. Built to support the
"trust, but verify" pattern documented in
[`docs/notes/multi-llm-consultation.md`](https://github.com/nakomis/sinter/blob/main/docs/notes/multi-llm-consultation.md)
in the Sinter repo.

## Providers

| Provider | Models | Auth |
|---|---|---|
| AWS Bedrock | `mistral-large`, `llama3-70b`, `nova-pro` | Standard AWS SDK credential chain (env vars / `~/.aws/credentials` / IAM role). Set `AWS_PROFILE` and `AWS_REGION` as usual. |
| Azure AI Foundry | `gpt-4o`, `grok` | Keychain: `azure-foundry-endpoint`, `azure-foundry-key`. Per-model override: `azure-foundry-<model>-endpoint` / `-key`. |
| Google AI Studio | `gemini-2-pro`, `gemini-2-flash` | Keychain: `gemini-api-key` (free-tier key from <https://aistudio.google.com/apikey>). |
| Anthropic | `opus-4.7` | Keychain: `anthropic-api-key` (from <https://console.anthropic.com/settings/keys>). Direct Anthropic API. |

**Claude is included as a deliberate, opt-in voice** — originally it was
left out, on the grounds that the orchestrating agent is itself Claude and
adding it again would mean paying twice. That still holds for the
*orchestrator* role. But an independent Claude *review* is a different job
from Claude *orchestrating*, and Opus is genuinely best-in-field for
editorial and technical critique — so it now sits in the cabal as a peer
provider. It uses the direct Anthropic API (Bedrock model access having
been declined, and the direct API has no approval gate).

**Copilot is not a provider** — Microsoft Copilot has no public API. It's
GPT-4o plus Microsoft's prompting layer; the closest programmatic
equivalent is `azure:gpt-4o`, optionally with a `system` prompt that
mimics Copilot's framing.

## Tools

| Tool | Purpose |
|---|---|
| `ask_all(prompt, providers=, system=, save_dir=, save_slug=)` | Fan out to multiple providers in parallel, return all responses + costs. |
| `ask_bedrock(prompt, model="mistral-large", system=)` | Single Bedrock call. |
| `ask_azure(prompt, model="gpt-4o", system=)` | Single Azure Foundry call. |
| `ask_gemini(prompt, model="gemini-3-pro", system=)` | Single Gemini call. |
| `ask_anthropic(prompt, model="opus-4.7", system=)` | Single Anthropic Claude call. |
| `list_providers()` | List configured provider:model specs. |
| `check_secrets()` | Report which secrets are configured (without revealing values). |

## Secrets — Keychain primary, SSM backup

Secrets are looked up in this order:

1. Environment variable `CABAL_<NAME_UPPER>` (e.g. `CABAL_GEMINI_API_KEY`).
2. macOS Keychain — service `cabal-mcp`, account `<name>`.
3. AWS SSM Parameter Store — `/cabal-mcp/<name>` (SecureString).

### One-shot setup

```python
from cabal import secrets
secrets.store("gemini-api-key", "AIza...")
secrets.store("azure-foundry-endpoint", "https://my-resource.services.ai.azure.com/models")
secrets.store("azure-foundry-key", "<...>")
secrets.store("anthropic-api-key", "sk-ant-...")
```

`secrets.store` writes to keychain **and** mirrors to SSM (so the same
machine or a fresh one can pick them up). Pass `ssm_too=False` if you want
keychain-only.

Manual keychain entry, if you prefer the CLI:

```sh
security add-generic-password -U -s cabal-mcp -a gemini-api-key -w 'AIza...'
```

## Install

```sh
cd ~/repos/nakomis/mcps/cabal-mcp
uv sync
```

## Wire into meta-mcp

Add to `~/repos/nakomis/mcps/meta-mcp/config.toml`:

```toml
[mcps.cabal]
command = "/Users/nakomis/repos/nakomis/mcps/cabal-mcp/cabal-mcp-launch.sh"
```

Then in Claude: `list_mcps()` should show `cabal`; `describe_mcp("cabal")`
will surface the tools above.

## Pricing

`cabal/pricing.py` carries USD-per-1M-token prices for cost estimation.
**These move; verify against the cloud's published pricing page if a
number matters.** Prices recorded as of repo creation in May 2026.

Quick refresh sources:

- Bedrock: <https://aws.amazon.com/bedrock/pricing/>
- Azure Foundry: <https://azure.microsoft.com/pricing/details/ai-studio/>
- Google AI Studio: <https://ai.google.dev/pricing>
- Anthropic: <https://www.anthropic.com/pricing>

## Cost discipline

Every consultation logs `input_tokens`, `output_tokens`, `cost_usd`, and
`latency_ms` per provider. The per-call summary file written to `save_dir`
includes a totals row. If "essentially free" stops being true, you'll see
it in the summary before you see it on the bill.
