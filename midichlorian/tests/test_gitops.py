import os
import subprocess
from pathlib import Path

import pytest

from midichlorian import gitops
from midichlorian.gitops import GitError

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, env=_ENV, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def world(tmp_path):
    """Bare origin, a seed clone that pushes to it, and the runner clone."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    git(seed, "checkout", "-b", "main")
    a = commit(seed, "a")
    git(seed, "push", "origin", "main")
    git(seed, "checkout", "-b", "side")
    b = commit(seed, "b")
    git(seed, "push", "origin", "side")
    git(seed, "checkout", "main")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", str(origin), str(clone))
    return {"origin": origin, "seed": seed, "clone": clone, "a": a, "b": b}


async def test_is_on_main(world):
    assert await gitops.is_on_main(world["clone"], world["a"]) is True
    assert await gitops.is_on_main(world["clone"], world["b"]) is False


async def test_unknown_sha_is_not_on_main(world):
    assert await gitops.is_on_main(world["clone"], "f" * 40) is False
    assert await gitops.is_ancestor(world["clone"], "f" * 40, world["a"]) is False


async def test_sha_pushed_after_clone_needs_fetch(world):
    new = commit(world["seed"], "c")
    git(world["seed"], "push", "origin", "main")
    assert await gitops.is_on_main(world["clone"], new) is False
    await gitops.fetch(world["clone"])
    assert await gitops.is_on_main(world["clone"], new) is True
    assert await gitops.main_head(world["clone"]) == new


async def test_is_ancestor(world):
    new = commit(world["seed"], "c")
    git(world["seed"], "push", "origin", "main")
    await gitops.fetch(world["clone"])
    assert await gitops.is_ancestor(world["clone"], world["a"], new) is True
    assert await gitops.is_ancestor(world["clone"], new, world["a"]) is False
    assert await gitops.is_ancestor(world["clone"], world["a"], world["a"]) is True


async def test_resolve_short_sha_and_ref(world):
    assert await gitops.resolve(world["clone"], world["a"][:8]) == world["a"]
    assert await gitops.resolve(world["clone"], "origin/main") == world["a"]


async def test_resolve_unknown_raises(world):
    with pytest.raises(GitError):
        await gitops.resolve(world["clone"], "nonexistent-ref")


async def test_checkout_leaves_worktree_at_sha(world):
    clone = world["clone"]
    (clone / "stray.txt").write_text("x")
    (clone / "a").write_text("modified")
    (clone / ".venv").mkdir()
    (clone / ".venv" / "keep").write_text("k")
    await gitops.fetch(clone)
    await gitops.checkout(clone, world["b"])
    assert git(clone, "rev-parse", "HEAD") == world["b"]
    assert git(clone, "status", "--porcelain", "--ignored") == "?? .venv/"
    assert (clone / "b").exists()
    assert (clone / ".venv" / "keep").exists()
    assert not (clone / "stray.txt").exists()


async def test_ensure_clone_clones_then_is_idempotent(world, tmp_path):
    target = tmp_path / "runner"
    await gitops.ensure_clone(target, str(world["origin"]))
    assert await gitops.main_head(target) == world["a"]
    marker = target / "marker"
    marker.write_text("m")
    await gitops.ensure_clone(target, str(world["origin"]))
    assert marker.exists()


async def test_git_failure_raises_with_stderr(tmp_path):
    with pytest.raises(GitError) as exc:
        await gitops.fetch(tmp_path / "not-a-repo")
    assert str(exc.value)


async def test_add_worktree_is_detached_at_sha_and_leaves_clone_alone(world, tmp_path):
    clone = world["clone"]
    new = commit(world["seed"], "c")
    git(world["seed"], "push", "origin", "main")
    await gitops.fetch(clone)
    before = git(clone, "rev-parse", "HEAD")
    wt = tmp_path / "data" / "worktrees" / "7"
    await gitops.add_worktree(clone, wt, new)
    assert git(wt, "rev-parse", "HEAD") == new
    assert (wt / "c").exists()
    assert git(clone, "rev-parse", "HEAD") == before  # the shared clone is untouched


async def test_two_worktrees_coexist(world, tmp_path):
    clone = world["clone"]
    await gitops.fetch(clone)
    one, two = tmp_path / "w1", tmp_path / "w2"
    await gitops.add_worktree(clone, one, world["a"])
    await gitops.add_worktree(clone, two, world["b"])
    assert git(one, "rev-parse", "HEAD") == world["a"]
    assert git(two, "rev-parse", "HEAD") == world["b"]


async def test_add_worktree_replaces_a_stale_one(world, tmp_path):
    clone = world["clone"]
    await gitops.fetch(clone)
    wt = tmp_path / "w"
    await gitops.add_worktree(clone, wt, world["a"])
    (wt / "junk").write_text("x")
    await gitops.add_worktree(clone, wt, world["b"])
    assert git(wt, "rev-parse", "HEAD") == world["b"]
    assert not (wt / "junk").exists()


async def test_remove_worktree_removes_dirty_tree_and_registration(world, tmp_path):
    clone = world["clone"]
    wt = tmp_path / "w"
    await gitops.add_worktree(clone, wt, world["a"])
    (wt / "untracked").write_text("x")
    (wt / "a").write_text("modified")
    await gitops.remove_worktree(clone, wt)
    assert not wt.exists()
    assert str(wt) not in git(clone, "worktree", "list")
    await gitops.remove_worktree(clone, wt)  # already gone: no error


async def test_remove_worktree_cleans_up_a_plain_directory(world, tmp_path):
    clone = world["clone"]
    wt = tmp_path / "w"
    await gitops.add_worktree(clone, wt, world["a"])
    import shutil

    shutil.rmtree(wt)  # deleted behind git's back: registration is now stale
    await gitops.remove_worktree(clone, wt)
    assert str(wt) not in git(clone, "worktree", "list")


async def test_add_worktree_rejects_option_like_sha(world, tmp_path):
    with pytest.raises(GitError):
        await gitops.add_worktree(world["clone"], tmp_path / "w", "--orphan")
