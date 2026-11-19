import json

from midichlorian import verify
from midichlorian.hosts import HOSTS
from midichlorian.remote import RunResult

CAL, CERE, REY, MACE = (HOSTS[n] for n in ("cal", "cere", "rey", "mace"))


class FakeSsh:
    """Answers by substring match on the command; records (host, command) calls."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    async def __call__(self, host, command):
        self.calls.append((host.name, command))
        for needle, result in self.answers.items():
            if needle in command:
                return result(host) if callable(result) else result
        return RunResult(0, "", "")


def ok(out=""):
    return RunResult(0, out, "")


PS_BEFORE = "db|running\nweb|running\n"
INSPECT_BEFORE = "/db|0\n/web|0\n"


def healthy(**over):
    answers = {
        "findmnt": ok("ro,relatime\n"),
        "systemctl --failed": ok(""),
        "docker ps -a": ok(PS_BEFORE),
        "docker inspect --format": ok(INSPECT_BEFORE),
    }
    answers.update(over)
    return FakeSsh(answers)


def by_name(checks):
    return {c.name: c for c in checks}


async def test_snapshot_is_json_serialisable_and_one_call_each():
    ssh = healthy()
    snap = await verify.snapshot(MACE, ssh)
    json.dumps(snap)
    assert snap["containers"] == {
        "db": {"state": "running", "restarts": 0},
        "web": {"state": "running", "restarts": 0},
    }
    assert sum("docker ps" in c for _, c in ssh.calls) == 1
    assert sum("docker inspect --format" in c for _, c in ssh.calls) == 1


async def test_snapshot_cere_makes_no_calls():
    ssh = healthy()
    snap = await verify.snapshot(CERE, ssh)
    assert snap["containers"] == {}
    assert ssh.calls == []


async def test_all_clear_on_mace():
    ssh = healthy()
    before = await verify.snapshot(MACE, ssh)
    checks = by_name(await verify.run_checks(MACE, before, ssh))
    assert all(c.ok for c in checks.values()), checks
    assert {"ro root", "failed units", "containers"} == set(checks)


async def test_rw_root_fails():
    ssh = healthy(findmnt=ok("rw,relatime\n"))
    checks = by_name(await verify.run_checks(MACE, {"containers": {}}, ssh))
    assert not checks["ro root"].ok
    assert "rw" in checks["ro root"].detail


async def test_rey_has_no_ro_check_and_cere_is_minimal():
    ssh = healthy()
    checks = by_name(await verify.run_checks(REY, {"containers": {}}, ssh))
    assert "ro root" not in checks
    ssh = healthy()
    assert await verify.run_checks(CERE, {}, ssh) == []
    assert ssh.calls == []


async def test_failed_units():
    ssh = healthy(**{"systemctl --failed": ok("foo.service loaded failed failed Foo\n")})
    checks = by_name(await verify.run_checks(CAL, {"containers": {}}, ssh))
    assert not checks["failed units"].ok
    assert "foo.service" in checks["failed units"].detail


async def test_restart_count_jump_fails_but_one_is_fine():
    before = {"containers": {"web": {"state": "running", "restarts": 0}}}
    ssh = healthy(**{"docker inspect --format": ok("/web|1\n")})
    assert by_name(await verify.run_checks(CAL, before, ssh))["containers"].ok
    ssh = healthy(**{"docker inspect --format": ok("/web|2\n")})
    c = by_name(await verify.run_checks(CAL, before, ssh))["containers"]
    assert not c.ok and "web" in c.detail


async def test_container_stopped_or_removed_fails_new_is_fine():
    before = {"containers": {
        "web": {"state": "running", "restarts": 0},
        "gone": {"state": "running", "restarts": 0},
        "off": {"state": "exited", "restarts": 0},
    }}
    ssh = healthy(**{
        "docker ps -a": ok("web|exited\nfresh|running\noff|exited\n"),
        "docker inspect --format": ok("/web|0\n/fresh|0\n/off|0\n"),
    })
    c = by_name(await verify.run_checks(CAL, before, ssh))["containers"]
    assert not c.ok
    assert "web" in c.detail and "gone" in c.detail
    assert "fresh" not in c.detail and "off" not in c.detail


async def test_ssh_failure_never_raises():
    for rc in (255, 124, 1):
        ssh = healthy(**{
            "findmnt": RunResult(rc, "", "boom"),
            "systemctl --failed": RunResult(rc, "", "boom"),
            "docker ps -a": RunResult(rc, "", "boom"),
        })
        checks = by_name(await verify.run_checks(MACE, {"containers": {}}, ssh))
        assert not checks["ro root"].ok and "boom" in checks["ro root"].detail
        assert not checks["failed units"].ok
        assert not checks["containers"].ok and "boom" in checks["containers"].detail


async def test_snapshot_error_means_no_baseline_check_fails():
    ssh = healthy()
    checks = by_name(
        await verify.run_checks(CAL, {"containers": {}, "error": "boom"}, ssh)
    )
    assert not checks["containers"].ok
    assert checks["containers"].detail == "no baseline: boom"


async def test_inspect_quotes_names_and_run_checks_makes_one_ps_one_inspect():
    ssh = healthy(**{
        "docker ps -a": ok("we ird|running\nweb|running\n"),
        "docker inspect --format": ok("/we ird|0\n/web|0\n"),
    })
    await verify.run_checks(CAL, {"containers": {}}, ssh)
    assert sum("docker ps" in c for _, c in ssh.calls) == 1
    inspects = [c for _, c in ssh.calls if "docker inspect --format" in c]
    assert len(inspects) == 1
    assert "'we ird'" in inspects[0]
