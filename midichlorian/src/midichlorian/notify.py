"""Delivery of a message into the requesting Claude Code session.

Claude Code listens on a Unix socket (`CLAUDE_CODE_MESSAGING_SOCKET`) and takes two
newline-terminated JSON lines: an auth line carrying `CLAUDE_CODE_MESSAGING_TOKEN`,
then a user message. The format is undocumented Claude Code internals (v2.1.224+), so
it lives here alone: if it changes, swap `deliver` for an MCP channel notification.
"""

import json
import logging
import os
import socket

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0


def deliver(text: str, socket_path: str | None = None, token: str | None = None) -> bool:
    """Send `text` to the session. True if written; False (logged, never raised) otherwise.

    Failures are logged by type only: neither the token nor the message goes in a log.
    """
    socket_path = socket_path or os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    token = token or os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN")
    if not socket_path or not token:
        logger.warning("no messaging socket or token in the environment; message not delivered")
        return False
    try:
        payload = (
            json.dumps({"type": "auth", "token": token}, separators=(",", ":")) + "\n"
            + json.dumps(
                {"type": "user", "message": {"role": "user", "content": text}},
                separators=(",", ":"),
                ensure_ascii=False,
            ) + "\n"
        ).encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(CONNECT_TIMEOUT)
            sock.connect(socket_path)
            sock.sendall(payload)
        return True
    except Exception as exc:  # noqa: BLE001 — a lost session must never break a follower
        logger.warning("could not deliver a message to the session (%s)", type(exc).__name__)
        return False
