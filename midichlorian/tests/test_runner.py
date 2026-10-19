import asyncio
import os
from pathlib import Path

from midichlorian import runner
from midichlorian.hosts import HOSTS

SCRIPT = """#!/bin/sh
echo "args: $*"
echo "skip=$DEPLOY_SKIP_CHECKOUT_CHECK pwd=$(pwd)"
echo "to stderr" >&2
exit 3
"""


def make_repo(tmp_path: Path, host: str, body: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / f"{host}-ansible.sh"
    script.write_text(body)
    script.chmod(0o755)
    return repo


async def test_ansible_runner_appends_output_and_returns_rc(tmp_path):
    repo = make_repo(tmp_path, "cal", SCRIPT)
    log = tmp_path / "logs" / "1.log"
    log.parent.mkdir()
    log.write_text("header\n")
    env = runner.build_env({"PATH": os.environ["PATH"]})
    rc = await runner.AnsibleRunner().run(HOSTS["cal"], ["a", "b"], repo, log, env)
    assert rc == 3
    lines = log.read_text().splitlines()
    assert lines[0] == "header"
    assert "args: --tags a,b" in lines
    assert f"skip=1 pwd={repo.resolve()}" in lines
    assert "to stderr" in lines


async def test_ansible_runner_full_run_passes_no_tags(tmp_path):
    repo = make_repo(tmp_path, "cal", SCRIPT)
    log = tmp_path / "1.log"
    rc = await runner.AnsibleRunner().run(
        HOSTS["cal"], None, repo, log, runner.build_env({"PATH": os.environ["PATH"]})
    )
    assert rc == 3
    assert "args: " in log.read_text().splitlines()


async def test_ansible_runner_flushes_each_line_while_running(tmp_path):
    go = tmp_path / "go"
    body = f"""#!/bin/sh
echo first
while [ ! -f {go} ]; do sleep 0.05; done
echo second
"""
    repo = make_repo(tmp_path, "cal", body)
    log = tmp_path / "1.log"
    task = asyncio.create_task(
        runner.AnsibleRunner().run(
            HOSTS["cal"],
            None,
            repo,
            log,
            runner.build_env({"PATH": os.environ["PATH"]}),
        )
    )
    try:
        for _ in range(200):
            if log.exists() and "first" in log.read_text():
                break
            await asyncio.sleep(0.02)
        assert log.read_text() == "first\n"  # visible before the script finishes
    finally:
        go.write_text("")
    assert await asyncio.wait_for(task, 10) == 0
    assert log.read_text() == "first\nsecond\n"


async def test_ansible_runner_missing_wrapper_is_rc_127_with_a_line(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    log = tmp_path / "1.log"
    rc = await runner.AnsibleRunner().run(HOSTS["cal"], None, repo, log, {})
    assert rc == 127
    assert "could not start" in log.read_text()


def test_build_env(tmp_path):
    brew = tmp_path / "brew-bin"
    brew.mkdir()
    env = runner.build_env({"PATH": "/usr/bin", "GH_TOKEN": "t"}, homebrew=brew)
    assert env["DEPLOY_SKIP_CHECKOUT_CHECK"] == "1"
    assert env["ANSIBLE_FORCE_COLOR"] == "0"
    assert env["ANSIBLE_NOCOLOR"] == "1"
    assert env["GH_TOKEN"] == "t"
    assert env["PATH"] == f"{brew}:/usr/bin"
    again = runner.build_env(env, homebrew=brew)
    assert again["PATH"] == f"{brew}:/usr/bin"  # not prefixed twice
    absent = runner.build_env({"PATH": "/usr/bin"}, homebrew=tmp_path / "nope")
    assert absent["PATH"] == "/usr/bin"


def test_append_log_writes_whole_lines(tmp_path):
    log = tmp_path / "sub" / "1.log"
    runner.append_log(log, "one\ntwo")
    runner.append_log(log, "three")
    assert log.read_text() == "one\ntwo\nthree\n"


def test_log_tail(tmp_path):
    log = tmp_path / "1.log"
    log.write_text("".join(f"line {i}\n" for i in range(100)))
    tail = runner.log_tail(log, 40)
    assert tail.splitlines()[0] == "line 60" and tail.splitlines()[-1] == "line 99"
    assert runner.log_tail(tmp_path / "missing.log") == ""


async def test_cancel_kills_the_whole_process_group(tmp_path):
    pids = tmp_path / "pids"
    body = f"""#!/bin/sh
trap '' TERM
sh -c 'trap "" TERM; echo $$ >> {pids}; sleep 30' &
echo $$ >> {pids}
echo started
wait
"""
    repo = make_repo(tmp_path, "cal", body)
    log = tmp_path / "1.log"
    task = asyncio.create_task(
        runner.AnsibleRunner(kill_grace=0.3).run(
            HOSTS["cal"],
            None,
            repo,
            log,
            runner.build_env({"PATH": os.environ["PATH"]}),
        )
    )
    for _ in range(200):
        if pids.exists() and len(pids.read_text().split()) == 2:
            break
        await asyncio.sleep(0.02)
    task.cancel()
    try:
        await asyncio.wait_for(task, 10)
    except asyncio.CancelledError:
        pass
    for pid in map(int, pids.read_text().split()):
        for _ in range(100):  # give the kernel a moment to reap
            if not _alive(pid):
                break
            await asyncio.sleep(0.02)
        assert not _alive(pid), pid


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
