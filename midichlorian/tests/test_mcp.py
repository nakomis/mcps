import json
import logging

import httpx
import pytest

from midichlorian import mcp_server
from midichlorian.mcp_server import Service

LUKE_SHA = "a" * 40
NEW_SHA = "b" * 40
SERVED = {"cere": ["cal", "leia", "luke", "mace", "rey"], "mace": ["cere"]}


class Estate:
    """Two fake instances behind an httpx.MockTransport."""

    def __init__(self):
        self.posts = []  # (instance, json)
        self.gets = []  # (instance, path, params)
        self.script = {}  # (instance, id) -> list of job dicts (the last repeats)
        self.next_id = {"cere": 70, "mace": 5}
        self.down = False
        self.stale_info = None  # served map returned until /api/info is read twice
        self.info_reads = 0
        self.deploy_400 = []  # queued 400 details

    def job(self, instance, jid, host, state, **kw):
        d = {"id": jid, "instance": instance, "host": host, "sha": LUKE_SHA, "state": state,
             "result": None, "detail": {"requested_sha": LUKE_SHA}, "tags": None}
        d.update(kw)
        return d

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        inst = req.url.host.split(".")[0]
        assert req.headers["authorization"] == "Bearer tok"
        if self.down:
            raise httpx.ConnectError("down")
        path = req.url.path
        if path == "/api/info":
            self.info_reads += 1
            served = SERVED[inst]
            return httpx.Response(200, json={"instance": inst, "served": served, "version": "x"})
        if path == "/api/deploy":
            body = json.loads(req.content)
            self.posts.append((inst, body))
            if self.deploy_400:
                return httpx.Response(400, json={"detail": self.deploy_400.pop(0)})
            jobs = []
            for h in body["hosts"]:
                jid = self.next_id[inst]
                self.next_id[inst] += 1
                jobs.append({"host": h, "job_id": jid, "state": "queued", "merged": False, "reason": None})
                self.script.setdefault((inst, jid), [self.job(inst, jid, h, "queued")])
            return httpx.Response(200, json={"request_id": 1, "jobs": jobs})
        if path.startswith("/api/runs/") and path.endswith("/cancel"):
            return httpx.Response(409, json={"detail": "only a queued or waiting run can be cancelled"})
        if path.startswith("/api/runs/") and path.endswith("/log"):
            self.gets.append((inst, path, dict(req.url.params)))
            return httpx.Response(200, json={"text": "line\n", "next": 5})
        if path.startswith("/api/runs/"):
            jid = int(path.rsplit("/", 1)[1])
            self.gets.append((inst, path, dict(req.url.params)))
            states = self.script[(inst, jid)]
            job = states.pop(0) if len(states) > 1 else states[0]
            return httpx.Response(200, json=job)
        if path in ("/api/hosts", "/api/queue", "/api/runs"):
            self.gets.append((inst, path, dict(req.url.params)))
            return httpx.Response(200, json={"hosts": [], "jobs": [], "runs": []})
        return httpx.Response(404, json={"detail": "nope"})


@pytest.fixture
def estate():
    return Estate()


@pytest.fixture
def sent():
    return []


@pytest.fixture
def svc(estate, sent):
    def notify(text):
        sent.append(text)
        return True

    return Service(
        {"cere": "http://cere.test:9140", "mace": "http://mace.test:9140"},
        "tok",
        transport=estate.transport(),
        poll_interval=0,
        notify=notify,
        default_requester="session-1",
    )


def done(estate, inst, jid, host, state="succeeded", **detail):
    d = {"requested_sha": LUKE_SHA, "deployed_sha": LUKE_SHA,
         "recap": {"ok": 10, "changed": 2, "unreachable": 0, "failed": 0, "skipped": 1}}
    d.update(detail)
    estate.script[(inst, jid)] = [estate.job(inst, jid, host, state, detail=d)]


async def test_single_instance_deploy_returns_at_once_and_delivers_on_success(svc, estate, sent):
    out = await svc.deploy(["luke"], sha="main", tags=["samba"], requester="martin-s")
    assert out["jobs"][0]["job_id"] == 70 and out["jobs"][0]["instance"] == "cere"
    assert "message" in out["note"].lower() or "message" in out["note"]
    inst, body = estate.posts[0]
    assert inst == "cere"
    assert body == {"hosts": ["luke"], "sha": "main", "tags": ["samba"],
                    "override_printer": False, "requester": "martin-s"}
    done(estate, "cere", 70, "luke")
    await svc.wait_idle()
    assert len(sent) == 1  # once
    msg = sent[0]
    assert msg.startswith("[midichlorian]")
    assert "luke" in msg and LUKE_SHA[:8] in msg and "succeeded" in msg
    assert "ok=10" in msg and "changed=2" in msg
    assert "https://midichlorian.home.nakomis.com/#run-70" in msg
    assert not svc.followers


async def test_requester_defaults_from_session(svc, estate):
    await svc.deploy(["luke"])
    assert estate.posts[0][1]["requester"] == "session-1"


async def test_failure_message_has_failing_task_warnings_and_tail_of_20_lines(svc, estate, sent):
    await svc.deploy(["luke"])
    tail = "\n".join(f"L{i}" for i in range(40))
    done(estate, "cere", 70, "luke", "failed", failing_task="Install thing", warnings=["worktree stuck"],
         log_tail=tail, deployed_sha=NEW_SHA, reason="recap failed=1 unreachable=0")
    await svc.wait_idle()
    msg = sent[0]
    assert "failed" in msg and "Install thing" in msg and "worktree stuck" in msg
    assert "L39" in msg and "L20" in msg and "L19" not in msg
    assert NEW_SHA[:8] in msg and LUKE_SHA[:8] in msg  # requested and deployed differ


async def test_waiting_delivered_once_then_terminal(svc, estate, sent):
    await svc.deploy(["rey"])
    wait = estate.job("cere", 70, "rey", "waiting", detail={"gate": "Prusa printing"})
    estate.script[("cere", 70)] = [
        estate.job("cere", 70, "rey", "queued"), wait, wait, wait,
        estate.job("cere", 70, "rey", "succeeded", detail={"deployed_sha": LUKE_SHA, "requested_sha": LUKE_SHA}),
    ]
    await svc.wait_idle()
    assert len(sent) == 2
    assert "waiting" in sent[0] and "Prusa printing" in sent[0]
    assert "succeeded" in sent[1]


async def test_multi_host_same_instance_one_request_one_message_paragraph_each(svc, estate, sent):
    await svc.deploy(["luke", "cal"])
    assert len(estate.posts) == 1 and estate.posts[0][1]["hosts"] == ["luke", "cal"]
    done(estate, "cere", 70, "luke")
    done(estate, "cere", 71, "cal", "already")
    await svc.wait_idle()
    assert len(sent) == 1
    assert sent[0].count("https://midichlorian") == 2


async def test_split_across_instances_group_two_only_after_group_one_succeeds(svc, estate, sent):
    out = await svc.deploy(["luke", "cere"])
    assert [p[0] for p in estate.posts] == ["cere"]  # only group 1 so far
    assert [j["host"] for j in out["jobs"]] == ["luke"]
    assert "cere" in out["note"]  # says the rest follows
    estate.script[("cere", 70)] = [
        estate.job("cere", 70, "luke", "running"),
        estate.job("cere", 70, "luke", "succeeded", detail={"requested_sha": LUKE_SHA}),
    ]
    done(estate, "mace", 5, "cere")  # group 2's job will be finished when first polled
    await svc.wait_idle()
    assert [p[0] for p in estate.posts] == ["cere", "mace"]
    assert estate.posts[1][1]["hosts"] == ["cere"]
    assert estate.posts[1][1]["sha"] == LUKE_SHA  # pinned to what group 1 ran
    assert estate.posts[1][1]["requester"] == "session-1"
    # group 2 runs: its follower polls mace with ?instance=mace
    assert ("mace", "/api/runs/5", {}) in estate.gets or any(g[0] == "mace" for g in estate.gets)
    assert "#run-mace-5" in sent[-1]


async def test_group_one_failure_means_group_two_never_submitted(svc, estate, sent):
    await svc.deploy(["luke", "cere"])
    done(estate, "cere", 70, "luke", "failed", failing_task="boom")
    await svc.wait_idle()
    assert [p[0] for p in estate.posts] == ["cere"]
    assert len(sent) == 1
    assert "skipped" in sent[0].lower() and "cere" in sent[0]


async def test_non_primary_job_is_followed_on_its_own_instance(svc, estate):
    await svc.deploy(["cere"])
    done(estate, "mace", 5, "cere")
    await svc.wait_idle()
    # asked via the mace instance directly, so the id cannot collide with cere's
    assert any(g[0] == "mace" and g[1] == "/api/runs/5" for g in estate.gets)


async def test_host_map_cached_and_refreshed_on_served_by_400(svc, estate):
    await svc.deploy(["luke"])
    await svc.deploy(["cal"])
    assert estate.info_reads == 2  # one per instance, once
    estate.deploy_400 = ["not served by cere: cal (served by mace)"]
    out = await svc.deploy(["cal"])
    assert estate.info_reads == 4  # refreshed (both instances) after the 400
    assert "error" not in out


async def test_deploy_error_is_returned_not_raised(svc, estate):
    estate.deploy_400 = ["unknown hosts: tatooine"]
    out = await svc.deploy(["luke"])
    assert "error" in out and "tatooine" in out["error"]
    assert not svc.followers


async def test_unreachable_server_retries_then_delivers_lost_track(estate, sent):
    s = Service(
        {"cere": "http://cere.test:9140"}, "tok", transport=estate.transport(),
        poll_interval=0.001, lost_after=0.05, notify=lambda t: sent.append(t) or True,
        default_requester="s",
    )
    await s.deploy(["luke"])
    estate.down = True
    await s.wait_idle()
    assert len(sent) == 1 and "lost track" in sent[0].lower() and "#run-70" in sent[0]


async def test_unreachable_then_recovers(estate, sent):
    s = Service(
        {"cere": "http://cere.test:9140"}, "tok", transport=estate.transport(),
        poll_interval=0.001, lost_after=60, notify=lambda t: sent.append(t) or True,
        default_requester="s",
    )
    await s.deploy(["luke"])
    estate.down = True
    import asyncio
    await asyncio.sleep(0.02)
    estate.down = False
    done(estate, "cere", 70, "luke")
    await s.wait_idle()
    assert len(sent) == 1 and "succeeded" in sent[0]


async def test_passthrough_tools(svc, estate):
    await svc.hosts()
    await svc.queue()
    await svc.runs(host="luke", limit=3)
    estate.script[("mace", 70)] = [estate.job("mace", 70, "cere", "failed")]
    await svc.run(70, instance="mace")
    await svc.run_log(70, since=5, instance="mace")
    assert ("cere", "/api/runs", {"host": "luke", "limit": "3"}) in estate.gets
    assert ("mace", "/api/runs/70/log", {"since": "5"}) in estate.gets or any(
        g[0] == "mace" and g[1] == "/api/runs/70/log" for g in estate.gets)
    r = await svc.cancel(70)
    assert "error" in r and "409" in r["error"]


async def test_override_printer_passed_through(svc, estate):
    await svc.deploy(["rey"], override_printer=True)
    assert estate.posts[0][1]["override_printer"] is True


async def test_tool_descriptions_carry_the_override_warning():
    tools = {t.name: t for t in await mcp_server.mcp.list_tools()}
    assert set(tools) == {"hosts", "queue", "runs", "run", "run_log", "deploy", "cancel"}
    assert "Only set override_printer when Martin has explicitly said to in this session." in (
        tools["deploy"].description
    )
    for name in ("run", "run_log", "cancel"):
        assert "instance" in tools[name].inputSchema["properties"]


async def test_no_token_in_logs(svc, estate, caplog):
    caplog.set_level(logging.DEBUG)
    await svc.deploy(["luke"])
    done(estate, "cere", 70, "luke")
    await svc.wait_idle()
    assert "tok\"" not in caplog.text and "Bearer tok" not in caplog.text


def test_default_requester_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_NAME", "HOME-510")
    assert mcp_server.default_requester() == "HOME-510"
    monkeypatch.delenv("CLAUDE_SESSION_NAME")
    assert mcp_server.default_requester().startswith("session-")
