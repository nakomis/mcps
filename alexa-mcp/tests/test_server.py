import io
import json

import pytest
from botocore.exceptions import ClientError, UnauthorizedSSOTokenError

from alexa_mcp import server


class FakeLambda:
    def __init__(self, result=None, function_error=None, raises=None):
        self.result, self.function_error, self.raises = result, function_error, raises
        self.calls = []

    def invoke(self, **kwargs):
        if self.raises:
            raise self.raises
        self.calls.append(kwargs)
        resp = {"Payload": io.BytesIO(json.dumps(self.result).encode())}
        if self.function_error:
            resp["FunctionError"] = self.function_error
        return resp


OK = {"ok": True, "action": "announce", "devices": ["Bedroom Echo", "Martin's Echo Show"]}


def test_default_devices_parsing():
    assert server.default_devices(" A , B,,A, ") == ["A", "B"]
    assert server.default_devices(None) == []


def test_uses_default_devices_when_none_given(monkeypatch):
    monkeypatch.setenv("ALEXA_DEFAULT_DEVICES", "Bedroom Echo, Martin's Echo Show")
    fake = FakeLambda(OK)
    out = server.send("announce", "Dinner", None, client=fake)
    payload = json.loads(fake.calls[0]["Payload"])
    assert payload == {"action": "announce", "text": "Dinner",
                       "devices": ["Bedroom Echo", "Martin's Echo Show"]}
    assert fake.calls[0]["FunctionName"] == server.FUNCTION
    assert "Bedroom Echo" in out and "Martin's Echo Show" in out


def test_explicit_devices_override_default(monkeypatch):
    monkeypatch.setenv("ALEXA_DEFAULT_DEVICES", "Bedroom Echo")
    fake = FakeLambda(OK)
    server.send("speak", "Hi", ["Shed Echo"], client=fake)
    assert json.loads(fake.calls[0]["Payload"])["devices"] == ["Shed Echo"]


def test_no_devices_anywhere_is_an_error(monkeypatch):
    monkeypatch.delenv("ALEXA_DEFAULT_DEVICES", raising=False)
    with pytest.raises(server.AlexaError, match="ALEXA_DEFAULT_DEVICES"):
        server.send("announce", "Hi", None, client=FakeLambda(OK))


def test_session_expired_tells_user_to_run_login():
    fake = FakeLambda({"ok": False, "error": "session_expired", "detail": "rejected"})
    with pytest.raises(server.AlexaError, match="alexa-mcp-login"):
        server.send("announce", "Hi", ["X"], client=fake)


def test_unknown_devices_lists_valid_names():
    fake = FakeLambda({"ok": False, "error": "unknown_devices",
                       "detail": {"unknown": ["Kitchen"], "valid": ["Bedroom Echo"]}})
    with pytest.raises(server.AlexaError) as e:
        server.send("announce", "Hi", ["Kitchen"], client=fake)
    assert "Kitchen" in str(e.value) and "Bedroom Echo" in str(e.value)
    assert "Nothing was sent" in str(e.value)


def test_function_error_is_surfaced():
    fake = FakeLambda({"errorMessage": "boom"}, function_error="Unhandled")
    with pytest.raises(server.AlexaError, match="boom"):
        server.send("announce", "Hi", ["X"], client=fake)


@pytest.mark.parametrize("exc", [
    UnauthorizedSSOTokenError(),
    ClientError({"Error": {"Code": "ExpiredTokenException"}}, "Invoke"),
])
def test_expired_sso_says_aws_sso_login(exc):
    with pytest.raises(server.AlexaError, match="aws sso login"):
        server.send("announce", "Hi", ["X"], client=FakeLambda(raises=exc))
