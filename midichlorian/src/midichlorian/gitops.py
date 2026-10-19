"""Async git helpers for the runner clone of nakomis/home-infra."""

import asyncio
import contextlib
import os
import shutil
import signal
from pathlib import Path

# A stalled fetch or clone must not hold the clone's lock (and so every submit and
# job) for ever: git has no default low-speed limit.
GIT_TIMEOUT = 120.0
# Never prompt for credentials (there is nobody to answer), and keep stderr in
# English, since callers match on it.
_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


class GitError(Exception):
    """A git command failed; the message is its stderr."""


async def _run(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **_GIT_ENV},
        start_new_session=True,  # so a timeout can kill git's helpers too
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), GIT_TIMEOUT)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        name = args[2] if args[:1] == ("-C",) and len(args) > 2 else args[0]
        raise GitError(f"git {name}: timeout after {GIT_TIMEOUT:g} s") from None
    return (
        proc.returncode,
        out.decode(errors="replace").strip(),
        err.decode(errors="replace").strip(),
    )


async def _git(repo: Path, *args: str) -> str:
    rc, out, err = await _run("-C", str(repo), *args)
    if rc != 0:
        raise GitError(err or f"git {args[0]} exited {rc}")
    return out


def _check_rev(rev: str) -> None:
    if rev.startswith("-"):
        raise GitError(f"invalid revision: {rev!r}")


async def fetch(repo: Path) -> None:
    """Update origin/main (and prune) from the remote."""
    await _git(repo, "fetch", "--prune", "origin")


async def ensure_clone(repo: Path, url: str) -> None:
    """Clone url into repo unless it is already a git repository."""
    if (repo / ".git").exists():
        return
    repo.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = await _run("clone", "--", url, str(repo))
    if rc != 0:
        raise GitError(err or f"git clone exited {rc}")


async def is_ancestor(repo: Path, a: str, b: str) -> bool:
    """True if a is an ancestor of (or equal to) b. Unknown objects give False."""
    _check_rev(a)
    _check_rev(b)
    rc, _, err = await _run("-C", str(repo), "merge-base", "--is-ancestor", a, b)
    if rc == 0:
        return True
    if rc == 1:
        return False
    if rc == 128 and "not a valid" in err.lower():
        return False
    raise GitError(err or f"git merge-base exited {rc}")


async def is_on_main(repo: Path, sha: str) -> bool:
    return await is_ancestor(repo, sha, "origin/main")


async def resolve(repo: Path, ref: str) -> str:
    """Full sha of a commit-ish (short shas and refs both work)."""
    _check_rev(ref)
    return await _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")


async def main_head(repo: Path) -> str:
    return await resolve(repo, "origin/main")


async def checkout(repo: Path, sha: str) -> None:
    """Hard-reset the worktree to sha, removing untracked files except .venv."""
    _check_rev(sha)
    await _git(repo, "reset", "--hard", sha)
    await _git(repo, "clean", "-fdx", "-e", ".venv")


async def add_worktree(repo: Path, path: Path, sha: str) -> None:
    """Check sha out, detached, in a fresh worktree at path (replacing any stale one).

    Each run gets its own worktree, so concurrent runs never share a checkout and
    the clone itself is only fetched into, never reset.
    """
    _check_rev(sha)
    await remove_worktree(repo, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    await _git(repo, "worktree", "add", "--detach", "--force", str(path), sha)


async def remove_worktree(repo: Path, path: Path) -> None:
    """Remove the worktree at path, dirty or not; quiet if it is already gone."""
    if path.exists():
        await _run(
            "-C", str(repo), "worktree", "remove", "--force", "--force", str(path)
        )
        shutil.rmtree(path, ignore_errors=True)
    await _git(repo, "worktree", "prune")
