"""Run a host's playbook wrapper and write its output to the run's log file.

The wrapper writes straight into the log (no pipe through this process), so the
API can follow it live, and so a playbook outlives the server (R27): it runs in
its own session, nothing here ever signals it, and a server shutdown that cancels
the run leaves it to finish and keep logging. While it runs, its pid is in
`<checkout>.pid` beside its checkout, so a restarted server leaves that checkout
alone until the process has gone.
"""

import asyncio
import contextlib
import os
import subprocess
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from midichlorian.hosts import HostSpec

HOMEBREW_BIN = Path("/opt/homebrew/bin")
WAIT_POLL_SECONDS = 0.5


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
        DEPLOY_SKIP_CHECKOUT_CHECK="1",
        ANSIBLE_FORCE_COLOR="0",
        ANSIBLE_NOCOLOR="1",
        PYTHONUNBUFFERED="1",  # output goes to a file: keep the live log live
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


def pid_file(checkout: Path) -> Path:
    """Where a run's pid is kept while it runs: beside its checkout."""
    return checkout.parent / f"{checkout.name}.pid"


def read_pid(path: Path) -> int | None:
    """The pid recorded in `path`, or None if there is no usable one."""
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _command_of(pid: int) -> str | None:
    """The command line of `pid` ("" if there is no such process), or None if it
    cannot be asked (no `ps`)."""
    try:
        done = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else ""


def pid_alive(path: Path) -> bool:
    """Whether the pid recorded in `path` is a live playbook process.

    A pid the system has since handed to something else (a reboot, a fresh
    container) is not: the command line must still mention ansible (the wrapper
    scripts are `<host>-ansible.sh`, which run ansible-playbook). If the command
    line cannot be read at all, a live pid is trusted.
    """
    pid = read_pid(path)
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass  # another user's process: exists, so let the command line decide
    except OSError:
        return False
    command = _command_of(pid)
    return command is None or "ansible" in command.lower()


class AnsibleRunner:
    """Runs `scripts/<host>-ansible.sh [--tags a,b]` from the given checkout."""

    async def run(
        self, host: HostSpec, tags: list[str] | None, repo: Path, log: Path, env: dict
    ) -> int:
        argv = [str(repo / host.wrapper)]
        if tags:
            argv += ["--tags", ",".join(tags)]
        log.parent.mkdir(parents=True, exist_ok=True)
        pids = pid_file(repo)
        with open(log, "ab") as out:  # noqa: ASYNC230 — a local append, opened once
            try:
                # Popen, not asyncio's subprocess: asyncio kills a child whose transport
                # is collected at shutdown, and this child must outlive the server.
                proc = subprocess.Popen(  # noqa: ASYNC220 — fork/exec is quick; nothing to await
                    argv,
                    cwd=repo,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # no signal aimed at the server reaches it
                )
            except OSError as exc:
                out.write(f"[midichlorian] could not start {argv[0]}: {exc}\n".encode())
                return 127
        pids.write_text(f"{proc.pid}\n")
        # A CancelledError (shutdown) leaves here without touching the process or its
        # pid file: the playbook finishes on its own.
        while (rc := proc.poll()) is None:
            await asyncio.sleep(WAIT_POLL_SECONDS)
        with contextlib.suppress(FileNotFoundError):
            pids.unlink()
        _end_with_newline(log)
        return rc


def _end_with_newline(log: Path) -> None:
    with open(log, "rb+") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            return
        f.seek(-1, os.SEEK_END)
        if f.read(1) != b"\n":
            f.write(b"\n")
