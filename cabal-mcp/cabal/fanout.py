"""Parallel fan-out across providers, with optional disk persistence."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import anyio

from . import tracelog
from .providers import anthropic, azure, bedrock, gemini
from .providers.base import Reply

# The default cabal: one voice per provider.
DEFAULT_PROVIDERS = [
    "bedrock:mistral-large",
    "bedrock:llama3-70b",
    "bedrock:nova-pro",
    "azure:gpt-5.4-pro",
    "azure:grok-4.3",
    "gemini:gemini-3-pro",
    "anthropic:opus-4.7",
]

ALL_PROVIDERS = DEFAULT_PROVIDERS  # alias; grows when more providers land

# Prepended to the system prompt when bluntness=True. Licences disagreement
# (LLMs default to agreeable) and asks for falsification over hedging — the
# framing that made the multi-LLM Sinter consultation actually useful.
BLUNTNESS_PREAMBLE = (
    "Be blunt. If any part of the question or design is wrong, hand-wavy, "
    "or based on a misunderstanding, say so directly. Prefer "
    "\"this won't work because X\" over polite hedging. Don't pad with "
    "caveats unless the caveat *is* the answer. If a question is malformed, "
    "point that out instead of answering it as asked. If the questions are "
    "numbered, answer them in order using the same numbering so the replies "
    "can be diffed across models."
)


def _slug(s: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:n] or "unnamed"


async def _ask_one(spec: str, prompt: str, system: str | None) -> Reply:
    log = tracelog.current()
    provider, _, model = spec.partition(":")
    log.log("provider.start", spec=spec, provider=provider, model=model)
    try:
        if provider == "bedrock":
            r = await bedrock.ask(prompt, model=model, system=system)
        elif provider == "azure":
            r = await azure.ask(prompt, model=model, system=system)
        elif provider == "gemini":
            r = await gemini.ask(prompt, model=model, system=system)
        elif provider == "anthropic":
            r = await anthropic.ask(prompt, model=model, system=system)
        else:
            r = Reply(
                provider=spec, response="", input_tokens=0, output_tokens=0,
                cost_usd=None, latency_ms=0,
                error=f"unknown provider '{provider}' in spec '{spec}'",
            )
    except BaseException as e:
        # Providers normally catch their own exceptions and stuff them into
        # Reply.error, but if one ever escapes we want the full traceback in
        # the trace log rather than a silent task-group crash.
        log.exception("provider.uncaught", e, spec=spec)
        raise
    if r.error:
        log.log(
            "provider.error",
            spec=spec, error=r.error, latency_ms=r.latency_ms,
        )
    else:
        log.log(
            "provider.ok",
            spec=spec,
            input_tokens=r.input_tokens,
            output_tokens=r.output_tokens,
            cost_usd=r.cost_usd,
            latency_ms=r.latency_ms,
        )
    return r


async def ask_all(
    prompt: str,
    *,
    providers: list[str] | None = None,
    system: str | None = None,
    save_dir: str | None = None,
    save_slug: str | None = None,
    bluntness: bool = True,
) -> dict:
    """Fan out `prompt` across the given providers in parallel.

    Returns a dict suitable for JSON serialisation back to the MCP caller.
    If `save_dir` is set, also writes one Markdown file per provider plus a
    summary file, all in that directory.

    If `bluntness` is True (the default), `BLUNTNESS_PREAMBLE` is prepended to
    the system prompt — licences disagreement and asks for falsification over
    hedging. Set False for tasks where the agreeable default is what you want.
    """
    specs = providers or DEFAULT_PROVIDERS
    if bluntness:
        system = BLUNTNESS_PREAMBLE + ("\n\n" + system if system else "")
    log = tracelog.start(
        "ask_all",
        providers=specs,
        prompt_chars=len(prompt) if prompt else 0,
        prompt_preview=(prompt or "")[:500],
        has_system=system is not None,
        system_chars=len(system) if system else 0,
        bluntness=bluntness,
        save_dir=save_dir,
    )
    results: list[Reply] = [None] * len(specs)  # type: ignore[list-item]

    try:
        async with anyio.create_task_group() as tg:
            async def runner(i: int, spec: str) -> None:
                results[i] = await _ask_one(spec, prompt, system)
            for i, spec in enumerate(specs):
                tg.start_soon(runner, i, spec)
    except BaseException as e:
        log.exception("fanout.task_group_crashed", e)
        raise

    saved: list[str] = []
    if save_dir:
        try:
            saved = _persist(save_dir, save_slug, prompt, system, results)
            log.log("persist.ok", files=len(saved), save_dir=save_dir)
        except Exception as e:
            log.exception("persist.failed", e, save_dir=save_dir)
            raise

    total_cost = sum((r.cost_usd or 0.0) for r in results)
    error_count = sum(1 for r in results if r.error)
    log.log(
        "call.end",
        providers=len(specs),
        errors=error_count,
        total_cost_usd=round(total_cost, 6),
    )

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
