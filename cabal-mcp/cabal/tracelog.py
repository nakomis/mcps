"""Per-call trace logging for cabal MCP calls.

Writes one log file per top-level call to `/Volumes/lru/cabal-mcp/` (override
with the `CABAL_TRACE_DIR` environment variable). Captures call lifecycle
events and every error with full traceback. Best-effort: if the log
directory cannot be opened the call still proceeds, with a one-line warning
on stderr.

A `ContextVar` holds the current log object so provider modules can find it
without threading it through every signature.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_DIR = "/Volumes/lru/cabal-mcp"

_current: ContextVar["TraceLog | _NoOpLog | None"] = ContextVar(
    "cabal_tracelog", default=None,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _format(msg: str, fields: dict[str, Any]) -> str:
    head = f"[{_now()}] {msg}"
    if not fields:
        return head + "\n"
    lines = [head]
    for k, v in fields.items():
        v_str = str(v)
        if "\n" in v_str or len(v_str) > 200:
            lines.append(f"  {k}:")
            for ln in v_str.splitlines() or [""]:
                lines.append(f"    {ln}")
        else:
            lines.append(f"  {k}={v_str}")
    return "\n".join(lines) + "\n"


class TraceLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"# cabal-mcp trace log\n# opened: {_now()}\n# path: {path}\n\n")

    def log(self, msg: str, **fields: Any) -> None:
        try:
            line = _format(msg, fields)
            with self._lock, open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            # Never let logging crash the call.
            pass

    def exception(self, msg: str, exc: BaseException, **fields: Any) -> None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self.log(msg, error=repr(exc), traceback=tb.rstrip(), **fields)


class _NoOpLog:
    """Returned when the trace dir cannot be opened. Silently swallows."""

    def log(self, *_: Any, **__: Any) -> None:
        pass

    def exception(self, *_: Any, **__: Any) -> None:
        pass


def start(call_kind: str, **fields: Any) -> "TraceLog | _NoOpLog":
    base_dir = Path(os.environ.get("CABAL_TRACE_DIR", DEFAULT_DIR))
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
    path = base_dir / f"{ts}-{call_kind}.log"
    try:
        log: TraceLog | _NoOpLog = TraceLog(path)
    except Exception as e:
        print(
            f"cabal: failed to open trace log {path}: {e!r}",
            file=sys.stderr,
        )
        log = _NoOpLog()
    _current.set(log)
    log.log("call.start", kind=call_kind, **fields)
    return log


def current() -> "TraceLog | _NoOpLog":
    log = _current.get()
    if log is None:
        return _NoOpLog()
    return log
