import httpx

from midichlorian.printers import Probes, cthulhu_state


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


async def test_cthulhu_connect_failures_are_idle_and_unreachable():
    for exc in (httpx.ConnectTimeout("slow"), httpx.ConnectError("refused")):

        def down(req, exc=exc):
            raise exc

        async with client(down) as c:
            s = await cthulhu_state(c)
        assert not s.busy and not s.reachable


async def test_cthulhu_error_after_connecting_holds():
    for exc in (
        httpx.ReadTimeout("hung"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("junk"),
    ):

        def broken(req, exc=exc):
            raise exc

        async with client(broken) as c:
            s = await cthulhu_state(c)
        assert s.busy and s.reachable
        assert s.detail == f"Cthulhu state unreadable: {type(exc).__name__}"


async def test_cthulhu_url_is_passed_through():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"connected": True, "print": None})

    async with client(handler) as c:
        await cthulhu_state(c, "http://example.test:1/api/status")
    assert seen == ["http://example.test:1/api/status"]


async def test_probes_states():
    async with client(status("Idle")) as c:
        states = await Probes(c).states()
    assert [s.name for s in states] == ["cthulhu"]
    assert [s.busy for s in states] == [False]


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


async def test_probes_ask_the_configured_cthulhu_url():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"connected": True, "print": None})

    async with client(handler) as c:
        await Probes(c, cthulhu_url="http://c.test/s").states()
    assert seen == ["http://c.test/s"]


def test_no_private_address_in_the_source():
    import inspect

    from midichlorian import printers

    assert "172.29." not in inspect.getsource(printers)
