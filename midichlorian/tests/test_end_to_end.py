"""One path through every real layer: the MCP Service, over HTTP, into create_app and
the real Coordinator (with the worker tests' fake runner, git, gates, ssh and printers),
and back as the callback text a session would receive. It pins down the shapes the
layers pass each other: SubmitResult, job dicts and the detail keys."""

import asyncio

import httpx
import pytest
from test_worker import World

from midichlorian import api, worker
from midichlorian.mcp_server import Service


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "POLL_SECONDS", 0.01)
    return World(tmp_path)


async def wait_for(predicate, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


async def run_estate(world, body):
    coordinator = world.coordinator()
    app = api.create_app(world.settings, coordinator, httpx.AsyncClient(), web_dir=world.tmp)
    sent: list[str] = []
    svc = Service(
        {"cere": "http://cere.test"},
        world.settings.token,
        transport=httpx.ASGITransport(app=app),
        poll_interval=0.01,
        notify=lambda text: sent.append(text) or True,
        default_requester="e2e-session",
    )
    async with app.router.lifespan_context(app):  # the real lifespan: run_forever
        await body(svc, sent)
        await asyncio.wait_for(svc.wait_idle(), 10)
    return sent


async def test_deploy_succeeds_and_the_session_hears_the_outcome(world):
    async def body(svc, sent):
        out = await svc.deploy(["cal"])
        assert out["jobs"] == [{"host": "cal", "job_id": 1, "instance": "cere", "state": "queued"}]

    sent = await run_estate(world, body)
    assert len(sent) == 1
    text = sent[0]
    assert text.startswith("[midichlorian] cal @ c: succeeded.")
    assert "Recap ok=273 changed=" in text and "failed=0" in text
    assert "https://midichlorian.home.nakomis.com/#run-1" in text
    job = world.job(1)
    assert job.requesters == ["e2e-session"] and job.detail["deployed_sha"] == "c"


async def test_deploy_waits_for_a_printer_then_succeeds(world):
    world.probes.busy = True

    async def body(svc, sent):
        await svc.deploy(["rey"])
        await wait_for(lambda: sent)
        assert sent[0].startswith("[midichlorian] rey run #1 is waiting.")
        assert "printer busy — Cthulhu: Exposure" in sent[0]
        assert "untrusted data" in sent[0]  # the gate text is fenced
        world.probes.busy = False
        world.clock.advance(61)

    sent = await run_estate(world, body)
    assert len(sent) == 2
    assert sent[1].startswith("[midichlorian] rey @ c: succeeded.")
