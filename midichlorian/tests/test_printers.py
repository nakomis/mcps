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


async def test_cthulhu_junk_or_http_error_from_a_reachable_server_holds():
    # R25: Cthulhu answered, but we cannot read its state: fail closed.
    for handler in (
        lambda r: httpx.Response(200, text="nope"),
        lambda r: httpx.Response(200, json=["a list"]),
        lambda r: httpx.Response(500),
        lambda r: httpx.Response(404),
    ):
        async with client(handler) as c:
            s = await cthulhu_state(c)
        assert s.busy and s.reachable
        assert s.detail.startswith("Cthulhu state unreadable: ")


async def test_cthulhu_timeout_is_idle_and_unreachable():
    def slow(req):
        raise httpx.ReadTimeout("slow")

    async with client(slow) as c:
        s = await cthulhu_state(c)
    assert not s.busy and not s.reachable


async def test_cthulhu_url_is_passed_through():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"connected": True, "print": None})

    async with client(handler) as c:
        await cthulhu_state(c, "http://example.test:1/api/status")
    assert seen == ["http://example.test:1/api/status"]


def key_resolver(*keys):
    """An async resolver returning each of keys in turn (then the last one), counting calls."""
    calls = []

    async def resolve():
        calls.append(1)
        return keys[min(len(calls), len(keys)) - 1]

    resolve.calls = calls
    return resolve


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


async def test_octoprint_no_key_with_container_running_holds():
    log = []
    for key in ("", None):
        s = await octoprint_state(fake_ssh(log=log), LEIA, key)
        assert s.busy and s.reachable
        assert s.detail == "OctoPrint state unreadable: no API key"
    # only the container check ran: there is no key to send
    assert log and all("docker inspect" in c for c, _ in log)


async def test_octoprint_no_key_with_container_stopped_is_idle():
    s = await octoprint_state(fake_ssh(running="false\n"), LEIA, None)
    assert not s.busy and s.reachable and "not running" in s.detail


async def test_octoprint_ssh_unreachable_is_idle():
    s = await octoprint_state(fake_ssh(rc=255), LEIA, "k")
    assert not s.busy and not s.reachable
    s = await octoprint_state(fake_ssh(rc=255), LEIA, None)
    assert not s.busy and not s.reachable


async def test_octoprint_unreadable_state_holds_and_never_shows_the_key():
    bodies = [
        "<html>",  # unparsable
        '{"error": "Forbidden"}',  # 401/403 body: no state
        '{"state": 5}',  # malformed
        '["state"]',  # not an object
    ]
    for body in bodies:
        s = await octoprint_state(fake_ssh(job=body), LEIA, "sekrit")
        assert s.busy and s.reachable, body
        assert s.detail.startswith("OctoPrint state unreadable: ")
        assert "sekrit" not in s.detail


async def test_octoprint_job_command_failure_holds():
    async def ssh(host, command, timeout=60, stdin=None):
        if "docker inspect" in command:
            return remote.RunResult(0, "true\n", "")
        return remote.RunResult(7, "", "curl: (7) Failed to connect")

    s = await octoprint_state(ssh, LEIA, "k")
    assert s.busy and s.detail.startswith("OctoPrint state unreadable: ")


async def test_probes_states():
    async with client(status("Idle")) as c:
        states = await Probes(c, LEIA, fake_ssh(), key_resolver("k")).states()
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
        assert s.busy and s.reachable and s.detail.startswith("Cthulhu state unreadable: ")


async def test_octoprint_missing_container_is_idle_and_reachable():
    async def ssh(host, command, timeout=60, stdin=None):
        return remote.RunResult(1, "", "Error: No such object: gateway-octoprint-1")

    s = await octoprint_state(ssh, LEIA, "k")
    assert not s.busy and s.reachable and "not running" in s.detail


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def test_probes_resolve_a_missing_key_lazily_at_most_every_five_minutes():
    resolver = key_resolver(None, None, "late")
    clock = Clock()
    log = []
    async with client(status("Idle")) as c:
        probes = Probes(c, LEIA, fake_ssh(log=log), resolver, clock=clock)
        first = await probes.states()
        assert first[1].detail == "OctoPrint state unreadable: no API key"
        assert len(resolver.calls) == 1
        clock.now += 60
        await probes.states()  # too soon to ask again
        assert len(resolver.calls) == 1
        clock.now += 240
        await probes.states()  # five minutes on: asks, still nothing
        assert len(resolver.calls) == 2
        clock.now += 300
        later = await probes.states()  # asks again and gets it
        assert len(resolver.calls) == 3
        assert later[1].busy and later[1].detail == "Prusa: Printing"
        clock.now += 3600
        await probes.states()  # a key in hand is never re-resolved
        assert len(resolver.calls) == 3
    assert log[-1][1] == b"late\n"


async def test_probes_resolver_error_counts_as_no_key():
    async def broken():
        raise RuntimeError("sts down")

    async with client(status("Idle")) as c:
        states = await Probes(c, LEIA, fake_ssh(), broken).states()
    assert states[1].busy and "no API key" in states[1].detail


async def test_probes_ask_the_configured_cthulhu_url():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"connected": True, "print": None})

    async with client(handler) as c:
        await Probes(c, LEIA, fake_ssh(), key_resolver("k"), cthulhu_url="http://c.test/s").states()
    assert seen == ["http://c.test/s"]


def test_no_private_address_in_the_source():
    import inspect

    from midichlorian import printers

    assert "172.29." not in inspect.getsource(printers)
