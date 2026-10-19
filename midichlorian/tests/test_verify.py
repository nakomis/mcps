import json

from midichlorian import verify
from midichlorian.hosts import HOSTS
from midichlorian.remote import RunResult

LUKE, CERE, REY, LEIA, MACE = (HOSTS[n] for n in ("luke", "cere", "rey", "leia", "mace"))


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


PS_BEFORE = "samba|running\nweb|running\n"
INSPECT_BEFORE = "/samba|0\n/web|0\n"


def healthy(**over):
    answers = {
        "findmnt": ok("ro,relatime\n"),
        "systemctl --failed": ok(""),
        "docker ps -a": ok(PS_BEFORE),
        "docker inspect --format": ok(INSPECT_BEFORE),
        "StartedAt": ok("2026-10-19T10:00:00Z\n"),
    }
    answers.update(over)
    return FakeSsh(answers)


def by_name(checks):
    return {c.name: c for c in checks}


async def test_snapshot_is_json_serialisable_and_one_call_each():
    ssh = healthy()
    snap = await verify.snapshot(LUKE, ssh)
    json.dumps(snap)
    assert snap["containers"] == {
        "samba": {"state": "running", "restarts": 0},
        "web": {"state": "running", "restarts": 0},
    }
    assert snap["samba_started"] == "2026-10-19T10:00:00Z"
    assert sum("docker ps" in c for _, c in ssh.calls) == 1
    assert sum("docker inspect --format" in c for _, c in ssh.calls) == 1


async def test_snapshot_cere_makes_no_calls():
    ssh = healthy()
    snap = await verify.snapshot(CERE, ssh)
    assert snap["containers"] == {}
    assert ssh.calls == []


async def test_all_clear_on_luke():
    ssh = healthy()
    before = await verify.snapshot(LUKE, ssh)
    checks = by_name(await verify.run_checks(LUKE, before, ssh))
    assert all(c.ok for c in checks.values()), checks
    assert {"ro root", "failed units", "containers", "samba"} <= set(checks)
    assert not any(n.startswith("cifs") for n in checks)


async def test_rw_root_fails():
    ssh = healthy(findmnt=ok("rw,relatime\n"))
    checks = by_name(await verify.run_checks(LUKE, {"containers": {}}, ssh))
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
    checks = by_name(await verify.run_checks(LEIA, {"containers": {}}, ssh))
    assert not checks["failed units"].ok
    assert "foo.service" in checks["failed units"].detail


async def test_restart_count_jump_fails_but_one_is_fine():
    before = {"containers": {"web": {"state": "running", "restarts": 0}}}
    ssh = healthy(**{"docker inspect --format": ok("/web|1\n")})
    assert by_name(await verify.run_checks(LEIA, before, ssh))["containers"].ok
    ssh = healthy(**{"docker inspect --format": ok("/web|2\n")})
    c = by_name(await verify.run_checks(LEIA, before, ssh))["containers"]
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
    c = by_name(await verify.run_checks(LEIA, before, ssh))["containers"]
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
        checks = by_name(await verify.run_checks(LUKE, {"containers": {}}, ssh))
        assert not checks["ro root"].ok and "boom" in checks["ro root"].detail
        assert not checks["failed units"].ok
        assert not checks["containers"].ok and "boom" in checks["containers"].detail


async def test_samba_restart_triggers_cifs_on_three_hosts():
    ssh = healthy()
    before = {"containers": {}, "samba_started": "2026-10-18T09:00:00Z"}
    checks = by_name(await verify.run_checks(LUKE, before, ssh))
    assert checks["samba"].ok
    for h in ("rey", "leia", "mace"):
        assert checks[f"cifs {h}"].ok
    ls = [(h, c) for h, c in ssh.calls if "ls /mnt/print-recordings" in c]
    assert sorted(h for h, _ in ls) == ["leia", "mace", "rey"]
    assert all(c.startswith("timeout 10 ls") for _, c in ls)


async def test_stale_handle_is_remounted_and_rey_printing_defers():
    def stale(host):
        return RunResult(2, "", "ls: cannot access: Stale file handle")

    ssh = healthy(**{"ls /mnt/print-recordings": stale})
    before = {"containers": {}, "samba_started": "old"}
    checks = by_name(await verify.run_checks(LUKE, before, ssh, rey_printing=True))
    umounts = [h for h, c in ssh.calls if c == "sudo umount -l -t cifs /mnt/print-recordings"]
    assert sorted(umounts) == ["leia", "mace"]
    assert checks["cifs rey"].ok
    assert checks["cifs rey"].detail == "stale; remount deferred — printing"
    assert checks["cifs leia"].ok and checks["cifs mace"].ok

    ssh = healthy(**{"ls /mnt/print-recordings": stale})
    await verify.run_checks(LUKE, before, ssh, rey_printing=False)
    assert "rey" in [h for h, c in ssh.calls if "umount" in c]


async def test_cifs_other_failure_and_failed_umount():
    ssh = healthy(**{"ls /mnt/print-recordings": RunResult(255, "", "no route")})
    checks = by_name(await verify.run_checks(LUKE, {"containers": {}, "samba_started": "x"}, ssh))
    assert not checks["cifs leia"].ok and "no route" in checks["cifs leia"].detail

    ssh = healthy(**{
        "ls /mnt/print-recordings": RunResult(2, "", "Stale file handle"),
        "umount": RunResult(32, "", "busy"),
    })
    checks = by_name(await verify.run_checks(LUKE, {"containers": {}, "samba_started": "x"}, ssh))
    assert not checks["cifs leia"].ok and "busy" in checks["cifs leia"].detail


async def test_snapshot_error_means_no_baseline_check_fails():
    ssh = healthy()
    checks = by_name(
        await verify.run_checks(LEIA, {"containers": {}, "error": "boom"}, ssh)
    )
    assert not checks["containers"].ok
    assert checks["containers"].detail == "no baseline: boom"


async def test_unreadable_baseline_samba_is_not_a_restart():
    ssh = healthy()
    checks = by_name(
        await verify.run_checks(LUKE, {"containers": {}, "samba_started": None}, ssh)
    )
    assert "baseline" in checks["samba"].detail
    assert not any(n.startswith("cifs") for n in checks)
    assert not any("ls /mnt/print-recordings" in c for _, c in ssh.calls)


async def test_unreadable_current_samba_fails():
    ssh = healthy(StartedAt=RunResult(1, "", "no such container"))
    checks = by_name(
        await verify.run_checks(LUKE, {"containers": {}, "samba_started": "x"}, ssh)
    )
    assert not checks["samba"].ok


async def test_inspect_quotes_names_and_run_checks_makes_one_ps_one_inspect():
    ssh = healthy(**{
        "docker ps -a": ok("we ird|running\nweb|running\n"),
        "docker inspect --format": ok("/we ird|0\n/web|0\n"),
    })
    await verify.run_checks(LEIA, {"containers": {}}, ssh)
    assert sum("docker ps" in c for _, c in ssh.calls) == 1
    inspects = [c for _, c in ssh.calls if "docker inspect --format" in c]
    assert len(inspects) == 1
    assert "'we ird'" in inspects[0]
