import httpx

from midichlorian import gates, remote
from midichlorian.printers import PrinterState, Probes


class FakeProbes:
    def __init__(self, *states):
        self._states = list(states)
        self.calls = 0

    async def states(self):
        self.calls += 1
        return self._states


BUSY = PrinterState("cthulhu", True, "Cthulhu: Lifting", True)
IDLE = PrinterState("cthulhu", False, "Cthulhu: Idle", True)


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


async def test_remote_run_basics():
    assert (await remote.run(["true"])).rc == 0
    assert (await remote.run(["false"])).rc == 1
    r = await remote.run(["cat"], stdin=b"hi")
    assert r.stdout == "hi"
    r = await remote.run(["sleep", "5"], timeout=0.2)
    assert (r.rc, r.stderr) == (124, "timeout")
    assert (await remote.run(["/nonexistent-bin"])).rc == 127


async def test_multiple_busy_printers_in_one_reason():
    other = PrinterState("other", True, "Other: Printing", True)
    g = await gates.check_printers("rey", None, False, FakeProbes(BUSY, other, IDLE))
    assert "Cthulhu: Lifting" in g.reason and "Other: Printing" in g.reason
    assert "Idle" not in g.reason


# --- the gate with the real probes (R25: unreadable holds, unreachable does not) ---


def _real_probes(cthulhu):
    return Probes(httpx.AsyncClient(transport=httpx.MockTransport(cthulhu)))


async def test_rey_waits_on_cthulhu_500():
    p = _real_probes(lambda r: httpx.Response(500))
    g = await gates.check_printers("rey", None, False, p)
    assert g.wait and "Cthulhu state unreadable: HTTP 500" in g.reason


async def test_rey_proceeds_when_cthulhu_refuses_connections():
    def refused(req):
        raise httpx.ConnectError("refused")

    g = await gates.check_printers("rey", None, False, _real_probes(refused))
    assert g.ok and not g.wait
