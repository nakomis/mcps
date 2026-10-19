from midichlorian import gates, remote
from midichlorian.printers import PrinterState


class FakeProbes:
    def __init__(self, *states):
        self._states = list(states)
        self.calls = 0

    async def states(self):
        self.calls += 1
        return self._states


BUSY = PrinterState("cthulhu", True, "Cthulhu: Lifting", True)
IDLE = PrinterState("octoprint", False, "Prusa: Operational", True)


async def test_rey_waits_when_busy():
    p = FakeProbes(BUSY, IDLE)
    g = await gates.check_printers("rey", None, False, p)
    assert (g.ok, g.wait) == (False, True) and "Cthulhu: Lifting" in g.reason


async def test_idle_passes():
    g = await gates.check_printers("rey", None, False, FakeProbes(IDLE))
    assert g.ok and not g.wait


async def test_override_passes_without_probing():
    p = FakeProbes(BUSY)
    g = await gates.check_printers("rey", None, True, p)
    assert g.ok and g.reason == "printer hold overridden by Martin" and p.calls == 0


async def test_cal_never_probes():
    p = FakeProbes(BUSY)
    assert (await gates.check_printers("cal", None, False, p)).ok and p.calls == 0


async def test_luke_tags():
    p = FakeProbes(BUSY)
    assert (await gates.check_printers("luke", ["claude-chats"], False, p)).ok
    assert p.calls == 0
    assert not (await gates.check_printers("luke", None, False, p)).ok
    assert not (await gates.check_printers("luke", ["samba"], False, p)).ok


async def test_check_aws_and_docker(monkeypatch):
    seen = []

    async def fake_run(argv, timeout=60, env=None, stdin=None):
        seen.append((argv, env))
        return remote.RunResult(1, "", "  expired token \n")

    monkeypatch.setattr(remote, "run", fake_run)
    env = {"AWS_CONFIG_FILE": "/x"}
    g = await gates.check_aws(env)
    assert (g.ok, g.wait, g.reason) == (False, False, "expired token")
    assert seen[0][1] is env and "nakom.is-admin" in seen[0][0]
    assert not (await gates.check_docker({"DOCKER_HOST": "y"})).ok
    assert seen[1][0] == ["docker", "info"]


async def test_backups():
    def mk(rc, out):
        async def ssh(host, command, timeout=60, stdin=None):
            assert host.name == "luke" and command == "./run-backups.sh" and timeout >= 1800
            return remote.RunResult(rc, out, "")

        return ssh

    assert (await gates.run_luke_backups(mk(0, "All databases backed up and verified\n"))).ok
    g = await gates.run_luke_backups(mk(0, "nothing"))
    assert not g.ok
    lines = "\n".join(f"line{i}" for i in range(50))
    g = await gates.run_luke_backups(mk(1, lines))
    assert not g.ok and "line49" in g.reason and "line10" not in g.reason


async def test_remote_run_basics():
    assert (await remote.run(["true"])).rc == 0
    assert (await remote.run(["false"])).rc == 1
    r = await remote.run(["cat"], stdin=b"hi")
    assert r.stdout == "hi"
    r = await remote.run(["sleep", "5"], timeout=0.2)
    assert (r.rc, r.stderr) == (124, "timeout")
    assert (await remote.run(["/nonexistent-bin"])).rc == 127
