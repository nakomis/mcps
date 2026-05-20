"""Parallel fan-out across providers, with optional disk persistence."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import anyio

from .providers import azure, bedrock, gemini
from .providers.base import Reply

# The default cabal: one voice per provider, cheapest sensible model each.
DEFAULT_PROVIDERS = [
    "bedrock:mistral-large",
    "bedrock:llama3-70b",
    "bedrock:nova-pro",
    "azure:gpt-4o",
    "azure:grok",
    "gemini:gemini-2-pro",
]

ALL_PROVIDERS = DEFAULT_PROVIDERS  # alias; grows when more providers land


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:n] or "unnamed"


async def _ask_one(spec: str, prompt: str, system: str | None) -> Reply:
    provider, _, model = spec.partition(":")
    if provider == "bedrock":
        return await bedrock.ask(prompt, model=model, system=system)
    if provider == "azure":
        return await azure.ask(prompt, model=model, system=system)
    if provider == "gemini":
        return await gemini.ask(prompt, model=model, system=system)
    return Reply(
        provider=spec, response="", input_tokens=0, output_tokens=0,
        cost_usd=None, latency_ms=0,
        error=f"unknown provider '{provider}' in spec '{spec}'",
    )


async def ask_all(
    prompt: str,
    *,
    providers: list[str] | None = None,
    system: str | None = None,
    save_dir: str | None = None,
    save_slug: str | None = None,
) -> dict:
    """Fan out `prompt` across the given providers in parallel.

    Returns a dict suitable for JSON serialisation back to the MCP caller.
    If `save_dir` is set, also writes one Markdown file per provider plus a
    summary file, all in that directory.
    """
    specs = providers or DEFAULT_PROVIDERS
    results: list[Reply] = [None] * len(specs)  # type: ignore[list-item]

    async with anyio.create_task_group() as tg:
        async def runner(i: int, spec: str) -> None:
            results[i] = await _ask_one(spec, prompt, system)
        for i, spec in enumerate(specs):
            tg.start_soon(runner, i, spec)

    saved: list[str] = []
    if save_dir:
        saved = _persist(save_dir, save_slug, prompt, system, results)

    total_cost = sum((r.cost_usd or 0.0) for r in results)

    return {
        "providers": specs,
        "results": [
            {
                "provider": r.provider,
                "response": r.response,
                "input_tokens": r.input_tokens,
                "output_tokens": r.output_tokens,
                "cost_usd": r.cost_usd,
                "latency_ms": r.latency_ms,
                "error": r.error,
            }
            for r in results
        ],
        "total_cost_usd": round(total_cost, 6),
        "saved_files": saved,
    }


def _persist(
    save_dir: str,
    save_slug: str | None,
    prompt: str,
    system: str | None,
    results: list[Reply],
) -> list[str]:
    d = Path(os.path.expanduser(save_dir))
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    slug = _slug(save_slug or prompt.splitlines()[0][:60] if prompt.strip() else "consultation")
    base = f"{ts}-{slug}"
    files: list[str] = []

    # Per-provider files
    for r in results:
        prov_slug = _slug(r.provider)
        f = d / f"{base}-{prov_slug}.md"
        f.write_text(_render_provider_md(r, prompt, system), encoding="utf-8")
        files.append(str(f))

    # Summary
    summary = d / f"{base}-summary.md"
    summary.write_text(_render_summary_md(prompt, system, results, files), encoding="utf-8")
    files.append(str(summary))
    return files


def _render_provider_md(r: Reply, prompt: str, system: str | None) -> str:
    sys_block = f"\n\n**System prompt:**\n\n```\n{system}\n```\n" if system else ""
    err_block = f"\n\n> ⚠ **Error:** `{r.error}`\n" if r.error else ""
    cost_block = (
        f"- Cost: ${r.cost_usd:.6f} USD\n" if r.cost_usd is not None
        else "- Cost: (no pricing entry for this model)\n"
    )
    return (
        f"# Cabal consultation — {r.provider}\n\n"
        f"- Tokens: {r.input_tokens} in / {r.output_tokens} out\n"
        f"{cost_block}"
        f"- Latency: {r.latency_ms} ms\n"
        f"{err_block}\n"
        f"## Prompt\n\n```\n{prompt}\n```\n"
        f"{sys_block}\n"
        f"## Response\n\n{r.response}\n"
    )


def _render_summary_md(
    prompt: str, system: str | None, results: list[Reply], files: list[str],
) -> str:
    rows = []
    for r in results:
        cost_s = f"${r.cost_usd:.6f}" if r.cost_usd is not None else "—"
        err_s = "✓" if not r.error else f"⚠ {r.error[:40]}"
        rows.append(
            f"| `{r.provider}` | {r.input_tokens} | {r.output_tokens} | "
            f"{cost_s} | {r.latency_ms} ms | {err_s} |"
        )
    total = sum((r.cost_usd or 0.0) for r in results)
    file_list = "\n".join(f"- `{f}`" for f in files)
    sys_block = f"\n\n**System prompt:**\n\n```\n{system}\n```" if system else ""
    return (
        f"# Cabal consultation — summary\n\n"
        f"## Prompt\n\n```\n{prompt}\n```{sys_block}\n\n"
        f"## Results\n\n"
        f"| Provider | Input tokens | Output tokens | Cost (USD) | Latency | Status |\n"
        f"|---|---:|---:|---:|---:|---|\n"
        + "\n".join(rows)
        + f"\n\n**Total cost: ${total:.6f} USD**\n\n"
        f"## Files\n\n{file_list}\n"
    )
