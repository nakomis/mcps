import asyncio
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from midichlorian import gates, gitops
from midichlorian.config import Settings
from midichlorian.gates import GateResult
from midichlorian.gitops import GitError
from midichlorian.printers import PrinterState
from midichlorian.remote import RunResult
from midichlorian.store import Store
from midichlorian.worker import Coordinator

FIX = Path(__file__).parent / "fixtures"
CLEAN = (FIX / "leia-clean.log").read_text()
FAILED = (FIX / "rey-failed.log").read_text()
OK = GateResult(True, False, "")


class FakeGit:
    """A linear origin/main history; fetch copies the remote into the local clone."""

    def __init__(self, history=("a", "b", "c")):
        self.remote = list(history)
        self.local = list(history)
        self.side = {"x"}  # commits that exist but are not on main
        self.worktrees: dict[Path, str] = {}
        self.fetches = 0
        self.fetch_error = None

    async def ensure_clone(self, repo, url):
        pass

    async def fetch(self, repo):
        self.fetches += 1
        if self.fetch_error:
            raise GitError(self.fetch_error)
        self.local = list(self.remote)

    async def resolve(self, repo, ref):
        if ref == "origin/main":
            return self.local[-1]
        if ref in self.local or ref in self.side:
            return ref
        raise GitError(f"fatal: unknown revision {ref}")

    async def main_head(self, repo):
        return self.local[-1]

    async def is_ancestor(self, repo, a, b):
        if a in self.local and b in self.local:
            return self.local.index(a) <= self.local.index(b)
        return a == b

    async def is_on_main(self, repo, sha):
        return sha in self.local

    async def add_worktree(self, repo, path, sha):
        path.mkdir(parents=True)
        self.worktrees[path] = sha

    async def remove_worktree(self, repo, path):
        shutil.rmtree(path, ignore_errors=True)
        self.worktrees.pop(path, None)


class FakeRunner:
    def __init__(self, git):
        self.git = git
        self.results: dict[str, tuple[str, int]] = {}
        self.calls: list[dict] = []
        self.gate: asyncio.Event | None = None
        self.active = 0
        self.max_active = 0
        self.raises: Exception | None = None

    async def run(self, host, tags, repo, log, env):
        self.calls.append(
            {
                "host": host.name,
                "tags": tags,
                "repo": repo,
                "env": env,
                "sha": self.git.worktrees.get(repo),
                "exists": repo.is_dir(),
            }
        )
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.raises:
                raise self.raises
            text, rc = self.results.get(host.name, (CLEAN, 0))
            with open(log, "a") as f:  # noqa: ASYNC230 — a tiny local fixture write
                f.write(text)
            return rc
        finally:
            self.active -= 1


class FakeSsh:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.overrides: dict[str, RunResult] = {}

    async def __call__(self, host, command, timeout=60, stdin=None):
        self.calls.append((host.name, command))
        for needle, result in self.overrides.items():
            if needle in command:
                return result
        if "findmnt" in command:
            return RunResult(0, "ro,relatime\n", "")
        if "StartedAt" in command:
            return RunResult(0, "2026-10-19T10:00:00Z\n", "")
        return RunResult(0, "", "")

    def commands(self, needle):
        return [c for _, c in self.calls if needle in c]


class FakeProbes:
    def __init__(self):
        self.busy = False
        self.calls = 0

    async def states(self):
        self.calls += 1
        return [
            PrinterState(
                "cthulhu",
                self.busy,
                "Cthulhu: Exposure" if self.busy else "Cthulhu: Idle",
                True,
            )
        ]


class FakeGates:
    """Real printer logic; scripted AWS, Docker and backup results."""

    check_printers = staticmethod(gates.check_printers)

    def __init__(self):
        self.aws = OK
        self.docker = OK
        self.backups = OK
        self.calls: list[str] = []

    async def check_aws(self, env):
        self.calls.append("aws")
        return self.aws

    async def check_docker(self, env):
        self.calls.append("docker")
        return self.docker

    async def run_luke_backups(self, ssh):
        self.calls.append("backups")
        return self.backups


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class World:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.settings = Settings(
            instance="cere",
            served=frozenset({"luke", "leia", "cal", "mace", "rey"}),
            data_dir=tmp_path / "data",
            repo_dir=tmp_path / "repo",
            repo_url="unused",
            token="t",
            peers={},
            bind="127.0.0.1",
            port=9140,
        )
        self.store = Store(tmp_path / "m.db")
        self.git = FakeGit()
        self.runner = FakeRunner(self.git)
        self.ssh = FakeSsh()
        self.probes = FakeProbes()
        self.gates = FakeGates()
        self.clock = FakeClock()

    def coordinator(self, git=None):
        return Coordinator(
            self.settings,
            self.store,
            self.runner,
            self.probes,
            self.ssh,
            self.clock,
            gates=self.gates,
            git=git or self.git,
        )

    def job(self, job_id):
        return self.store.get_job(job_id)


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


async def settle(c, rounds=3):
    for _ in range(rounds):
        await c.tick()
        await c.drain()


async def until(predicate, tries=200):
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def states(res):
    return [j["state"] for j in res.jobs]


# --- the success path ---------------------------------------------------------


async def test_success_deploys_main_head_and_records_it(w):
    c = w.coordinator()
    res = await c.submit(["leia"], "b", None, False, "sess-1")
    assert states(res) == ["queued"]
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "succeeded"
    assert job.detail["requested_sha"] == "b"
    assert job.detail["deployed_sha"] == "c"
    assert job.detail["recap"]["failed"] == 0 and job.detail["recap"]["ok"] == 273
    assert job.detail["checks"] and all(ch["ok"] for ch in job.detail["checks"])
    assert job.detail["warnings"] == []
    assert job.detail["failing_task"] is None
    assert job.started and job.finished
    assert w.store.deployed("leia")["full"]["sha"] == "c"
    assert w.ssh.commands("/var/log/midichlorian/deployed") == [
        (
            "sudo -n mkdir -p /var/log/midichlorian && "
            "echo 'c 2026-10-19T12:00:00Z full' | sudo -n tee /var/log/midichlorian/deployed >/dev/null"
        )
    ]
    run = w.runner.calls[0]
    assert run["sha"] == "c" and run["exists"]
    assert run["repo"].parent == w.settings.data_dir / "worktrees"
    assert not run["repo"].exists()  # worktree removed after the run
    assert run["env"]["DEPLOY_SKIP_CHECKOUT_CHECK"] == "1"
    assert run["env"]["ANSIBLE_NOCOLOR"] == "1"
    log = Path(job.log_path)
    assert log == w.settings.data_dir / "logs" / f"{job.id}.log"
    header = log.read_text().splitlines()[0]
    assert (
        "leia" in header and "b" in header and "full" in header and "sess-1" in header
    )
    assert "PLAY RECAP" in log.read_text()


async def test_main_ref_resolves_to_origin_main_head(w):
    c = w.coordinator()
    res = await c.submit(["cal"], "main", None, False, "s")
    assert w.job(res.jobs[0]["job_id"]).sha == "c"


async def test_tags_are_passed_and_recorded(w):
    c = w.coordinator()
    res = await c.submit(["cal"], "c", ["b-tag", "a-tag", "a-tag"], False, "s")
    await settle(c)
    assert w.runner.calls[0]["tags"] == ["a-tag", "b-tag"]
    assert w.store.deployed("cal")["tagged"][0]["tags"] == ["a-tag", "b-tag"]
    marker = w.ssh.commands("/var/log/midichlorian/deployed")[0]
    assert "a-tag,b-tag" in marker
    assert w.job(res.jobs[0]["job_id"]).state == "succeeded"


async def test_marker_failure_is_a_warning_not_a_failure(w):
    w.ssh.overrides["/var/log/midichlorian"] = RunResult(
        1, "", "sudo: a password is required"
    )
    c = w.coordinator()
    res = await c.submit(["cal"], "c", None, False, "s")
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "succeeded"
    assert any("a password is required" in x for x in job.detail["warnings"])
    assert w.store.deployed("cal")["full"]["sha"] == "c"


# --- Review Focus 1: fetch before the on-main check ---------------------------


async def test_sha_newer_than_local_clone_is_fetched_not_refused(w):
    w.git.remote.append("d")  # pushed after the runner last fetched
    c = w.coordinator()
    res = await c.submit(["cal"], "d", None, False, "s")
    assert states(res) == ["queued"]
    await settle(c)
    assert w.job(res.jobs[0]["job_id"]).detail["deployed_sha"] == "d"


def _git(cwd, *args):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "T",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "T",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


async def test_real_git_newer_sha_is_fetched_and_run_from_a_worktree(w, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    origin, seed = w.tmp / "origin.git", w.tmp / "seed"
    _git(w.tmp, "init", "--bare", "-b", "main", str(origin))
    _git(w.tmp, "clone", str(origin), str(seed))
    _git(seed, "checkout", "-b", "main")
    (seed / "f").write_text("1")
    _git(seed, "add", "f")
    _git(seed, "commit", "-m", "1")
    _git(seed, "push", "origin", "main")
    _git(w.tmp, "clone", str(origin), str(w.settings.repo_dir))
    (seed / "f").write_text("2")
    _git(seed, "commit", "-am", "2")
    _git(seed, "push", "origin", "main")
    newer = _git(seed, "rev-parse", "HEAD")

    seen = {}

    class Recorder(FakeRunner):
        async def run(self, host, tags, repo, log, env):
            seen["head"] = _git(repo, "rev-parse", "HEAD")
            seen["f"] = (repo / "f").read_text()
            return await super().run(host, tags, repo, log, env)

    w.runner = Recorder(w.git)
    c = w.coordinator(git=gitops)
    res = await c.submit(["cal"], newer[:10], None, False, "s")
    assert states(res) == ["queued"]
    await settle(c)
    assert w.job(res.jobs[0]["job_id"]).state == "succeeded"
    assert seen == {"head": newer, "f": "2"}
    assert "worktrees" not in _git(w.settings.repo_dir, "worktree", "list")


async def test_fetch_failure_is_a_warning_when_the_sha_is_known(w):
    w.git.fetch_error = "could not resolve host github.com"
    c = w.coordinator()
    res = await c.submit(["cal"], "b", None, False, "s")
    assert states(res) == ["queued"]
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "succeeded"
    assert "could not resolve host" in Path(job.log_path).read_text()


# --- refusals -----------------------------------------------------------------


async def test_not_on_main_is_refused_for_every_host(w):
    c = w.coordinator()
    res = await c.submit(["leia", "cal"], "x", None, False, "s")
    assert states(res) == ["failed", "failed"]
    assert all(j["reason"] == "not on origin/main" for j in res.jobs)
    assert all(
        w.job(j["job_id"]).detail["reason"] == "not on origin/main" for j in res.jobs
    )
    await settle(c)
    assert w.runner.calls == []


async def test_unknown_commit_is_refused(w):
    c = w.coordinator()
    res = await c.submit(["cal"], "nope", None, False, "s")
    assert states(res) == ["failed"] and res.jobs[0]["reason"] == "unknown commit"


async def test_unserved_or_bad_requests_raise(w):
    c = w.coordinator()
    with pytest.raises(ValueError):
        await c.submit(["cere"], "c", None, False, "s")
    with pytest.raises(ValueError):
        await c.submit([], "c", None, False, "s")
    with pytest.raises(ValueError):
        await c.submit(["cal", "cal"], "c", None, False, "s")
    with pytest.raises(ValueError):
        await c.submit(["cal"], "c", ["ok", "bad tag;rm"], False, "s")


# --- already deployed -----------------------------------------------------------


async def test_already_deployed_short_circuits_at_submit(w):
    w.store.record_deploy("leia", "c", None, "2026-10-19T00:00:00Z")
    c = w.coordinator()
    res = await c.submit(["leia", "cal"], "b", None, False, "s")
    assert states(res) == ["already", "queued"]
    leia = w.job(res.jobs[0]["job_id"])
    assert leia.detail["deployed_sha"] == "c"
    await settle(c)
    assert [r["host"] for r in w.runner.calls] == ["cal"]  # later host still runs
    assert w.job(res.jobs[1]["job_id"]).state == "succeeded"


async def test_every_host_already_deployed_returns_at_once(w):
    w.store.record_deploy("leia", "c", None, "2026-10-19T00:00:00Z")
    w.store.record_deploy("cal", "c", None, "2026-10-19T00:00:00Z")
    c = w.coordinator()
    res = await c.submit(["leia", "cal"], "c", None, False, "s")
    assert states(res) == ["already", "already"]
    await settle(c)
    assert w.runner.calls == []


async def test_tagged_deploy_covers_only_its_tags(w):
    w.store.record_deploy("cal", "c", ["x", "y"], "2026-10-19T00:00:00Z")
    c = w.coordinator()
    assert states(await c.submit(["cal"], "b", ["x"], False, "s")) == ["already"]
    assert states(await c.submit(["cal"], "b", ["z"], False, "s")) == ["queued"]


async def test_already_is_rechecked_when_the_job_reaches_the_front(w):
    c = w.coordinator()
    res = await c.submit(["cal"], "b", None, False, "s")
    w.store.record_deploy(
        "cal", "c", None, "2026-10-19T00:00:00Z"
    )  # e.g. by another job
    await settle(c)
    assert w.job(res.jobs[0]["job_id"]).state == "already"
    assert w.runner.calls == []


# --- Review Focus 4: first host fails ---------------------------------------------


async def test_first_host_fails_later_hosts_skipped(w):
    w.runner.results["rey"] = (FAILED, 2)
    c = w.coordinator()
    res = await c.submit(["rey", "leia", "cal"], "c", None, False, "sess-a")
    await settle(c)
    rey, leia, cal = (w.job(j["job_id"]) for j in res.jobs)
    assert rey.state == "failed"
    assert rey.detail["failing_task"] == "Create the plex mount point"
    assert "fatal: [host-c]" in rey.detail["log_tail"]
    assert rey.detail["log_tail"].splitlines()[-1] == "EXIT 2"
    assert w.store.deployed("rey") is None
    assert w.ssh.commands("/var/log/midichlorian/deployed") == []
    for later in (leia, cal):
        assert later.state == "skipped"
        assert "rey" in later.detail["reason"]
        assert later.requesters == ["sess-a"]
    assert [r["host"] for r in w.runner.calls] == ["rey"]


async def test_failed_verification_fails_a_clean_playbook(w):
    w.ssh.overrides["findmnt"] = RunResult(0, "rw,relatime\n", "")
    c = w.coordinator()
    res = await c.submit(["cal"], "c", None, False, "s")
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "failed"
    assert next(ch for ch in job.detail["checks"] if not ch["ok"])["name"] == "ro root"
    assert "ro root" in job.detail["reason"]
    assert job.detail["log_tail"]
    assert w.store.deployed("cal") is None


async def test_no_recap_is_a_failure(w):
    w.runner.results["cal"] = ("ERROR! the playbook could not be found\n", 0)
    c = w.coordinator()
    res = await c.submit(["cal"], "c", None, False, "s")
    await settle(c)
    assert w.job(res.jobs[0]["job_id"]).state == "failed"


async def test_worktree_cleanup_failure_is_only_a_warning(w):
    async def broken_remove(repo, path):
        raise GitError("fatal: worktree locked")

    w.git.remove_worktree = broken_remove
    c = w.coordinator()
    res = await c.submit(["cal"], "c", None, False, "s")
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "succeeded"
    assert any("worktree locked" in x for x in job.detail["warnings"])


async def test_internal_error_after_the_run_keeps_the_log_tail(w, monkeypatch):
    from midichlorian import worker

    def broken_parse(text):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(worker.recap, "parse", broken_parse)
    c = w.coordinator()
    res = await c.submit(["cal"], "c", None, False, "s")
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "failed" and "internal error" in job.detail["reason"]
    assert "PLAY RECAP" in job.detail["log_tail"]


async def test_finished_jobs_leave_no_recheck_entries(w):
    w.probes.busy = True
    c = w.coordinator()
    job_id = (await c.submit(["leia"], "c", None, False, "s")).jobs[0]["job_id"]
    await settle(c)
    assert job_id in c._recheck_at
    w.gates.aws = GateResult(False, False, "ExpiredToken")  # fails at the next re-check
    w.clock.advance(61)
    await settle(c)
    assert w.job(job_id).state == "failed"
    assert c._recheck_at == {}


async def test_runner_crash_fails_the_job_and_cleans_up(w):
    w.runner.raises = RuntimeError("boom")
    c = w.coordinator()
    res = await c.submit(["cal"], "c", None, False, "s")
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "failed" and "boom" in job.detail["reason"]
    assert w.git.worktrees == {}
    assert w.store.deployed("cal") is None


# --- gates ---------------------------------------------------------------------------


async def test_printer_busy_waits_then_runs_when_idle(w):
    w.probes.busy = True
    c = w.coordinator()
    res = await c.submit(["leia"], "c", None, False, "s")
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "waiting"
    assert (
        "printer busy" in job.detail["gate"]
        and "Cthulhu: Exposure" in job.detail["gate"]
    )
    assert w.probes.calls == 1
    w.clock.advance(30)
    await settle(c)
    assert w.probes.calls == 1  # not re-checked within 60 s
    w.clock.advance(31)
    q = c.subscribe()
    await settle(c)
    assert w.probes.calls == 2 and w.job(job.id).state == "waiting"
    assert q.empty()  # same hold, same reason: no new event
    w.probes.busy = False
    w.clock.advance(61)
    await settle(c)
    job = w.job(job.id)
    assert job.state == "succeeded"
    assert "gate" not in job.detail
    assert Path(job.log_path).read_text().count("waiting: printer busy") == 1


async def test_printer_override_runs_through_a_busy_printer(w):
    w.probes.busy = True
    c = w.coordinator()
    res = await c.submit(["rey"], "c", None, True, "s")
    await settle(c)
    assert w.job(res.jobs[0]["job_id"]).state == "succeeded"


async def test_waiting_job_holds_its_host(w):
    w.probes.busy = True
    c = w.coordinator()
    first = await c.submit(["leia"], "c", None, False, "s1")
    second = await c.submit(
        ["leia", "cal"], "c", None, True, "s2"
    )  # not merged: multi-host
    await settle(c)
    assert w.job(first.jobs[0]["job_id"]).state == "waiting"
    assert w.job(second.jobs[0]["job_id"]).state == "queued"
    assert w.runner.calls == []


async def test_aws_gate_failure_fails_at_once(w):
    w.gates.aws = GateResult(False, False, "ExpiredToken")
    c = w.coordinator()
    res = await c.submit(["cal", "mace"], "c", None, False, "s")
    await settle(c)
    cal, mace = (w.job(j["job_id"]) for j in res.jobs)
    assert cal.state == "failed" and "ExpiredToken" in cal.detail["reason"]
    assert mace.state == "skipped"
    assert w.runner.calls == []


async def test_luke_backups_run_last_and_failure_leaves_luke_untouched(w):
    w.gates.backups = GateResult(False, False, "Luke backups failed (rc 1)")
    c = w.coordinator()
    res = await c.submit(["luke"], "c", ["claude-chats"], False, "s")
    await settle(c)
    job = w.job(res.jobs[0]["job_id"])
    assert job.state == "failed" and "Luke backups failed" in job.detail["reason"]
    assert w.gates.calls == ["aws", "docker", "backups"]
    assert w.runner.calls == [] and w.git.worktrees == {}


async def test_docker_gate_only_for_controller_side_builds(w):
    w.gates.docker = GateResult(False, False, "Cannot connect to the Docker daemon")
    c = w.coordinator()
    res = {
        h: await c.submit([h], "c", None, False, "s")
        for h in ("rey", "cal", "leia", "mace")
    }
    await settle(c)
    for h in ("rey", "cal", "leia"):
        job = w.job(res[h].jobs[0]["job_id"])
        assert job.state == "failed" and "Docker gate" in job.detail["reason"], h
    assert w.job(res["mace"].jobs[0]["job_id"]).state == "succeeded"
    assert w.gates.calls.count("docker") == 3


async def test_cancel_during_gates_never_starts_luke_backups(w):
    release = asyncio.Event()

    async def slow_docker(env):
        w.gates.calls.append("docker")
        await release.wait()
        return OK

    w.gates.check_docker = slow_docker
    c = w.coordinator()
    job_id = (await c.submit(["luke"], "c", ["x"], False, "s")).jobs[0]["job_id"]
    await c.tick()
    await until(lambda: "docker" in w.gates.calls)
    assert c.cancel(job_id)
    release.set()
    await settle(c)
    assert "backups" not in w.gates.calls
    assert w.job(job_id).state == "cancelled"


async def test_printer_rechecked_after_luke_backups(w):
    async def backups_while_a_print_starts(ssh):
        w.gates.calls.append("backups")
        w.probes.busy = True  # a print started during the (long) backups
        return OK

    w.gates.run_luke_backups = backups_while_a_print_starts
    c = w.coordinator()
    job_id = (await c.submit(["luke"], "c", None, False, "s")).jobs[0]["job_id"]
    await settle(c)
    job = w.job(job_id)
    assert job.state == "waiting" and "printer busy" in job.detail["gate"]
    assert w.runner.calls == []
    w.probes.busy = False
    w.gates.run_luke_backups = FakeGates.run_luke_backups.__get__(w.gates)
    w.clock.advance(61)
    await settle(c)
    assert w.job(job_id).state == "succeeded"
    assert w.gates.calls.count("backups") == 2  # backups re-run before the real run


async def test_gate_passes_are_logged(w):
    c = w.coordinator()
    job_id = (await c.submit(["luke"], "c", None, False, "s")).jobs[0]["job_id"]
    await settle(c)
    log = Path(w.job(job_id).log_path).read_text()
    for line in ("AWS ok", "printers idle", "Docker ok", "Luke's backups ok"):
        assert f"[midichlorian] {line}" in log, line
    over = (await c.submit(["rey"], "c", None, True, "s")).jobs[0]["job_id"]
    await settle(c)
    assert "printer hold overridden by Martin" in Path(w.job(over).log_path).read_text()


# --- Review Focus 2: a second request while running ------------------------------------


async def test_second_request_for_running_host_queues_behind_it(w):
    w.runner.gate = asyncio.Event()
    c = w.coordinator()
    first = await c.submit(["leia"], "c", None, False, "s1")
    await c.tick()
    await until(lambda: w.runner.active == 1)
    first_id = first.jobs[0]["job_id"]
    assert w.job(first_id).state == "running"

    w.git.remote.append("d")
    second = await c.submit(["leia"], "d", None, False, "s2")
    second_id = second.jobs[0]["job_id"]
    assert second_id != first_id and not second.jobs[0]["merged"]
    assert second.jobs[0]["state"] == "queued"
    assert w.job(first_id).requesters == ["s1"]

    await c.tick()
    for _ in range(20):
        await asyncio.sleep(0)
    assert len(w.runner.calls) == 1  # never in parallel

    w.runner.gate.set()
    await settle(c)
    assert w.runner.max_active == 1
    assert w.job(first_id).state == "succeeded"
    assert w.job(second_id).state == "succeeded"
    assert w.job(second_id).detail["deployed_sha"] == "d"


async def test_request_during_gate_checks_neither_merges_nor_starts(w):
    """A job still in its gates (Luke's backups take minutes) is not yet `running`,
    but it owns its host: a new request must not join it or start beside it."""
    release = asyncio.Event()

    async def slow_backups(ssh):
        w.gates.calls.append("backups")
        await release.wait()
        return OK

    w.gates.run_luke_backups = slow_backups
    c = w.coordinator()
    first = await c.submit(["luke"], "c", ["x"], False, "s1")
    await c.tick()
    await until(lambda: "backups" in w.gates.calls)
    assert w.job(first.jobs[0]["job_id"]).state == "queued"

    second = await c.submit(["luke"], "c", None, False, "s2")
    assert (
        second.jobs[0]["job_id"] != first.jobs[0]["job_id"]
    )  # no merge: its gates saw ["x"]
    assert w.job(first.jobs[0]["job_id"]).tags == ["x"]
    await c.tick()
    for _ in range(20):
        await asyncio.sleep(0)
    assert w.gates.calls.count("aws") == 1  # the second job has not started

    release.set()
    await settle(c)
    assert w.runner.max_active == 1
    assert [r["tags"] for r in w.runner.calls] == [["x"], None]


async def test_different_hosts_run_concurrently(w):
    w.runner.gate = asyncio.Event()
    c = w.coordinator()
    await c.submit(["leia"], "c", None, False, "s1")
    await c.submit(["cal"], "c", None, False, "s2")
    await c.tick()
    await until(lambda: w.runner.active == 2)
    w.runner.gate.set()
    await settle(c)
    assert {r["host"] for r in w.runner.calls} == {"leia", "cal"}


async def test_queued_request_merges_into_queued_job(w):
    c = w.coordinator()
    first = await c.submit(["leia"], "b", ["x"], False, "s1")
    second = await c.submit(["leia"], "c", ["y"], False, "s2")
    job_id = first.jobs[0]["job_id"]
    assert second.jobs[0]["job_id"] == job_id and second.jobs[0]["merged"]
    job = w.job(job_id)
    assert job.requesters == ["s1", "s2"] and job.sha == "c" and job.tags == ["x", "y"]
    older = await c.submit(["leia"], "a", None, False, "s3")
    job = w.job(job_id)
    assert older.jobs[0]["job_id"] == job_id and job.sha == "c" and job.tags is None
    await settle(c)
    assert len(w.runner.calls) == 1


async def test_override_and_non_override_requests_never_merge(w):
    c = w.coordinator()
    over = await c.submit(["luke"], "c", ["samba"], True, "s1")
    plain = await c.submit(["luke"], "c", None, False, "s2")
    assert plain.jobs[0]["job_id"] != over.jobs[0]["job_id"]
    assert not plain.jobs[0]["merged"]
    assert w.job(over.jobs[0]["job_id"]).tags == ["samba"]
    assert w.job(plain.jobs[0]["job_id"]).override_printer is False
    again = await c.submit(["luke"], "c", ["y"], True, "s3")  # same override: merges
    assert again.jobs[0]["job_id"] == over.jobs[0]["job_id"]


async def test_multi_host_request_does_not_merge_its_first_host(w):
    c = w.coordinator()
    first = await c.submit(["leia"], "c", None, False, "s1")
    second = await c.submit(["leia", "cal"], "c", None, False, "s2")
    assert second.jobs[0]["job_id"] != first.jobs[0]["job_id"]
    await settle(c)
    # the second request's own leia job finds leia already at c, then cal runs
    assert w.job(second.jobs[0]["job_id"]).state == "already"
    assert [r["host"] for r in w.runner.calls] == ["leia", "cal"]


# --- cancel ------------------------------------------------------------------------------


async def test_cancel_queued_but_not_running(w):
    w.runner.gate = asyncio.Event()
    c = w.coordinator()
    a = (await c.submit(["leia"], "c", None, False, "s1")).jobs[0]["job_id"]
    await c.tick()
    await until(lambda: w.runner.active == 1)
    b = (await c.submit(["leia", "cal"], "c", None, False, "s2")).jobs
    assert c.cancel(a) is False  # running
    assert c.cancel(b[0]["job_id"]) is True
    assert c.cancel(b[0]["job_id"]) is False  # already cancelled
    w.runner.gate.set()
    await settle(c)
    assert w.job(a).state == "succeeded"
    assert w.job(b[0]["job_id"]).state == "cancelled"
    assert w.job(b[1]["job_id"]).state == "skipped"
    assert len(w.runner.calls) == 1


async def test_cancel_during_gates_stays_cancelled(w):
    release = asyncio.Event()

    async def slow_failing_aws(env):
        w.gates.calls.append("aws")
        await release.wait()
        return GateResult(False, False, "ExpiredToken")

    w.gates.check_aws = slow_failing_aws
    c = w.coordinator()
    job_id = (await c.submit(["cal"], "c", None, False, "s")).jobs[0]["job_id"]
    await c.tick()
    await until(lambda: "aws" in w.gates.calls)
    assert c.cancel(job_id) is True
    release.set()
    await settle(c)
    assert w.job(job_id).state == "cancelled"
    assert w.runner.calls == []


# --- Review Focus 5: restart with a running job ------------------------------------------


async def test_restart_marks_running_interrupted_and_queue_continues(w):
    rid = w.store.create_request(["leia"], "c", None, False, "old")
    stuck = w.store.add_job(rid, "leia", "c", None, 0, False, "old")
    w.store.set_state(stuck.id, "running", started="2026-10-19T11:00:00Z")
    rid2 = w.store.create_request(["leia"], "c", None, False, "next")
    nxt = w.store.add_job(rid2, "leia", "c", None, 0, False, "next")
    stale = w.settings.data_dir / "worktrees" / str(stuck.id)
    stale.mkdir(parents=True)

    c = w.coordinator()
    job = w.job(stuck.id)
    assert job.state == "interrupted"
    assert "check the host" in job.detail["reason"]
    await settle(c)
    assert not stale.exists()  # left-over worktree cleaned up
    assert w.job(nxt.id).state == "succeeded"
    assert w.job(stuck.id).state == "interrupted"  # never re-run
    assert len(w.runner.calls) == 1


# --- events ------------------------------------------------------------------------------


async def test_subscribers_see_every_state_change(w):
    c = w.coordinator()
    q = c.subscribe()
    res = await c.submit(["cal"], "c", None, False, "s")
    await settle(c)
    seen = []
    while not q.empty():
        ev = q.get_nowait()
        assert ev["type"] == "job"
        if ev["job"]["id"] == res.jobs[0]["job_id"]:
            seen.append(ev["job"]["state"])
    assert seen[0] == "queued" and "running" in seen and seen[-1] == "succeeded"
    c.unsubscribe(q)
    await c.submit(["mace"], "c", None, False, "s")
    assert q.empty()


async def test_slow_subscriber_never_blocks(w):
    c = w.coordinator()
    q = c.subscribe(maxsize=2)
    res = await c.submit(["cal"], "c", None, False, "s")
    await settle(c)
    assert q.qsize() == 2
    events = [q.get_nowait(), q.get_nowait()]
    assert events[-1]["job"]["state"] == "succeeded"  # oldest dropped, newest kept
    assert events[-1]["job"]["id"] == res.jobs[0]["job_id"]


async def test_run_forever_processes_submissions(w):
    c = w.coordinator()
    loop = asyncio.create_task(c.run_forever())
    try:
        res = await c.submit(["cal"], "c", None, False, "s")
        for _ in range(500):
            if w.job(res.jobs[0]["job_id"]).state == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert w.job(res.jobs[0]["job_id"]).state == "succeeded"
    finally:
        loop.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop
