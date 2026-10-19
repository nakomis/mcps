"""Run a host's playbook wrapper and write its output to the run's log file.

The log is appended to line by line and flushed per line, so the API can
follow it live while the run is going.
"""

import asyncio
import contextlib
import os
import signal
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from midichlorian.hosts import HostSpec

HOMEBREW_BIN = Path("/opt/homebrew/bin")
_LINE_LIMIT = 16 * 1024 * 1024  # Ansible's -v results can make very long lines


class PlaybookRunner(Protocol):
    async def run(
        self, host: HostSpec, tags: list[str] | None, repo: Path, log: Path, env: dict
    ) -> int: ...


def build_env(
    base: Mapping[str, str] | None = None, homebrew: Path = HOMEBREW_BIN
) -> dict:
    """The environment for gates and playbook runs: the server's own (which carries
    GH_TOKEN and the AWS config) plus the non-interactive deploy switches."""
    env = dict(os.environ if base is None else base)
    env.update(
        DEPLOY_SKIP_CHECKOUT_CHECK="1", ANSIBLE_FORCE_COLOR="0", ANSIBLE_NOCOLOR="1"
    )
    path = env.get("PATH", "")
    if homebrew.is_dir() and str(homebrew) not in path.split(":"):
        env["PATH"] = f"{homebrew}:{path}" if path else str(homebrew)
    return env


def append_log(log: Path, text: str) -> None:
    """Append text to the log as whole lines."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.writelines(line + "\n" for line in text.splitlines())


def log_tail(log: Path, lines: int = 40) -> str:
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            return "\n".join(line.rstrip("\n") for line in deque(f, maxlen=lines))
    except FileNotFoundError:
        return ""


class AnsibleRunner:
    """Runs `scripts/<host>-ansible.sh [--tags a,b]` from the given checkout."""

    def __init__(self, kill_grace: float = 10.0):
        self.kill_grace = kill_grace  # seconds between SIGTERM and SIGKILL on cancel

    async def run(
        self, host: HostSpec, tags: list[str] | None, repo: Path, log: Path, env: dict
    ) -> int:
        argv = [str(repo / host.wrapper)]
        if tags:
            argv += ["--tags", ",".join(tags)]
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "ab") as out:  # noqa: ASYNC230 — local appends, flushed per line
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=repo,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    limit=_LINE_LIMIT,
                    start_new_session=True,  # its own process group, so cancel reaches ansible's children
                )
            except OSError as exc:
                out.write(f"[midichlorian] could not start {argv[0]}: {exc}\n".encode())
                return 127
            try:
                while True:
                    try:
                        line = await proc.stdout.readline()
                    except ValueError:  # a line beyond _LINE_LIMIT; it is dropped
                        out.write(b"[midichlorian] over-long output line dropped\n")
                        out.flush()
                        continue
                    if not line:
                        break
                    out.write(line if line.endswith(b"\n") else line + b"\n")
                    out.flush()
                return await proc.wait()
            except asyncio.CancelledError:
                await self._kill_group(proc)
                raise

    async def _kill_group(self, proc) -> None:
        """SIGTERM the wrapper's process group, SIGKILL whatever is left after the grace."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(proc.wait()), self.kill_grace)
        with contextlib.suppress(ProcessLookupError):  # stragglers that ignored SIGTERM
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
