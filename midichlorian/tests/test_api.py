import asyncio
import logging
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from midichlorian import api
from midichlorian.config import Settings
from midichlorian.printers import PrinterState
from midichlorian.remote import RunResult
from midichlorian.store import Store
from midichlorian.worker import SubmitResult

TOKEN = "t" * 64
SHA = "a" * 40


class FakeProbes:
    def __init__(self):
        self.calls = 0
        self.fail = False

    async def states(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return [
            PrinterState("cthulhu", False, "Cthulhu: Idle", True),
            PrinterState("octoprint", True, "Prusa: Printing", True),
        ]


class FakeCoordinator:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store
        self.probes = FakeProbes()
        self.submitted = []
        self.ran = False

    async def submit(self, hosts, sha, tags=None, override_printer=False, requester=""):
        self.submitted.append((hosts, sha, tags, override_printer, requester))
        return SubmitResult(
            7, [{"host": h, "job_id": 70 + i, "state": "queued", "merged": False, "reason": None}
                for i, h in enumerate(hosts)]
        )

    def cancel(self, job_id):
        job = self.store.get_job(job_id)
        if job is None or job.state not in ("queued", "waiting"):
            return False
        self.store.set_state(job_id, "cancelled", finished="2026-10-19T10:00:00Z")
        return True

    async def run_forever(self):
        self.ran = True
        await asyncio.sleep(3600)


def settings(tmp_path, peers=None):
    return Settings(
        instance="cere",
        served=frozenset({"luke", "leia", "cal", "mace", "rey"}),
        data_dir=tmp_path,
        repo_dir=tmp_path / "repo",
        repo_url="x",
        token=TOKEN,
        peers=peers if peers is not None else {},
        bind="127.0.0.1",
        port=9140,
    )


class Peer:
    """A mocked peer instance ('mace', serving cere)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.down = False
        self.reject = False
        self.runs = [{"id": 5, "host": "cere", "state": "succeeded", "created": "2026-10-19T09:00:00Z",
                      "instance": "mace"}]
        self.hosts = [{"name": "cere", "served_by": "mace", "deployed": None, "last": None,
                       "running": None, "queued": 0, "gates": {}}]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError("down", request=request)
        if self.reject or request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "no"})
        path = request.url.path
        if path == "/api/info":
            return httpx.Response(200, json={"instance": "mace", "served": ["cere"], "version": "0"})
        if path == "/api/hosts":
            return httpx.Response(200, json={"hosts": self.hosts, "peers_unreachable": []})
        if path == "/api/queue":
            return httpx.Response(200, json={"jobs": [], "peers_unreachable": []})
        if path == "/api/runs":
            return httpx.Response(200, json={"runs": self.runs, "peers_unreachable": []})
        if path == "/api/runs/5":
            return httpx.Response(200, json={"id": 5, "host": "cere", "instance": "mace"})
        if path == "/api/runs/5/log":
            return httpx.Response(200, json={"text": "peer log\n", "next": 9})
        if path == "/api/runs/5/cancel":
            return httpx.Response(409, json={"detail": "not cancellable"})
        return httpx.Response(404, json={"detail": "nope"})


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "m.db")


@pytest.fixture
def web(tmp_path):
    d = tmp_path / "web"
    d.mkdir()
    (d / "index.html").write_text("<h1>hi</h1>")
    (d / "app.js").write_text("//js")
    (d / "style.css").write_text("/*css*/")
    return d


@pytest.fixture
def peer():
    return Peer()


@pytest.fixture
def env(tmp_path, store, peer, web):
    s = settings(tmp_path, {"mace": "http://mace:9140"})
    coord = FakeCoordinator(s, store)
    client = httpx.AsyncClient(transport=httpx.MockTransport(peer.handler))
    app = api.create_app(s, coord, client, web_dir=web)
    with TestClient(app) as tc:
        tc.headers["Authorization"] = f"Bearer {TOKEN}"
        yield tc, coord, peer, store


@pytest.fixture
def solo(tmp_path, store, web):
    s = settings(tmp_path)
    coord = FakeCoordinator(s, store)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    app = api.create_app(s, coord, client, web_dir=web)
    with TestClient(app) as tc:
        tc.headers["Authorization"] = f"Bearer {TOKEN}"
        yield tc, coord, store


def make_job(store, host="luke", state="queued", tags=None, **fields):
    rid = store.create_request([host], SHA, tags, False, "martin")
    job = store.add_job(rid, host, SHA, tags, 0, False, "martin")
    if state != "queued" or fields:
        store.set_state(job.id, state, **fields)
    return store.get_job(job.id)


# --- auth ----------------------------------------------------------------------------


def test_401_without_or_with_wrong_token(solo):
    tc, _, _ = solo
    bare = TestClient(tc.app)
    assert bare.get("/api/hosts").status_code == 401
    assert bare.get("/api/hosts", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert bare.get("/api/hosts", headers={"X-Midichlorian-Token": "nope"}).status_code == 401
    assert bare.post("/api/deploy", json={}).status_code == 401


def test_both_header_forms_authenticate(solo):
    tc, _, _ = solo
    bare = TestClient(tc.app)
    assert bare.get("/api/info", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200
    assert bare.get("/api/info", headers={"X-Midichlorian-Token": TOKEN}).status_code == 200


def test_healthz_and_static_need_no_auth(solo):
    tc, _, _ = solo
    bare = TestClient(tc.app)
    assert bare.get("/healthz").json() == {"ok": True}
    assert bare.get("/").text == "<h1>hi</h1>"
    assert bare.get("/app.js").status_code == 200
    assert bare.get("/style.css").status_code == 200
    assert bare.get("/favicon.ico").status_code == 404  # absent file, not 401


def test_api_docs_are_not_served(solo):
    tc, _, _ = solo
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert tc.get(path).status_code == 404, path


def test_healthz_is_503_once_the_scheduler_has_died(tmp_path, store, web, caplog):
    s = settings(tmp_path)
    coord = FakeCoordinator(s, store)

    async def dies():
        raise RuntimeError("scheduler bug")

    coord.run_forever = dies
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with TestClient(api.create_app(s, coord, client, web_dir=web)) as tc:
        for _ in range(100):
            resp = tc.get("/healthz")
            if resp.status_code == 503:
                break
            time.sleep(0.01)
        assert resp.status_code == 503
        assert resp.json()["ok"] is False
    assert "scheduler bug" in caplog.text


def test_info(solo):
    tc, _, _ = solo
    body = tc.get("/api/info").json()
    assert body["instance"] == "cere"
    assert body["served"] == ["cal", "leia", "luke", "mace", "rey"]
    assert body["version"]


def test_lifespan_runs_the_coordinator(solo):
    _, coord, _ = solo
    assert coord.ran


def test_no_token_in_logs(solo, caplog):
    tc, _, _ = solo
    caplog.set_level(logging.DEBUG)
    bare = TestClient(tc.app)
    bare.get("/api/hosts", headers={"Authorization": f"Bearer {TOKEN}x"})
    bare.get("/api/hosts", headers={"X-Midichlorian-Token": TOKEN})
    tc.post("/api/deploy", json={"hosts": ["bogus"], "sha": "main", "requester": "m"})
    assert TOKEN not in caplog.text


# --- hosts ---------------------------------------------------------------------------


def test_hosts_local_fields(solo):
    tc, coord, store = solo
    store.record_deploy("luke", SHA, None, "2026-10-19T08:00:00Z")
    make_job(store, "luke", "succeeded", finished="2026-10-19T08:00:00Z")
    running = make_job(store, "luke", "running", started="2026-10-19T09:00:00Z")
    make_job(store, "luke", "queued")
    make_job(store, "luke", "waiting")
    body = tc.get("/api/hosts").json()
    by = {h["name"]: h for h in body["hosts"]}
    assert set(by) == {"luke", "leia", "cal", "mace", "rey"}
    luke = by["luke"]
    assert luke["served_by"] == "cere"
    assert luke["deployed"]["full"]["sha"] == SHA
    assert luke["running"] == running.id
    assert luke["queued"] == 2
    assert luke["last"]["state"] == "succeeded"
    assert luke["gates"]["printers"][1]["busy"] is True
    assert luke["gates"]["printer_sensitive"] == "samba"
    assert luke["gates"]["luke_backups"] == "not reported in v1"
    assert by["cal"]["running"] is None and by["cal"]["last"] is None
    assert body["peers_unreachable"] == []


def test_hosts_printer_state_cached_for_30_seconds(solo, monkeypatch):
    tc, coord, _ = solo
    now = [1000.0]
    monkeypatch.setattr(api, "_monotonic", lambda: now[0])
    tc.get("/api/hosts")
    tc.get("/api/hosts")
    assert coord.probes.calls == 1
    now[0] += 31
    tc.get("/api/hosts")
    assert coord.probes.calls == 2


def test_hosts_probe_failure_is_reported_not_raised(solo):
    tc, coord, _ = solo
    coord.probes.fail = True
    resp = tc.get("/api/hosts")
    assert resp.status_code == 200
    gates = resp.json()["hosts"][0]["gates"]
    assert gates["printers"] == []
    assert "boom" in gates["printers_error"] or "RuntimeError" in gates["printers_error"]


def test_hosts_merge_peer(env):
    tc, _, peer, _ = env
    body = tc.get("/api/hosts").json()
    names = [h["name"] for h in body["hosts"]]
    assert "cere" in names and "luke" in names
    cere = next(h for h in body["hosts"] if h["name"] == "cere")
    assert cere["served_by"] == "mace"
    assert body["peers_unreachable"] == []
    sent = [r for r in peer.requests if r.url.path == "/api/hosts"][0]
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"
    assert sent.url.params["local"] == "1"


def test_hosts_peer_down_gives_partial(env):
    tc, _, peer, _ = env
    peer.down = True
    body = tc.get("/api/hosts").json()
    assert {h["name"] for h in body["hosts"]} == {"luke", "leia", "cal", "mace", "rey"}
    assert body["peers_unreachable"] == ["mace"]


def test_hosts_peer_rejecting_our_token_counts_as_unreachable(env):
    tc, _, peer, _ = env
    peer.reject = True
    body = tc.get("/api/hosts").json()
    assert body["peers_unreachable"] == ["mace"]


def test_peer_list_with_non_dict_items_counts_as_unreachable(env):
    tc, _, peer, _ = env
    peer.hosts = ["junk"]
    peer.runs = [{"id": 5, "created": "2026-10-19T09:00:00Z"}, 7]
    body = tc.get("/api/hosts").json()
    assert body["peers_unreachable"] == ["mace"]
    assert "junk" not in body["hosts"]
    resp = tc.get("/api/runs")
    assert resp.status_code == 200
    assert resp.json()["peers_unreachable"] == ["mace"]


def test_local_param_suppresses_peer_fan_out(env):
    tc, _, peer, _ = env
    body = tc.get("/api/hosts", params={"local": "1"}).json()
    assert peer.requests == []
    assert "cere" not in {h["name"] for h in body["hosts"]}


# --- queue and runs ------------------------------------------------------------------


def test_queue_lists_active_jobs_with_instance(solo):
    tc, _, store = solo
    make_job(store, "luke", "succeeded", finished="2026-10-19T08:00:00Z")
    r = make_job(store, "cal", "running")
    q = make_job(store, "leia", "queued")
    w = make_job(store, "rey", "waiting")
    body = tc.get("/api/queue").json()
    assert [j["id"] for j in body["jobs"]] == [r.id, q.id, w.id]
    assert all(j["instance"] == "cere" for j in body["jobs"])


def test_runs_newest_first_filter_and_limit(solo):
    tc, _, store = solo
    a = make_job(store, "luke", "succeeded")
    b = make_job(store, "cal", "failed")
    c = make_job(store, "luke", "failed")
    ids = [r["id"] for r in tc.get("/api/runs").json()["runs"]]
    assert ids == [c.id, b.id, a.id]
    assert [r["id"] for r in tc.get("/api/runs", params={"host": "luke"}).json()["runs"]] == [c.id, a.id]
    assert [r["id"] for r in tc.get("/api/runs", params={"limit": 1}).json()["runs"]] == [c.id]
    run = tc.get("/api/runs").json()["runs"][0]
    assert run["instance"] == "cere" and run["host"] == "luke" and run["requesters"] == ["martin"]


def test_runs_merge_peer_newest_first(env):
    tc, _, peer, store = env
    job = make_job(store, "luke", "succeeded")
    store.set_state(job.id, "succeeded", finished="x")
    body = tc.get("/api/runs", params={"limit": 10}).json()
    # the local job was created "now" (2026+), the peer's is 2026-10-19T09:00 -> compare by created
    assert {r["instance"] for r in body["runs"]} == {"cere", "mace"}
    created = [r["created"] for r in body["runs"]]
    assert created == sorted(created, reverse=True)
    sent = [r for r in peer.requests if r.url.path == "/api/runs"][0]
    assert sent.url.params["limit"] == "10"


def test_runs_peer_down_partial(env):
    tc, _, peer, store = env
    make_job(store, "luke", "succeeded")
    peer.down = True
    body = tc.get("/api/runs").json()
    assert len(body["runs"]) == 1
    assert body["peers_unreachable"] == ["mace"]


def test_run_detail_and_404(solo):
    tc, _, store = solo
    job = make_job(store, "luke", "failed", detail={"checks": [{"name": "x", "ok": False}]})
    body = tc.get(f"/api/runs/{job.id}").json()
    assert body["id"] == job.id and body["instance"] == "cere"
    assert body["detail"]["checks"][0]["name"] == "x"
    assert tc.get("/api/runs/9999").status_code == 404
    assert tc.get("/api/runs/notanumber").status_code == 422


def test_run_unknown_locally_is_not_searched_on_peers(env):
    tc, _, peer, _ = env
    assert tc.get("/api/runs/5").status_code == 404
    assert peer.requests == []


def test_instance_param_proxies_to_peer(env):
    tc, _, peer, _ = env
    body = tc.get("/api/runs/5", params={"instance": "mace"}).json()
    assert body["instance"] == "mace"
    log = tc.get("/api/runs/5/log", params={"instance": "mace", "since": 3}).json()
    assert log == {"text": "peer log\n", "next": 9}
    sent = [r for r in peer.requests if r.url.path == "/api/runs/5/log"][0]
    assert sent.url.params["since"] == "3"
    assert "instance" not in sent.url.params
    assert sent.headers["authorization"] == f"Bearer {TOKEN}"


def test_instance_param_own_name_is_local(env):
    tc, _, peer, store = env
    job = make_job(store, "luke", "failed")
    assert tc.get(f"/api/runs/{job.id}", params={"instance": "cere"}).json()["id"] == job.id
    assert peer.requests == []


def test_instance_param_unknown_is_404_and_unreachable_is_502(env):
    tc, _, peer, _ = env
    assert tc.get("/api/runs/5", params={"instance": "ghost"}).status_code == 404
    peer.down = True
    assert tc.get("/api/runs/5", params={"instance": "mace"}).status_code == 502


def test_follow_with_instance_is_refused(env):
    tc, _, _, _ = env
    assert tc.get("/api/runs/5/log", params={"instance": "mace", "follow": 1}).status_code == 400


# --- cancel --------------------------------------------------------------------------


def test_cancel_queued_job(solo):
    tc, _, store = solo
    job = make_job(store, "luke", "queued")
    resp = tc.post(f"/api/runs/{job.id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["state"] == "cancelled" and resp.json()["instance"] == "cere"


def test_cancel_running_is_409(solo):
    tc, _, store = solo
    job = make_job(store, "luke", "running")
    resp = tc.post(f"/api/runs/{job.id}/cancel")
    assert resp.status_code == 409
    assert store.get_job(job.id).state == "running"


def test_cancel_unknown_is_404(solo):
    tc, _, _ = solo
    assert tc.post("/api/runs/404/cancel").status_code == 404


def test_cancel_proxies_and_passes_status_through(env):
    tc, _, peer, _ = env
    resp = tc.post("/api/runs/5/cancel", params={"instance": "mace"})
    assert resp.status_code == 409
    assert resp.json() == {"detail": "not cancellable"}


# --- log -----------------------------------------------------------------------------


def log_job(store, tmp_path, content: bytes, state="succeeded"):
    log = tmp_path / "1.log"
    log.write_bytes(content)
    return make_job(store, "luke", state, log_path=str(log))


def test_log_paging(solo, tmp_path):
    tc, _, store = solo
    job = log_job(store, tmp_path, b"one\ntwo\nthree\n")
    first = tc.get(f"/api/runs/{job.id}/log").json()
    assert first == {"text": "one\ntwo\nthree\n", "next": 14}
    rest = tc.get(f"/api/runs/{job.id}/log", params={"since": 4}).json()
    assert rest == {"text": "two\nthree\n", "next": 14}
    assert tc.get(f"/api/runs/{job.id}/log", params={"since": 14}).json() == {"text": "", "next": 14}
    assert tc.get(f"/api/runs/{job.id}/log", params={"since": 999}).json() == {"text": "", "next": 999}


def test_log_replaces_invalid_utf8(solo, tmp_path):
    tc, _, store = solo
    job = log_job(store, tmp_path, b"ok \xff\xfe done\n")
    body = tc.get(f"/api/runs/{job.id}/log").json()
    assert "ok " in body["text"] and "done" in body["text"] and "�" in body["text"]
    assert body["next"] == 11


def test_log_page_is_capped_at_a_line_boundary(solo, tmp_path, monkeypatch):
    tc, _, store = solo
    monkeypatch.setattr(api, "LOG_PAGE_BYTES", 9)
    job = log_job(store, tmp_path, b"aaaa\nbbbb\ncccc\n")
    first = tc.get(f"/api/runs/{job.id}/log").json()
    assert first == {"text": "aaaa\n", "next": 5}
    second = tc.get(f"/api/runs/{job.id}/log", params={"since": first["next"]}).json()
    assert second == {"text": "bbbb\n", "next": 10}


def test_log_without_file_is_empty(solo):
    tc, _, store = solo
    job = make_job(store, "luke", "queued")
    assert tc.get(f"/api/runs/{job.id}/log").json() == {"text": "", "next": 0}
    assert tc.get("/api/runs/9999/log").status_code == 404


def sse(tc, path, **params):
    events, cur = [], {}
    with tc.stream("GET", path, params=params) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith(":"):
                continue
            if line == "":
                if cur:
                    events.append(cur)
                cur = {}
                continue
            key, _, value = line.partition(": ")
            cur[key] = value
    return events


def test_follow_streams_existing_then_closes_when_terminal(solo, tmp_path):
    tc, _, store = solo
    job = log_job(store, tmp_path, b"one\ntwo\npartial")
    events = sse(tc, f"/api/runs/{job.id}/log", follow=1)
    data = [e["data"] for e in events if e.get("event", "message") == "message"]
    assert data == ["one", "two", "partial"]
    assert events[-1]["event"] == "end"
    assert events[-1]["data"] == "succeeded"


def test_follow_streams_appended_lines_until_finish(solo, tmp_path, monkeypatch):
    tc, _, store = solo
    monkeypatch.setattr(api, "LOG_POLL_SECONDS", 0.02)
    job = log_job(store, tmp_path, b"first\n", state="running")
    log = Path(job.log_path)

    def later():
        time.sleep(0.15)
        with open(log, "ab") as f:
            f.write(b"second\nthi")
        time.sleep(0.1)
        with open(log, "ab") as f:
            f.write(b"rd\n")
        time.sleep(0.1)
        store.set_state(job.id, "succeeded", finished="2026-10-19T10:00:00Z")

    t = threading.Thread(target=later)
    t.start()
    events = sse(tc, f"/api/runs/{job.id}/log", follow=1)
    t.join()
    data = [e["data"] for e in events if e.get("event", "message") == "message"]
    assert data == ["first", "second", "third"]
    assert events[-1] == {"event": "end", "data": "succeeded"}


def test_follow_honours_since(solo, tmp_path):
    tc, _, store = solo
    job = log_job(store, tmp_path, b"one\ntwo\n")
    events = sse(tc, f"/api/runs/{job.id}/log", follow=1, since=4)
    assert [e["data"] for e in events if e.get("event", "message") == "message"] == ["two"]


def test_follow_unknown_run_is_404(solo):
    tc, _, _ = solo
    assert tc.get("/api/runs/9999/log", params={"follow": 1}).status_code == 404


# --- deploy --------------------------------------------------------------------------


def deploy(tc, **over):
    body = {"hosts": ["luke"], "sha": "main", "requester": "martin", **over}
    return tc.post("/api/deploy", json=body)


def test_deploy_submits_and_returns_the_result(solo):
    tc, coord, _ = solo
    resp = deploy(tc, hosts=["luke", "cal"], tags=["samba"], override_printer=True)
    assert resp.status_code == 200
    body = resp.json()
    assert body["request_id"] == 7
    assert [j["host"] for j in body["jobs"]] == ["luke", "cal"]
    assert coord.submitted == [(["luke", "cal"], "main", ["samba"], True, "martin")]


def test_deploy_override_printer_defaults_false(solo):
    tc, coord, _ = solo
    deploy(tc)
    assert coord.submitted[0][3] is False
    assert coord.submitted[0][2] is None


def test_deploy_unknown_hosts_400_names_them(solo):
    tc, coord, _ = solo
    resp = deploy(tc, hosts=["luke", "tatooine", "dagobah"])
    assert resp.status_code == 400
    assert "tatooine" in resp.json()["detail"] and "dagobah" in resp.json()["detail"]
    assert coord.submitted == []


def test_deploy_requires_non_empty_requester(solo):
    tc, coord, _ = solo
    assert deploy(tc, requester="").status_code == 400
    assert deploy(tc, requester="   ").status_code == 400
    assert tc.post("/api/deploy", json={"hosts": ["luke"], "sha": "main"}).status_code == 422
    assert coord.submitted == []


def test_deploy_unserved_host_names_serving_instance(env):
    tc, coord, peer, _ = env
    resp = deploy(tc, hosts=["cere"])
    assert resp.status_code == 400
    assert "mace" in resp.json()["detail"]
    assert coord.submitted == []
    # the instance mapping comes from /api/info and is cached
    deploy(tc, hosts=["cere"])
    assert len([r for r in peer.requests if r.url.path == "/api/info"]) == 1


def test_deploy_mixed_instances_400(env):
    tc, coord, _, _ = env
    resp = deploy(tc, hosts=["luke", "cere"])
    assert resp.status_code == 400
    assert "mace" in resp.json()["detail"] and "cere" in resp.json()["detail"]
    assert coord.submitted == []


def test_deploy_peer_unreachable_still_refuses_unserved(env):
    tc, coord, peer, _ = env
    peer.down = True
    resp = deploy(tc, hosts=["cere"])
    assert resp.status_code == 400
    assert "cere" in resp.json()["detail"]
    assert coord.submitted == []


def test_deploy_coordinator_value_error_is_400(solo):
    tc, coord, _ = solo

    async def boom(*a, **k):
        raise ValueError("tags may only contain letters")

    coord.submit = boom
    resp = deploy(tc)
    assert resp.status_code == 400
    assert "tags may only" in resp.json()["detail"]


# --- main helpers --------------------------------------------------------------------


async def test_octoprint_key_from_env():
    async def run(*a, **k):
        raise AssertionError("must not call aws")

    assert await api.resolve_octoprint_key({"MIDICHLORIAN_OCTOPRINT_KEY": "k1"}, run=run) == "k1"


async def test_octoprint_key_from_ssm():
    seen = {}

    async def run(argv, timeout=60, env=None, stdin=None):
        seen.update(argv=argv, env=env)
        return RunResult(0, "secretkey\n", "")

    env = {"AWS_CONFIG_FILE": "/x/config"}
    assert await api.resolve_octoprint_key(env, run=run) == "secretkey"
    assert seen["argv"][:3] == ["aws", "ssm", "get-parameter"]
    assert "/octoprint/api-key" in seen["argv"] and "--with-decryption" in seen["argv"]
    assert seen["argv"][seen["argv"].index("--profile") + 1] == "nakom.is-admin"
    assert seen["argv"][seen["argv"].index("--region") + 1] == "eu-west-2"
    assert seen["env"]["AWS_CONFIG_FILE"] == "/x/config"


async def test_octoprint_key_failure_warns_without_the_key(caplog):
    async def run(argv, timeout=60, env=None, stdin=None):
        return RunResult(255, "", "AccessDenied")

    with caplog.at_level(logging.WARNING):
        assert await api.resolve_octoprint_key({}, run=run) is None
    assert "OctoPrint" in caplog.text and "AccessDenied" in caplog.text


async def test_octoprint_key_empty_output_is_none(caplog):
    async def run(argv, timeout=60, env=None, stdin=None):
        return RunResult(0, "\n", "")

    assert await api.resolve_octoprint_key({}, run=run) is None


async def test_peer_running_check(tmp_path):
    answers = {"jobs": [{"state": "queued"}, {"state": "running"}]}
    seen = []

    def handler(request):
        seen.append(request)
        if answers is None:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=answers)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    check = api.peer_running_check(settings(tmp_path, {"mace": "http://mace:9140/"}), client)
    assert await check("mace") is True
    req = seen[-1]
    assert str(req.url) == "http://mace:9140/api/queue?local=1"
    assert req.headers["authorization"] == f"Bearer {TOKEN}"
    answers = {"jobs": [{"state": "queued"}, "junk", {"state": "waiting"}]}
    assert await check("mace") is False
    answers = {"jobs": "not a list"}
    assert await check("mace") is None
    answers = None
    assert await check("mace") is None
