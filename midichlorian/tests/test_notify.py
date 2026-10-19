import json
import logging
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from midichlorian import notify

TOKEN = "s3cr3t-token-value"


@pytest.fixture
def sock():
    """A listening Unix socket; `.received` fills with the bytes of one connection."""
    d = tempfile.mkdtemp(dir="/tmp")  # short: AF_UNIX paths are length-limited
    path = str(Path(d) / "m.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    srv.settimeout(3)

    class S:
        pass

    s = S()
    s.path, s.received = path, []

    def serve():
        try:
            conn, _ = srv.accept()
        except OSError:  # closed (or timed out) with nobody having connected
            return
        with conn:
            buf = b""
            while chunk := conn.recv(4096):
                buf += chunk
            s.received.append(buf)

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    s.join = t.join
    yield s
    srv.close()
    shutil.rmtree(d, ignore_errors=True)


def test_deliver_writes_the_two_exact_lines(sock):
    assert notify.deliver("hello", sock.path, TOKEN) is True
    sock.join(2)
    raw = sock.received[0]
    assert raw == (
        b'{"type":"auth","token":"' + TOKEN.encode() + b'"}\n'
        b'{"type":"user","message":{"role":"user","content":"hello"}}\n'
    )


def test_content_with_quotes_and_newlines_round_trips(sock):
    text = 'a "quoted" line\nsecond\\line\tand ünïcode'
    assert notify.deliver(text, sock.path, TOKEN) is True
    sock.join(2)
    lines = sock.received[0].decode().split("\n")
    assert lines[2] == "" and len(lines) == 3  # exactly two newline-terminated lines
    assert json.loads(lines[1])["message"]["content"] == text


def test_reads_env_by_default(sock, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", sock.path)
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", TOKEN)
    assert notify.deliver("x") is True
    sock.join(2)
    assert TOKEN.encode() in sock.received[0]


def test_missing_env_returns_false(monkeypatch, caplog):
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_TOKEN", raising=False)
    assert notify.deliver("x") is False
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/tmp/nonexistent.sock")
    assert notify.deliver("x") is False  # token missing


def test_connect_failure_returns_false_and_never_logs_the_token(caplog):
    caplog.set_level(logging.DEBUG)
    assert notify.deliver("secret body", "/tmp/definitely-not-here.sock", TOKEN) is False
    assert TOKEN not in caplog.text
    assert "secret body" not in caplog.text
    assert caplog.records  # the failure was logged


def test_never_raises_on_odd_failures(tmp_path):
    assert notify.deliver("x", str(tmp_path), TOKEN) is False  # a directory, not a socket
    assert notify.deliver("x", "/tmp/" + "a" * 300, TOKEN) is False  # path too long
