"""Large-I/O tests for meta_mcp.proxy.SubMcp and meta_mcp.server.

A sub-MCP replies with one JSON-RPC message per line on stdout. asyncio's
default StreamReader limit is 64 KiB, so a single reply bigger than that made
readline() raise "Separator is not found, and chunk exceed the limit" (seen
with cadcraft's ~126 KB list_commands).

Separately, stderr was a PIPE that nothing read. Once a chatty sub-MCP wrote
~64 KB of logs the OS pipe filled, the sub-MCP blocked on its next write, and
the call waiting on its stdout hung forever.

And a result that does fit should still not be dumped whole into the caller's
context: server._spill_if_large writes it to a file and returns a preview.
"""

import asyncio
import json
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meta_mcp import proxy, server  # noqa: E402
from meta_mcp.proxy import SubMcp  # noqa: E402

# tools/call arguments: {"reply_bytes": N} pads the reply to ~N bytes on one
# line; {"stderr_bytes": N} writes N bytes of log lines to stderr first;
# {"die": MSG} writes MSG to stderr and exits without replying.
_FAKE_SERVER = textwrap.dedent("""
    import sys, json
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid = msg.get("id")
        if mid is None:
            continue
        args = msg.get("params", {}).get("arguments", {})
        if msg.get("method") == "tools/call":
            if "die" in args:
                sys.stderr.write(args["die"] + "\\n")
                sys.stderr.flush()
                sys.exit(1)
            n = args.get("stderr_bytes", 0)
            while n > 0:
                sys.stderr.write("x" * 99 + "\\n")
                n -= 100
            sys.stderr.flush()
            result = {"padding": "y" * args.get("reply_bytes", 0)}
        else:
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "tools": []}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\\n")
        sys.stdout.flush()
""")


def _new_sub() -> SubMcp:
    return SubMcp(name="fake", command=[sys.executable, "-c", _FAKE_SERVER])


async def _call(args: dict) -> dict:
    sub = _new_sub()
    try:
        return await asyncio.wait_for(sub.call_tool("t", args), timeout=10.0)
    finally:
        await sub.shutdown()


def test_reply_larger_than_64k():
    result = asyncio.run(_call({"reply_bytes": 200_000}))
    assert len(result["padding"]) == 200_000


def test_stderr_flood_does_not_hang():
    # Well past any OS pipe buffer (64 KiB on macOS and Linux).
    result = asyncio.run(_call({"stderr_bytes": 1_000_000, "reply_bytes": 10}))
    assert result["padding"] == "y" * 10


async def _crash_then_call() -> tuple[str, dict]:
    sub = _new_sub()
    try:
        try:
            await sub.call_tool("t", {"die": "boom: config missing"})
            raise AssertionError("expected the crash to raise")
        except RuntimeError as exc:
            error = str(exc)
        # The dead process is respawned on the next call.
        result = await asyncio.wait_for(sub.call_tool("t", {"reply_bytes": 3}), timeout=10.0)
        return error, result
    finally:
        await sub.shutdown()


def test_crash_quotes_stderr_and_respawns():
    error, result = asyncio.run(_crash_then_call())
    assert "closed stdout unexpectedly" in error
    assert "boom: config missing" in error
    assert result["padding"] == "yyy"


async def _over_limit_then_call() -> tuple[str, dict]:
    sub = _new_sub()
    try:
        try:
            await sub.call_tool("t", {"reply_bytes": 5000})
            raise AssertionError("expected the over-limit reply to raise")
        except RuntimeError as exc:
            error = str(exc)
        result = await asyncio.wait_for(sub.call_tool("t", {"reply_bytes": 3}), timeout=10.0)
        return error, result
    finally:
        await sub.shutdown()


def test_reply_over_limit_errors_clearly_and_recovers(monkeypatch):
    monkeypatch.setattr(proxy, "_STREAM_LIMIT", 1000)
    error, result = asyncio.run(_over_limit_then_call())
    assert "longer than" in error
    assert result["padding"] == "yyy"


def test_spill_failure_falls_back_to_truncated_inline(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(server, "SPILL_DIR", blocker / "sub")
    text = "q" * (server.INLINE_LIMIT * 3)
    out = server._spill_if_large(text, "fake", "t")
    assert len(out) < server.INLINE_LIMIT + 1000
    assert "could not be written" in out
    assert out.endswith("q" * 100)


def test_small_result_returned_inline(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SPILL_DIR", tmp_path)
    assert server._spill_if_large("hello", "fake", "t") == "hello"
    assert list(tmp_path.iterdir()) == []


def test_large_result_spilled_to_file(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SPILL_DIR", tmp_path)
    text = json.dumps({"padding": "z" * (server.INLINE_LIMIT * 3)})
    out = server._spill_if_large(text, "fake", "t")

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].suffix == ".json"
    assert files[0].stat().st_mode & 0o777 == 0o600
    assert files[0].read_text() == text
    assert str(files[0]) in out
    assert len(out) < server.INLINE_LIMIT
    assert text[: server.PREVIEW_CHARS] in out


def test_prune_removes_only_old_spill_files(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "SPILL_DIR", tmp_path)
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text("{}")
    new.write_text("{}")
    ancient = old.stat().st_mtime - (server.SPILL_MAX_AGE_DAYS + 1) * 86400
    os.utime(old, (ancient, ancient))
    server._prune_spill_dir()
    assert not old.exists()
    assert new.exists()
