import httpx

from midichlorian import gates, hosts, remote
from midichlorian.hosts import HOSTS
from midichlorian.printers import PrinterState, Probes


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


async def test_luke_tags(monkeypatch):
    p = FakeProbes(BUSY)
    # R26: a tagged Luke run holds too, unless every tag is allowlisted
    assert not (await gates.check_printers("luke", ["print-recordings"], False, p)).ok
    assert not (await gates.check_printers("luke", ["claude-chats"], False, p)).ok
    monkeypatch.setattr(hosts, "LUKE_HOLD_FREE_TAGS", frozenset({"claude-chats"}))
    calls = p.calls
    assert (await gates.check_printers("luke", ["claude-chats"], False, p)).ok
    assert p.calls == calls
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


async def test_multiple_busy_printers_in_one_reason():
    other = PrinterState("octoprint", True, "Prusa: Printing", True)
    g = await gates.check_printers("leia", None, False, FakeProbes(BUSY, other, IDLE))
    assert "Cthulhu: Lifting" in g.reason and "Prusa: Printing" in g.reason
    assert "Operational" not in g.reason


async def test_backup_success_string_on_stderr():
    async def ssh(host, command, timeout=60, stdin=None):
        return remote.RunResult(0, "", "All databases backed up and verified")

    assert (await gates.run_luke_backups(ssh)).ok


# --- the gate with the real probes (R25: unreadable holds, unreachable does not) ---


def _real_probes(cthulhu, octo_body="{\"state\": \"Operational\"}", key="k"):
    async def ssh(host, command, timeout=60, stdin=None):
        if "docker inspect" in command:
            return remote.RunResult(0, "true\n", "")
        return remote.RunResult(0, octo_body, "")

    async def resolver():
        return key

    client = httpx.AsyncClient(transport=httpx.MockTransport(cthulhu))
    return Probes(client, HOSTS["leia"], ssh, resolver)


def _cthulhu_idle(req):
    return httpx.Response(200, json={"connected": True, "print": {"statusLabel": "Idle"}})


async def test_leia_waits_with_no_key_while_octoprint_runs():
    g = await gates.check_printers("leia", None, False, _real_probes(_cthulhu_idle, key=None))
    assert g.wait and "OctoPrint state unreadable: no API key" in g.reason


async def test_leia_waits_on_a_403_body():
    p = _real_probes(_cthulhu_idle, octo_body='{"error": "Forbidden"}')
    g = await gates.check_printers("leia", None, False, p)
    assert g.wait and "OctoPrint state unreadable" in g.reason


async def test_rey_waits_on_cthulhu_500():
    p = _real_probes(lambda r: httpx.Response(500))
    g = await gates.check_printers("rey", None, False, p)
    assert g.wait and "Cthulhu state unreadable: HTTP 500" in g.reason


async def test_rey_proceeds_when_cthulhu_refuses_connections():
    def refused(req):
        raise httpx.ConnectError("refused")

    g = await gates.check_printers("rey", None, False, _real_probes(refused))
    assert g.ok and not g.wait
