"""Concurrency tests for meta_mcp.proxy.SubMcp.

A sub-MCP is one subprocess behind a single stdin/stdout pipe. Two coroutines
awaiting stdout.readline() at once collide ("readuntil() called while another
coroutine is already waiting for incoming data"), and even when they don't, one
consumes the other's response line. _send()/_notify() therefore hold a per-SubMcp
_io_lock so only one request/response round-trip is in flight at a time.

These tests exercise the real SubMcp against a tiny fake sub-MCP subprocess:
  - test_concurrent_calls_are_serialised: N concurrent calls each get their OWN
    correct, id-matched response. Passes with the lock.
  - test_without_lock_collides: with the lock defeated, the same load collides
    (documents WHY the lock is needed). Passes by observing the failure.

Run directly for a demo:  uv run python tests/test_proxy_concurrency.py
"""

import asyncio
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meta_mcp.proxy import SubMcp  # noqa: E402

# A minimal JSON-RPC-over-stdio echo server. Sleeps briefly before replying so
# concurrent client calls genuinely overlap in the read window.
_FAKE_SERVER = textwrap.dedent("""
    import sys, json, time
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mid = msg.get("id")
        if mid is None:          # a notification (e.g. notifications/initialized)
            continue
        method = msg.get("method")
        time.sleep(0.03)          # widen the overlap window for concurrent callers
        if method == "tools/call":
            result = {"echo": msg.get("params", {}).get("arguments", {})}
        elif method == "tools/list":
            result = {"tools": []}
        else:                     # initialize, etc.
            result = {"protocolVersion": "2024-11-05", "capabilities": {}}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\\n")
        sys.stdout.flush()
""")


def _new_sub() -> SubMcp:
    return SubMcp(name="fake", command=[sys.executable, "-c", _FAKE_SERVER])


class _NullLock:
    """A no-op async context manager — defeats serialisation, to show the race."""
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _check_serialised(n: int) -> None:
    sub = _new_sub()
    try:
        await sub.ensure_started()
        results = await asyncio.gather(
            *[sub.call_tool("echo", {"i": i}) for i in range(n)]
        )
        # Every call must get back its OWN argument, id-matched — no cross-talk.
        for i, r in enumerate(results):
            assert r["echo"]["i"] == i, f"call {i} got the wrong response: {r!r}"
    finally:
        await sub.shutdown()


async def _check_without_lock(n: int) -> bool:
    """Returns True if the unlocked version collided/hung (the expected failure)."""
    sub = _new_sub()
    try:
        await sub.ensure_started()
        sub._io_lock = _NullLock()  # defeat the fix
        try:
            await asyncio.wait_for(
                asyncio.gather(*[sub.call_tool("echo", {"i": i}) for i in range(n)]),
                timeout=5.0,
            )
        except (RuntimeError, asyncio.TimeoutError, AssertionError, KeyError):
            return True  # collided or hung, exactly as the docstring predicts
        return False
    finally:
        await sub.shutdown()


def test_concurrent_calls_are_serialised():
    asyncio.run(_check_serialised(25))


def test_without_lock_collides():
    assert asyncio.run(_check_without_lock(25)), (
        "expected concurrent unlocked stdio reads to collide, but they didn't — "
        "the test may no longer be exercising the race"
    )


if __name__ == "__main__":
    print("with _io_lock — 25 concurrent calls, each id-matched … ", end="", flush=True)
    asyncio.run(_check_serialised(25))
    print("PASS")
    print("without the lock — same load … ", end="", flush=True)
    collided = asyncio.run(_check_without_lock(25))
    print("collided/hung as expected (PASS)" if collided else "did NOT collide (unexpected)")
