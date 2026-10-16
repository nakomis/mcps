"""ask_all survives one provider blowing up or returning odd usage."""

from __future__ import annotations

from pathlib import Path

import pytest

from cabal import fanout
from cabal.providers.base import Reply

pytestmark = pytest.mark.anyio


def _ok(provider, cost=0.01, in_tok=10, out_tok=5):
    return Reply(provider=provider, response="fine", input_tokens=in_tok,
                 output_tokens=out_tok, cost_usd=cost, latency_ms=1)


@pytest.fixture
def providers(monkeypatch):
    seen = {}

    async def bedrock_ask(prompt, *, model, system=None, max_tokens=None):
        seen["bedrock"] = max_tokens
        return _ok(f"bedrock:{model}")

    async def azure_ask(prompt, *, model, system=None, max_tokens=None):
        raise TypeError("unsupported operand type(s) for /: 'NoneType' and 'int'")

    async def gemini_ask(prompt, *, model, system=None, max_tokens=None):
        seen["gemini"] = max_tokens
        return _ok(f"gemini:{model}", cost=None, out_tok=None)

    async def anthropic_ask(prompt, *, model, system=None, max_tokens=None):
        seen["anthropic"] = max_tokens
        return _ok(f"anthropic:{model}", cost=0.02)

    monkeypatch.setattr(fanout.bedrock, "ask", bedrock_ask)
    monkeypatch.setattr(fanout.azure, "ask", azure_ask)
    monkeypatch.setattr(fanout.gemini, "ask", gemini_ask)
    monkeypatch.setattr(fanout.anthropic, "ask", anthropic_ask)
    return seen


SPECS = ["bedrock:nova-pro", "azure:grok-4.3", "gemini:gemini-3.1-pro",
         "anthropic:opus-4-8"]


async def test_one_provider_raising_does_not_sink_the_rest(providers, tmp_path):
    out = await fanout.ask_all("q", providers=SPECS, max_tokens=16000,
                               save_dir=str(tmp_path / "saved"))
    by = {r["provider"]: r for r in out["results"]}
    assert len(out["results"]) == 4

    assert "TypeError" in by["azure:grok-4.3"]["error"]
    assert by["bedrock:nova-pro"]["error"] is None
    assert by["anthropic:opus-4-8"]["error"] is None

    # Known costs summed; Gemini's unknown cost flagged rather than crashing.
    assert out["total_cost_usd"] == pytest.approx(0.03)
    assert out["total_cost_incomplete"] is True
    assert out["cost_unknown_for"] == ["gemini:gemini-3.1-pro"]

    # max_tokens is threaded through to every provider.
    assert providers == {"bedrock": 16000, "gemini": 16000, "anthropic": 16000}

    # Persisted Markdown renders despite None counts and an error.
    summary = next(Path(f) for f in out["saved_files"] if f.endswith("-summary.md"))
    text = summary.read_text()
    assert "known costs only" in text
    assert "gemini-3-1-pro" in " ".join(out["saved_files"])


async def test_unknown_provider_is_an_error_result(providers):
    out = await fanout.ask_all("q", providers=["nope:thing", "bedrock:nova-pro"])
    by = {r["provider"]: r for r in out["results"]}
    assert "unknown provider" in by["nope:thing"]["error"]
    assert by["bedrock:nova-pro"]["error"] is None
    assert out["total_cost_incomplete"] is False
