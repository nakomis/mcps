"""max_tokens reaches every provider's request; SDK clients are mocked."""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from cabal.providers import anthropic as anthropic_p
from cabal.providers import azure as azure_p
from cabal.providers import bedrock as bedrock_p
from cabal.providers import gemini as gemini_p

pytestmark = pytest.mark.anyio


# --- fake SDK clients -------------------------------------------------------

@pytest.fixture
def fake_anthropic(monkeypatch):
    import anthropic

    calls = []

    class _Stream:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text="hi")],
                usage=SimpleNamespace(input_tokens=10, output_tokens=2),
            )

    class FakeClient:
        def __init__(self, **kw):
            self.messages = SimpleNamespace(stream=self._stream)

        def _stream(self, **kw):
            calls.append(kw)
            return _Stream()

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    return lambda: calls[-1]["max_tokens"]


@pytest.fixture
def fake_gemini(monkeypatch):
    from google import genai

    calls = []
    usage = SimpleNamespace(prompt_token_count=10, candidates_token_count=2,
                            thoughts_token_count=None)

    class FakeClient:
        def __init__(self, **kw):
            self.models = SimpleNamespace(generate_content=self._gen)

        def _gen(self, *, model, contents, config):
            calls.append(config)
            return SimpleNamespace(text="hi", usage_metadata=usage)

    monkeypatch.setattr(genai, "Client", FakeClient)
    fake = lambda: calls[-1].max_output_tokens  # noqa: E731
    fake.usage = usage
    return fake


@pytest.fixture
def fake_azure(monkeypatch):
    import azure.ai.inference as inference

    calls = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def complete(self, **kw):
            calls.append(kw)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="hi"))],
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
            )

    monkeypatch.setattr(inference, "ChatCompletionsClient", FakeClient)
    return lambda: calls[-1]["max_tokens"]


BEDROCK_RESPONSES = {
    "mistral-large": {"choices": [{"message": {"content": "hi"}}],
                      "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    "llama3-70b": {"generation": "hi", "prompt_token_count": 10,
                   "generation_token_count": 2},
    "nova-pro": {"output": {"message": {"content": [{"text": "hi"}]}},
                 "usage": {"inputTokens": 10, "outputTokens": 2}},
    "fable-5": {"content": [{"type": "text", "text": "hi"}], "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 2}},
}


def _bedrock_max(model, body):
    if model == "llama3-70b":
        return body["max_gen_len"]
    if model == "nova-pro":
        return body["inferenceConfig"]["maxTokens"]
    return body["max_tokens"]


@pytest.fixture
def fake_bedrock(monkeypatch):
    import boto3

    calls = []

    class FakeClient:
        def invoke_model(self, *, modelId, body, **kw):
            short = next(k for k, v in bedrock_p.MODELS.items() if v == modelId)
            calls.append((short, json.loads(body)))
            payload = json.dumps(BEDROCK_RESPONSES[short]).encode()
            return {"body": io.BytesIO(payload), "ResponseMetadata": {"HTTPHeaders": {
                "x-amzn-bedrock-input-token-count": "11",
                "x-amzn-bedrock-output-token-count": "3",
            }}}

    monkeypatch.setattr(boto3, "client", lambda *a, **kw: FakeClient())
    return lambda: _bedrock_max(*calls[-1])


# --- cases -------------------------------------------------------------------

CASES = (
    [("anthropic", m, anthropic_p.MAX_OUTPUT_TOKENS[m]) for m in anthropic_p.MODELS]
    + [("gemini", m, gemini_p.MAX_OUTPUT_TOKENS[m]) for m in gemini_p.MODELS]
    + [("azure", m, None) for m in azure_p.MODELS]
    + [("bedrock", m, bedrock_p.MAX_OUTPUT_TOKENS[m]) for m in bedrock_p.MODELS]
)
MODULES = {"anthropic": anthropic_p, "gemini": gemini_p, "azure": azure_p,
           "bedrock": bedrock_p}


@pytest.fixture
def sent(request, fake_anthropic, fake_gemini, fake_azure, fake_bedrock):
    return {"anthropic": fake_anthropic, "gemini": fake_gemini,
            "azure": fake_azure, "bedrock": fake_bedrock}


@pytest.mark.parametrize("provider,model,cap", CASES)
async def test_default_is_4096(sent, provider, model, cap):
    r = await MODULES[provider].ask("q", model=model)
    assert r.error is None, r.error
    assert sent[provider]() == 4096
    assert r.notes == []


@pytest.mark.parametrize("provider,model,cap", CASES)
async def test_explicit_value_reaches_request(sent, provider, model, cap):
    r = await MODULES[provider].ask("q", model=model, max_tokens=1000)
    assert r.error is None, r.error
    assert sent[provider]() == 1000
    assert r.notes == []


@pytest.mark.parametrize("provider,model,cap", CASES)
async def test_large_value_clamped_to_model_max(sent, provider, model, cap):
    r = await MODULES[provider].ask("q", model=model, max_tokens=200_000)
    assert r.error is None, r.error
    if cap is None:
        assert sent[provider]() == 200_000
        assert r.notes == []
    else:
        assert sent[provider]() == cap
        assert any("clamped" in n and str(cap) in n for n in r.notes)


@pytest.mark.parametrize("bad", [0, -5])
async def test_invalid_max_tokens_is_an_error_result(fake_gemini, bad):
    r = await gemini_p.ask("q", model="gemini-3.1-pro", max_tokens=bad)
    assert r.error and "max_tokens" in r.error


async def test_gemini_missing_counts_do_not_crash(fake_gemini):
    fake_gemini.usage.candidates_token_count = None
    r = await gemini_p.ask("q", model="gemini-3.1-pro")
    assert r.error is None
    assert r.output_tokens is None
    assert r.cost_usd is None
    assert any("no token count" in n for n in r.notes)


async def test_gemini_thinking_tokens_count_as_output(fake_gemini):
    fake_gemini.usage.thoughts_token_count = 100
    r = await gemini_p.ask("q", model="gemini-3.1-pro")
    assert r.output_tokens == 102


async def test_bedrock_falls_back_to_header_token_counts(fake_bedrock, monkeypatch):
    monkeypatch.setitem(BEDROCK_RESPONSES, "mistral-large",
                        {"choices": [{"message": {"content": "hi"}}]})
    r = await bedrock_p.ask("q", model="mistral-large")
    assert (r.input_tokens, r.output_tokens) == (11, 3)
    assert r.cost_usd is not None
