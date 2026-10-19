import httpx

from midichlorian import remote
from midichlorian.hosts import HOSTS
from midichlorian.printers import Probes, cthulhu_state, octoprint_state

LEIA = HOSTS["leia"]


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def status(label, connected=True):
    return lambda req: httpx.Response(
        200, json={"connected": connected, "print": {"statusLabel": label}}
    )


async def test_cthulhu_busy():
    async with client(status("Lifting")) as c:
        s = await cthulhu_state(c)
    assert s.busy and s.reachable and "Lifting" in s.detail


async def test_cthulhu_idle_labels():
    for label in ("", "Idle", "Complete", "Stopped", "Cancelled"):
        async with client(status(label)) as c:
            assert not (await cthulhu_state(c)).busy


async def test_cthulhu_disconnected_is_idle():
    async with client(status("Lifting", connected=False)) as c:
        s = await cthulhu_state(c)
    assert not s.busy and s.reachable


async def test_cthulhu_connection_error():
    def boom(req):
        raise httpx.ConnectError("down")

    async with client(boom) as c:
        s = await cthulhu_state(c)
    assert not s.busy and not s.reachable and s.detail


async def test_cthulhu_bad_json_and_http_error():
    async with client(lambda r: httpx.Response(200, text="nope")) as c:
        assert not (await cthulhu_state(c)).reachable
    async with client(lambda r: httpx.Response(500)) as c:
        assert not (await cthulhu_state(c)).reachable


def fake_ssh(running="true\n", job='{"state": "Printing"}', rc=0, log=None):
    async def ssh(host, command, timeout=60, stdin=None):
        if log is not None:
            log.append((command, stdin))
        if "docker inspect" in command:
            return remote.RunResult(rc, running, "")
        return remote.RunResult(rc, job, "")

    return ssh


async def test_octoprint_printing_and_key_via_stdin():
    log = []
    s = await octoprint_state(fake_ssh(log=log), LEIA, "sekrit")
    assert s.busy and s.reachable
    assert all("sekrit" not in c for c, _ in log)
    assert log[-1][1] == b"sekrit\n"
    assert "sekrit" not in s.detail


async def test_octoprint_states():
    for state, busy in [("Operational", False), ("Paused", True), ("Finishing", True)]:
        s = await octoprint_state(fake_ssh(job=f'{{"state": "{state}"}}'), LEIA, "k")
        assert s.busy is busy


async def test_octoprint_container_stopped():
    s = await octoprint_state(fake_ssh(running="false\n"), LEIA, "k")
    assert not s.busy and s.reachable


async def test_octoprint_no_key_does_not_probe():
    log = []
    for key in ("", None):
        s = await octoprint_state(fake_ssh(log=log), LEIA, key)
        assert (s.busy, s.reachable, s.detail) == (False, False, "no OctoPrint API key")
    assert log == []


async def test_octoprint_ssh_failure_and_bad_json():
    s = await octoprint_state(fake_ssh(rc=255), LEIA, "k")
    assert not s.busy and not s.reachable
    s = await octoprint_state(fake_ssh(job="<html>"), LEIA, "k")
    assert not s.busy and not s.reachable


async def test_probes_states():
    async with client(status("Idle")) as c:
        states = await Probes(c, LEIA, fake_ssh(), "k").states()
    assert [s.name for s in states] == ["cthulhu", "octoprint"]
    assert [s.busy for s in states] == [False, True]


async def test_cthulhu_malformed_print_payload():
    for payload in ({"connected": True, "print": None},):
        async with client(lambda r, p=payload: httpx.Response(200, json=p)) as c:
            s = await cthulhu_state(c)
        assert not s.busy and s.detail
    for payload in (
        {"connected": True, "print": "x"},
        {"connected": True, "print": {"statusLabel": ["a"]}},
        {"connected": True, "print": {"statusLabel": 5}},
    ):
        async with client(lambda r, p=payload: httpx.Response(200, json=p)) as c:
            s = await cthulhu_state(c)
        assert not s.busy and not s.reachable and s.detail


async def test_octoprint_missing_container_is_idle_and_reachable():
    async def ssh(host, command, timeout=60, stdin=None):
        return remote.RunResult(1, "", "Error: No such object: gateway-octoprint-1")

    s = await octoprint_state(ssh, LEIA, "k")
    assert not s.busy and s.reachable and "not running" in s.detail
