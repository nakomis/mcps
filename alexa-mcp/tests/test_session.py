"""AlexaSession against a fake alexapy/aiohttp (neither is installed in dev)."""

import asyncio
import sys
import types

import pytest

from alexa_announce import session as s


class Errors:
    class AlexapyConnectionError(Exception): ...
    class AlexapyLoginError(Exception): ...
    class AlexapyTooManyRequestsError(Exception): ...


class FakeLogin:
    def __init__(self, *a, **kw):
        self.chain = {"refresh": True, "exchange": True, "csrf": True, "test": True}
        self.raise_in = None
        self.calls = []
        self.closed = False
        self.status = {}
        FakeLogin.last = self

    def _create_session(self):
        self.calls.append("create")

    async def _step(self, name):
        self.calls.append(name)
        if self.raise_in == name:
            raise RuntimeError("boom")
        return self.chain[name]

    async def refresh_access_token(self):
        return await self._step("refresh")

    async def exchange_token_for_cookies(self):
        return await self._step("exchange")

    async def get_csrf(self):
        return await self._step("csrf")

    async def test_loggedin(self, *, rebuild_session=True):
        assert rebuild_session is False
        return await self._step("test")

    async def login(self, *a, **kw):
        raise AssertionError("login() must never be called")

    async def close(self):
        self.closed = True


class FakeAPI:
    error = None
    result = [{"accountName": "x"}]

    @classmethod
    async def get_devices(cls, login):
        if cls.error:
            raise cls.error
        return cls.result


class ClientError(Exception): ...
class ClientConnectorError(ClientError): ...


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    alexapy = types.ModuleType("alexapy")
    alexapy.AlexaLogin = FakeLogin
    alexapy.AlexaAPI = FakeAPI
    errors = types.ModuleType("alexapy.errors")
    for k, v in vars(Errors).items():
        if k.startswith("Alexapy"):
            setattr(errors, k, v)
    alexapy.errors = errors
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientError = ClientError
    aiohttp.ClientConnectorError = ClientConnectorError
    monkeypatch.setitem(sys.modules, "alexapy", alexapy)
    monkeypatch.setitem(sys.modules, "alexapy.errors", errors)
    monkeypatch.setitem(sys.modules, "aiohttp", aiohttp)
    FakeAPI.error = None
    FakeAPI.result = [{"accountName": "x"}]


STATE = {"email": "a@b.c", "refresh_token": "r"}


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def open_session(tmp_path):
    return run(s.AlexaSession.open(STATE, str(tmp_path)))


def test_open_uses_refresh_chain_never_login(tmp_path):
    sess = open_session(tmp_path)
    assert isinstance(sess, s.AlexaSession)
    assert FakeLogin.last.calls == ["create", "refresh", "exchange", "csrf", "test"]


@pytest.mark.parametrize("failing", ["refresh", "exchange", "csrf", "test"])
def test_open_chain_failure_is_session_expired(tmp_path, failing):
    orig = FakeLogin.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.chain[failing] = False

    FakeLogin.__init__ = init
    try:
        with pytest.raises(s.SessionExpired):
            open_session(tmp_path)
    finally:
        FakeLogin.__init__ = orig
    assert FakeLogin.last.closed
    assert "login" not in FakeLogin.last.calls


def test_open_exception_is_transport_error(tmp_path):
    orig = FakeLogin.__init__

    def init(self, *a, **kw):
        orig(self, *a, **kw)
        self.raise_in = "exchange"

    FakeLogin.__init__ = init
    try:
        with pytest.raises(s.AmazonTransportError):
            open_session(tmp_path)
    finally:
        FakeLogin.__init__ = orig
    assert FakeLogin.last.closed


@pytest.mark.parametrize("exc, expected", [
    (Errors.AlexapyLoginError("401"), s.AmazonAuthError),
    (Errors.AlexapyConnectionError("x"), s.AmazonTransportError),
    (Errors.AlexapyTooManyRequestsError("x"), s.AmazonTransportError),
    (ClientError("x"), s.AmazonTransportError),
])
def test_devices_maps_alexapy_errors(tmp_path, exc, expected):
    sess = open_session(tmp_path)
    FakeAPI.error = exc
    with pytest.raises(expected):
        run(sess.devices())


def test_devices_empty_is_auth_error(tmp_path):
    sess = open_session(tmp_path)
    FakeAPI.result = None
    with pytest.raises(s.AmazonAuthError):
        run(sess.devices())


class FakeResp:
    def __init__(self, status=200, body=""):
        self.status, self._body = status, body

    async def text(self):
        return self._body


class FakePost:
    def __init__(self, resp=None, exc=None, log=None):
        self.resp, self.exc, self.log = resp, exc, log

    async def __aenter__(self):
        if self.exc:
            raise self.exc
        return self.resp

    async def __aexit__(self, *a):
        self.log.append("released")


def session_with_post(tmp_path, **kw):
    sess = open_session(tmp_path)
    log = []
    login = FakeLogin.last
    login._get_cookies_from_session = lambda: {}
    login._headers = {}
    login.url = "amazon.co.uk"
    login.session = types.SimpleNamespace(post=lambda *a, **k: FakePost(log=log, **kw))
    return sess, log


def test_run_releases_2xx_response(tmp_path):
    sess, log = session_with_post(tmp_path, resp=FakeResp(200))
    run(sess.run({"type": "x"}))
    assert log == ["released"]


def test_run_connect_failure_is_retryable(tmp_path):
    sess, _ = session_with_post(tmp_path, exc=ClientConnectorError("dns"))
    with pytest.raises(s.AmazonTransportError):
        run(sess.run({"type": "x"}))


@pytest.mark.parametrize("exc", [asyncio.TimeoutError(), ClientError("reset"), OSError("pipe")])
def test_run_failure_after_send_is_uncertain(tmp_path, exc):
    sess, _ = session_with_post(tmp_path, exc=exc)
    with pytest.raises(s.AmazonSendUncertain):
        run(sess.run({"type": "x"}))


def test_run_http_codes(tmp_path):
    sess, _ = session_with_post(tmp_path, resp=FakeResp(403))
    with pytest.raises(s.AmazonAuthError):
        run(sess.run({"type": "x"}))
    sess, _ = session_with_post(tmp_path, resp=FakeResp(500, "bad"))
    with pytest.raises(s.AmazonError):
        run(sess.run({"type": "x"}))
