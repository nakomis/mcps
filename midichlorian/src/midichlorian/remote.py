"""Run local and remote commands without ever raising for a non-zero exit."""

import asyncio
import contextlib
from dataclasses import dataclass

from midichlorian.hosts import HostSpec


@dataclass(frozen=True)
class RunResult:
    rc: int
    stdout: str
    stderr: str


async def run(
    argv: list[str],
    timeout: float = 60,
    env: dict | None = None,
    stdin: bytes | None = None,
) -> RunResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        return RunResult(127, "", str(exc))
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return RunResult(124, "", "timeout")
    return RunResult(
        proc.returncode if proc.returncode is not None else 1,
        out.decode(errors="replace"),
        err.decode(errors="replace"),
    )


async def ssh(
    host: HostSpec, command: str, timeout: float = 60, stdin: bytes | None = None
) -> RunResult:
    return await run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=30",
            f"nakomis@{host.ssh}",
            command,
        ],
        timeout=timeout,
        stdin=stdin,
    )
