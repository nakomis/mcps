import time

import pytest

from cabal import pricing
from cabal.fanout import _cost_summary
from cabal.providers.base import Reply, ok_reply


def test_cost_known_counts():
    assert pricing.cost("anthropic:opus-4-8", 1_000_000, 1_000_000) == pytest.approx(30.0)


@pytest.mark.parametrize("in_tok,out_tok", [(None, 10), (10, None), (None, None)])
def test_cost_missing_counts_is_unknown(in_tok, out_tok):
    assert pricing.cost("gemini:gemini-3.1-pro", in_tok, out_tok) is None


def test_cost_unknown_model():
    assert pricing.cost("nope:nothing", 10, 10) is None


def test_ok_reply_notes_missing_counts():
    r = ok_reply("gemini:gemini-3.1-pro", "hi", 12, None, time.monotonic())
    assert r.cost_usd is None
    assert r.output_tokens is None
    assert any("no token count" in n for n in r.notes)


def test_ok_reply_notes_missing_pricing():
    r = ok_reply("azure:mystery", "hi", 12, 3, time.monotonic())
    assert r.cost_usd is None
    assert any("no pricing entry" in n for n in r.notes)


def _reply(provider, cost_usd, error=None, in_tok=1, out_tok=1):
    return Reply(provider=provider, response="", input_tokens=in_tok,
                 output_tokens=out_tok, cost_usd=cost_usd, latency_ms=0, error=error)


def test_cost_summary_sums_known_and_flags_unknown():
    total, unknown = _cost_summary([
        _reply("a:1", 0.5),
        _reply("b:2", None, in_tok=None, out_tok=None),
        _reply("c:3", 0.25),
        # Failed before reaching the model: nothing spent, not flagged.
        _reply("d:4", None, error="boom", in_tok=None, out_tok=None),
    ])
    assert total == pytest.approx(0.75)
    assert unknown == ["b:2"]
