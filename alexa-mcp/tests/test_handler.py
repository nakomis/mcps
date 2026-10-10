import asyncio
import logging

from alexa_announce import handler as h
from alexa_announce.session import (
    AmazonAuthError,
    AmazonError,
    AmazonTransportError,
    SessionExpired,
)

DEVICES = [
    {"accountName": "Bedroom Echo", "serialNumber": "S3", "deviceType": "T",
     "capabilities": ["VOLUME_SETTING"]},
    {"accountName": "Martin's Echo Show", "serialNumber": "S2", "deviceType": "T",
     "capabilities": ["VOLUME_SETTING"]},
]


class FakeSession:
    customer_id = "CUST"

    def __init__(self, run_errors=()):
        self.run_errors = list(run_errors)
        self.ran = []
        self.loops = []
        self.closed = False

    async def devices(self):
        self.loops.append(asyncio.get_running_loop())
        return DEVICES

    async def run(self, node):
        if self.run_errors:
            raise self.run_errors.pop(0)
        self.ran.append(node)

    async def close(self):
        self.closed = True


def provider_for(*sessions, state=None):
    queue = list(sessions)
    opened = []

    async def opener(s):
        if not queue:
            raise SessionExpired("no more sessions")
        opened.append(s)
        session = queue.pop(0)
        if isinstance(session, Exception):
            raise session
        return session

    p = h.Provider(lambda: state or {"refresh_token": "x"}, opener)
    p.opened = opened
    return p


EVENT = {"action": "announce", "text": "Dinner", "devices": ["bedroom echo"]}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_success_returns_canonical_names():
    s = FakeSession()
    assert run(h.handle(EVENT, provider_for(s))) == {
        "ok": True, "action": "announce", "devices": ["Bedroom Echo"]}
    assert s.ran[0]["type"] == "AlexaAnnouncement"


def test_speak_uses_parallel_node():
    s = FakeSession()
    run(h.handle({**EVENT, "action": "speak"}, provider_for(s)))
    assert s.ran[0]["@type"].endswith("ParallelNode")


def test_bad_request_never_opens_session():
    p = provider_for(FakeSession())
    out = run(h.handle({"action": "announce", "text": "", "devices": ["x"]}, p))
    assert out["ok"] is False and out["error"] == "bad_request"
    assert p.opened == []


def test_unknown_device_sends_nothing():
    s = FakeSession()
    out = run(h.handle({**EVENT, "devices": ["Shed"]}, provider_for(s)))
    assert out["error"] == "unknown_devices"
    assert out["detail"]["unknown"] == ["Shed"]
    assert s.ran == []


def test_auth_error_relogs_in_once_and_retries():
    first, second = FakeSession(run_errors=[AmazonAuthError("401")]), FakeSession()
    p = provider_for(first, second)
    assert run(h.handle(EVENT, p))["ok"] is True
    assert first.closed is True
    assert len(second.ran) == 1


def test_auth_error_twice_is_session_expired():
    p = provider_for(FakeSession(run_errors=[AmazonAuthError("401")]),
                     FakeSession(run_errors=[AmazonAuthError("401")]))
    out = run(h.handle(EVENT, p))
    assert out["error"] == "session_expired"


def test_open_failure_is_session_expired():
    out = run(h.handle(EVENT, provider_for(SessionExpired("refresh rejected"))))
    assert out == {"ok": False, "error": "session_expired", "detail": "refresh rejected"}


def test_amazon_error_reports_status_and_truncated_body():
    s = FakeSession(run_errors=[AmazonError(500, "x" * 1000)])
    out = run(h.handle(EVENT, provider_for(s)))
    assert out["error"] == "amazon_error"
    assert out["detail"]["status"] == 500
    assert len(out["detail"]["body"]) == 300


def test_warm_invocations_reuse_session_on_same_loop(monkeypatch):
    s = FakeSession()
    monkeypatch.setattr(h, "_provider", provider_for(s))
    assert h.handler(EVENT, None)["ok"] is True
    assert h.handler(EVENT, None)["ok"] is True
    assert len(s.ran) == 2
    assert s.loops[0] is s.loops[1]


def test_log_line_never_contains_message_text(caplog):
    caplog.set_level(logging.INFO)
    run(h.handle({**EVENT, "text": "secret-plans-xyzzy"}, provider_for(FakeSession())))
    assert "secret-plans-xyzzy" not in caplog.text
    assert "bedroom echo" in caplog.text


def test_transport_error_retries_on_fresh_session():
    first = FakeSession(run_errors=[AmazonTransportError("reset by peer")])
    second = FakeSession()
    p = provider_for(first, second)
    assert run(h.handle(EVENT, p))["ok"] is True
    assert first.closed is True
    assert len(second.ran) == 1


def test_transport_error_twice_is_amazon_error_and_next_call_reopens():
    first = FakeSession(run_errors=[AmazonTransportError("timed out")])
    second = FakeSession(run_errors=[AmazonTransportError("timed out")])
    third = FakeSession()
    p = provider_for(first, second, third)
    out = run(h.handle(EVENT, p))
    assert out["ok"] is False
    assert out["error"] == "amazon_error"
    assert out["detail"]["status"] is None
    assert "timed out" in out["detail"]["body"]
    assert second.closed is True
    # The provider was reset, so the next call opens a new session.
    assert run(h.handle(EVENT, p))["ok"] is True
    assert len(p.opened) == 3
    assert len(third.ran) == 1


def test_opener_transport_error_once_then_succeeds():
    p = provider_for(AmazonTransportError("login timed out"), FakeSession())
    assert run(h.handle(EVENT, p))["ok"] is True
