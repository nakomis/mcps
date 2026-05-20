"""Cabal MCP server — convene a cabal of LLMs and compile their counsel.

Exposes:
  - ask_all(prompt, providers=None, system=None, save_dir=None, save_slug=None)
  - ask_bedrock(prompt, model="mistral-large", system=None)
  - ask_azure(prompt, model="gpt-4o", system=None)
  - ask_gemini(prompt, model="gemini-2-pro", system=None)
  - list_providers()
  - check_secrets()
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import secrets
from .fanout import ALL_PROVIDERS, DEFAULT_PROVIDERS, ask_all as _ask_all
from .providers import azure, bedrock, gemini

mcp = FastMCP("cabal")


@mcp.tool()
async def ask_all(
    prompt: str,
    providers: list[str] | None = None,
    system: str | None = None,
    save_dir: str | None = None,
    save_slug: str | None = None,
    bluntness: bool = True,
) -> dict:
    """Fan a prompt out across multiple LLM providers in parallel.

    Args:
        prompt: the question / preamble to send to every provider verbatim.
        providers: list of provider:model specs (e.g. ["bedrock:mistral-large",
            "azure:gpt-4o", "gemini:gemini-2-pro"]). Defaults to all configured.
        system: optional system prompt applied to every model that supports one.
        save_dir: if set, write per-provider Markdown files + a summary into
            this directory. Path is expanded (~ is honoured).
        save_slug: filename slug for saved files. Defaults to a slug derived
            from the prompt's first line.
        bluntness: if True (default), prepend a "be blunt / falsify, don't
            hedge / answer numbered questions in order" preamble to the system
            prompt. Set False when you actually want the agreeable default.

    Returns:
        Dict with `results` (one per provider), `total_cost_usd`, and
        `saved_files` (paths written, if save_dir was set).
    """
    return await _ask_all(
        prompt,
        providers=providers,
        system=system,
        save_dir=save_dir,
        save_slug=save_slug,
        bluntness=bluntness,
    )


@mcp.tool()
async def ask_bedrock(
    prompt: str,
    model: str = "mistral-large",
    system: str | None = None,
) -> dict:
    """Ask a single Bedrock-hosted model. See list_providers() for model names."""
    r = await bedrock.ask(prompt, model=model, system=system)
    return r.__dict__


@mcp.tool()
async def ask_azure(
    prompt: str,
    model: str = "gpt-4o",
    system: str | None = None,
) -> dict:
    """Ask a single Azure Foundry-hosted model (e.g. gpt-4o, grok)."""
    r = await azure.ask(prompt, model=model, system=system)
    return r.__dict__


@mcp.tool()
async def ask_gemini(
    prompt: str,
    model: str = "gemini-3-pro",
    system: str | None = None,
) -> dict:
    """Ask a single Google AI Studio model (gemini-3-pro or gemini-2-flash)."""
    r = await gemini.ask(prompt, model=model, system=system)
    return r.__dict__


@mcp.tool()
def list_providers() -> dict:
    """List all providers the MCP can talk to, with their canonical IDs."""
    return {
        "default_cabal": DEFAULT_PROVIDERS,
        "all": ALL_PROVIDERS,
        "by_provider": {
            "bedrock": [f"bedrock:{m}" for m in bedrock.MODELS],
            "azure":   [f"azure:{m}"   for m in azure.MODELS],
            "gemini":  [f"gemini:{m}"  for m in gemini.MODELS],
        },
    }


@mcp.tool()
def check_secrets() -> dict:
    """Report which secrets are configured (without revealing values).

    Bedrock uses the AWS SDK credential chain — not reported here. Set
    AWS_PROFILE / AWS_REGION as you would for any other AWS tool.
    """
    needed = [
        "azure-foundry-endpoint",
        "azure-foundry-key",
        "gemini-api-key",
    ]
    found = {}
    for name in needed:
        try:
            lookup = secrets.get(name)
            found[name] = {"present": True, "source": lookup.source}
        except secrets.SecretNotFoundError:
            found[name] = {"present": False, "source": None}
    return {
        "secrets": found,
        "note": "Bedrock uses the AWS SDK credential chain — set AWS_PROFILE / AWS_REGION as usual.",
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
