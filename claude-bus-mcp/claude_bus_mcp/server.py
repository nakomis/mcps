#!/usr/bin/env python3
"""Claude-Bus MCP — cross-Claude coordination over NATS JetStream.

Provides distributed leases on named real-world resources (e.g. leia-apply,
luke-apply), pub/sub messaging between sessions, and blocking ask/reply.

State stores (provisioned by claude-bus/init-streams.sh):
  - KV bucket `bus_leases`     — keys are resource names, per-key TTL
  - KV bucket `bus_sessions`   — keys are session_ids, TTL refreshed on heartbeat
  - Stream  `BUS_MSGS`         — subjects bus.tell.*, bus.broadcast, bus.ask.*, bus.reply.*
  - Stream  `BUS_AUDIT`        — subject  bus.audit.>

Push: when a session announces itself, this MCP subscribes to its inbox
subjects and appends one JSON line per incoming message to
/tmp/claude-bus-<session_id>.log. The Claude session is expected to be
`Monitor`ing that file (with `stdbuf -oL tail -F -n0 <path>`).
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import nats
from mcp.server.fastmcp import FastMCP
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.errors import KeyWrongLastSequenceError, NoKeysError

mcp = FastMCP("claude-bus-mcp")


# ── Config ────────────────────────────────────────────────────────────────────

NATS_SERVERS = os.environ.get(
    "CLAUDE_BUS_NATS_SERVERS",
    "nats://luke.local:4222,nats://leia.local:4222,nats://phi.local:4222",
).split(",")

INBOX_DIR = Path(os.environ.get("CLAUDE_BUS_INBOX_DIR", "/tmp"))
SESSION_TTL_SECONDS = int(os.environ.get("CLAUDE_BUS_SESSION_TTL", "600"))


# ── Connection + state (lazy) ─────────────────────────────────────────────────

_nc: nats.NATS | None = None
_js = None  # JetStream context
_kv_leases = None
_kv_sessions = None

# session_id → list of active subscriptions for that session's inboxes
_inbox_subs: dict[str, list] = {}


async def _connect():
    """Lazy NATS connect + KV handles. Idempotent — safe to call repeatedly."""
    global _nc, _js, _kv_leases, _kv_sessions
    if _nc and _nc.is_connected:
        return
    _nc = await nats.connect(
        servers=NATS_SERVERS,
        name="claude-bus-mcp",
        max_reconnect_attempts=-1,
        reconnect_time_wait=2,
    )
    _js = _nc.jetstream()
    _kv_leases = await _js.key_value("bus_leases")
    _kv_sessions = await _js.key_value("bus_sessions")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lease_id() -> str:
    return secrets.token_hex(8)


def _inbox_path(session_id: str) -> Path:
    safe = session_id.replace("/", "_")
    return INBOX_DIR / f"claude-bus-{safe}.log"


def _append_inbox(session_id: str, event: dict) -> None:
    """Append one JSON event line to the session's inbox log."""
    path = _inbox_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")
        f.flush()


async def _audit(subject: str, payload: dict) -> None:
    """Best-effort write to BUS_AUDIT. Never raises."""
    try:
        await _js.publish(f"bus.audit.{subject}", json.dumps(payload).encode())
    except Exception:
        pass


# ── Inbox subscriptions ───────────────────────────────────────────────────────


async def _start_inbox(session_id: str) -> None:
    """Subscribe this MCP to the session's inbox subjects and write events to disk."""
    if session_id in _inbox_subs:
        return  # already wired up
    subs = []

    async def on_tell(msg):
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            data = {"raw": msg.data.decode(errors="replace")}
        _append_inbox(
            session_id,
            {
                "kind": "tell",
                "ts": _now_iso(),
                "from": data.get("from"),
                "text": data.get("text", ""),
            },
        )

    async def on_broadcast(msg):
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            data = {"raw": msg.data.decode(errors="replace")}
        _append_inbox(
            session_id,
            {
                "kind": "broadcast",
                "ts": _now_iso(),
                "from": data.get("from"),
                "text": data.get("text", ""),
            },
        )

    async def on_ask(msg):
        try:
            data = json.loads(msg.data.decode())
        except Exception:
            data = {"raw": msg.data.decode(errors="replace")}
        _append_inbox(
            session_id,
            {
                "kind": "ask",
                "ts": _now_iso(),
                "from": data.get("from"),
                "text": data.get("text", ""),
                "ask_id": msg.reply,  # caller will pass this to reply()
            },
        )

    subs.append(await _nc.subscribe(f"bus.tell.{session_id}", cb=on_tell))
    subs.append(await _nc.subscribe(f"bus.ask.{session_id}", cb=on_ask))
    subs.append(await _nc.subscribe("bus.broadcast", cb=on_broadcast))
    _inbox_subs[session_id] = subs


async def _stop_inbox(session_id: str) -> None:
    for sub in _inbox_subs.pop(session_id, []):
        try:
            await sub.unsubscribe()
        except Exception:
            pass


# ── Tools: session lifecycle ──────────────────────────────────────────────────


@mcp.tool()
async def announce(session_id: str, subject: str, worktree: str, branch: str) -> dict:
    """Register this session with the bus and start its inbox watcher.

    The MCP appends incoming events to /tmp/claude-bus-<session_id>.log,
    which the caller should be `Monitor`ing with:
        stdbuf -oL tail -F -n0 /tmp/claude-bus-<session_id>.log
    """
    await _connect()
    record = {
        "session_id": session_id,
        "subject": subject,
        "worktree": worktree,
        "branch": branch,
        "state": "idle",
        "intent": "",
        "announced_at": _now_iso(),
        "heartbeat_at": _now_iso(),
    }
    # Use create+update with msg_ttl so session presence auto-expires.
    payload = json.dumps(record).encode()
    try:
        await _kv_sessions.create(session_id, payload, msg_ttl=SESSION_TTL_SECONDS)
    except Exception:
        # Already exists — refresh via update.
        entry = await _kv_sessions.get(session_id)
        await _kv_sessions.update(
            session_id, payload, last=entry.revision, msg_ttl=SESSION_TTL_SECONDS
        )
    await _start_inbox(session_id)
    await _audit("session.announce", record)
    return {
        "ok": True,
        "inbox": str(_inbox_path(session_id)),
        "watch_cmd": f"stdbuf -oL tail -F -n0 {_inbox_path(session_id)}",
    }


@mcp.tool()
async def heartbeat(session_id: str, state: str = "idle", intent: str = "") -> dict:
    """Refresh the session's presence TTL and update state/intent."""
    await _connect()
    try:
        entry = await _kv_sessions.get(session_id)
        record = json.loads(entry.value.decode())
    except Exception:
        record = {"session_id": session_id}
    record.update({"state": state, "intent": intent, "heartbeat_at": _now_iso()})
    payload = json.dumps(record).encode()
    try:
        entry = await _kv_sessions.get(session_id)
        await _kv_sessions.update(
            session_id, payload, last=entry.revision, msg_ttl=SESSION_TTL_SECONDS
        )
    except Exception:
        # Session entry vanished (TTL'd out); recreate.
        await _kv_sessions.create(session_id, payload, msg_ttl=SESSION_TTL_SECONDS)
    return {"ok": True, "ttl_seconds": SESSION_TTL_SECONDS}


@mcp.tool()
async def list_sessions() -> list[dict]:
    """List every currently-announced session (TTL'd; dead sessions disappear)."""
    await _connect()
    out = []
    try:
        keys = await _kv_sessions.keys()
    except NoKeysError:
        return []
    for key in keys:
        try:
            entry = await _kv_sessions.get(key)
            out.append(json.loads(entry.value.decode()))
        except Exception:
            continue
    return out


# ── Tools: leases ─────────────────────────────────────────────────────────────


@mcp.tool()
async def acquire(
    resource: str,
    ttl_seconds: int = 1800,
    intent: str = "",
    holder_session: str = "",
) -> dict:
    """Try to take an exclusive lease on a named resource.

    Returns {"ok": True, "lease_id": "...", "expires_at": "..."} on success.
    Returns {"ok": False, "held_by": "...", "expires_at": "..."} if held.
    """
    await _connect()
    lease_id = _lease_id()
    record = {
        "lease_id": lease_id,
        "resource": resource,
        "holder_session": holder_session,
        "intent": intent,
        "acquired_at": _now_iso(),
        "expires_at": datetime.fromtimestamp(
            time.time() + ttl_seconds, tz=timezone.utc
        ).isoformat(timespec="seconds"),
    }
    try:
        await _kv_leases.create(
            resource, json.dumps(record).encode(), msg_ttl=ttl_seconds
        )
        await _audit("lease.acquire", record)
        return {
            "ok": True,
            "lease_id": lease_id,
            "expires_at": record["expires_at"],
        }
    except Exception as e:
        # Contention: someone holds it. Return who.
        try:
            entry = await _kv_leases.get(resource)
            current = json.loads(entry.value.decode())
            return {
                "ok": False,
                "held_by": current.get("holder_session", "unknown"),
                "intent": current.get("intent", ""),
                "expires_at": current.get("expires_at", ""),
                "error": type(e).__name__,
            }
        except Exception:
            return {"ok": False, "error": f"acquire failed: {e}"}


@mcp.tool()
async def renew(resource: str, lease_id: str, ttl_seconds: int = 1800) -> dict:
    """Extend the TTL on a lease the caller still holds. Fails otherwise."""
    await _connect()
    try:
        entry = await _kv_leases.get(resource)
        current = json.loads(entry.value.decode())
    except Exception:
        return {"ok": False, "error": "no such lease (expired or never held)"}
    if current.get("lease_id") != lease_id:
        return {"ok": False, "error": "not the lease holder"}
    current["expires_at"] = datetime.fromtimestamp(
        time.time() + ttl_seconds, tz=timezone.utc
    ).isoformat(timespec="seconds")
    try:
        await _kv_leases.update(
            resource,
            json.dumps(current).encode(),
            last=entry.revision,
            msg_ttl=ttl_seconds,
        )
        await _audit("lease.renew", current)
        return {"ok": True, "expires_at": current["expires_at"]}
    except KeyWrongLastSequenceError:
        return {"ok": False, "error": "concurrent update; retry"}


@mcp.tool()
async def release(resource: str, lease_id: str) -> dict:
    """Release a lease the caller holds. No-op if already expired."""
    await _connect()
    try:
        entry = await _kv_leases.get(resource)
        current = json.loads(entry.value.decode())
    except Exception:
        return {"ok": True, "note": "lease was already gone"}
    if current.get("lease_id") != lease_id:
        return {"ok": False, "error": "not the lease holder"}
    try:
        await _kv_leases.delete(resource, last=entry.revision)
        await _audit("lease.release", current)
        return {"ok": True}
    except KeyWrongLastSequenceError:
        return {"ok": False, "error": "concurrent update; retry"}


@mcp.tool()
async def list_resources() -> list[dict]:
    """List every active lease in the bus_leases KV bucket."""
    await _connect()
    out = []
    try:
        keys = await _kv_leases.keys()
    except NoKeysError:
        return []
    for key in keys:
        try:
            entry = await _kv_leases.get(key)
            out.append(json.loads(entry.value.decode()))
        except Exception:
            continue
    return out


@mcp.tool()
async def whoholds(resource: str) -> dict:
    """Return the current lease record for one resource, or empty if free."""
    await _connect()
    try:
        entry = await _kv_leases.get(resource)
        return json.loads(entry.value.decode())
    except Exception:
        return {}


# ── Tools: messaging ──────────────────────────────────────────────────────────


@mcp.tool()
async def tell(to_session: str, text: str, from_session: str = "") -> dict:
    """Send a fire-and-forget message to another session."""
    await _connect()
    payload = {"from": from_session, "text": text, "ts": _now_iso()}
    await _js.publish(f"bus.tell.{to_session}", json.dumps(payload).encode())
    await _audit("msg.tell", {"to": to_session, **payload})
    return {"ok": True}


@mcp.tool()
async def broadcast(text: str, from_session: str = "") -> dict:
    """Publish a broadcast every announced session will receive."""
    await _connect()
    payload = {"from": from_session, "text": text, "ts": _now_iso()}
    await _js.publish("bus.broadcast", json.dumps(payload).encode())
    await _audit("msg.broadcast", payload)
    return {"ok": True}


@mcp.tool()
async def ask(
    to_session: str,
    text: str,
    timeout_seconds: int = 60,
    from_session: str = "",
) -> dict:
    """Send a question and block until the recipient replies or we time out.

    Uses NATS request/reply: the recipient's MCP sees the message on
    bus.ask.<to_session> and writes it to their inbox log with `ask_id`
    set to the auto-generated reply subject. They call `reply(ask_id, text)`
    which publishes back to that subject and unblocks this call.
    """
    await _connect()
    payload = {"from": from_session, "text": text, "ts": _now_iso()}
    try:
        msg = await _nc.request(
            f"bus.ask.{to_session}",
            json.dumps(payload).encode(),
            timeout=float(timeout_seconds),
        )
        try:
            reply_payload = json.loads(msg.data.decode())
        except Exception:
            reply_payload = {"text": msg.data.decode(errors="replace")}
        await _audit(
            "msg.ask.replied",
            {"to": to_session, **payload, "reply": reply_payload},
        )
        return {"ok": True, "reply": reply_payload.get("text", "")}
    except NatsTimeoutError:
        await _audit("msg.ask.timeout", {"to": to_session, **payload})
        return {"ok": False, "error": "timeout"}


@mcp.tool()
async def reply(ask_id: str, text: str, from_session: str = "") -> dict:
    """Satisfy a pending ask. `ask_id` is the subject from the inbox event."""
    await _connect()
    payload = {"from": from_session, "text": text, "ts": _now_iso()}
    await _nc.publish(ask_id, json.dumps(payload).encode())
    await _audit("msg.reply", {"ask_id": ask_id, **payload})
    return {"ok": True}


@mcp.tool()
async def inbox(session_id: str, max_lines: int = 100) -> list[dict]:
    """Read up to the last `max_lines` events from the session's inbox log.

    Most of the time callers should use `Monitor` for push delivery; this
    is a convenience for catching up after a reconnect.
    """
    path = _inbox_path(session_id)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        lines = f.readlines()[-max_lines:]
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"raw": line})
    return out


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
