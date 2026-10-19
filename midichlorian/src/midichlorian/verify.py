"""Before/after checks around a playbook run. Never raises: failures become Checks."""

import shlex
from dataclasses import dataclass

from midichlorian.hosts import HOSTS, HostSpec

PS_CMD = "docker ps -a --format '{{.Names}}|{{.State}}'"
SAMBA_CMD = "docker inspect -f '{{.State.StartedAt}}' samba"
CIFS_PATH = "/mnt/print-recordings"
CIFS_HOSTS = ("rey", "leia", "mace")


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _err(res) -> str:
    return (res.stderr or res.stdout or f"rc {res.rc}").strip()


async def _containers(host: HostSpec, ssh) -> tuple[dict | None, str]:
    """Return ({name: {state, restarts}}, "") or (None, error)."""
    ps = await ssh(host, PS_CMD)
    if ps.rc != 0:
        return None, _err(ps)
    states = dict(
        line.split("|", 1) for line in ps.stdout.splitlines() if "|" in line
    )
    if not states:
        return {}, ""
    names = " ".join(shlex.quote(n) for n in states)
    insp = await ssh(
        host, "docker inspect --format '{{.Name}}|{{.RestartCount}}' " + names
    )
    if insp.rc != 0:
        return None, _err(insp)
    restarts = {}
    for line in insp.stdout.splitlines():
        name, _, count = line.partition("|")
        if count.strip().isdigit():
            restarts[name.lstrip("/")] = int(count)
    return {
        n: {"state": s.strip(), "restarts": restarts.get(n, 0)}
        for n, s in states.items()
    }, ""


async def _samba_started(host: HostSpec, ssh) -> str | None:
    res = await ssh(host, SAMBA_CMD)
    return res.stdout.strip() if res.rc == 0 and res.stdout.strip() else None


async def snapshot(host: HostSpec, ssh) -> dict:
    snap: dict = {"containers": {}, "samba_started": None}
    if host.docker:
        containers, error = await _containers(host, ssh)
        snap["containers"] = containers or {}
        if error:
            snap["error"] = error
        if host.name == "luke":
            snap["samba_started"] = await _samba_started(host, ssh)
    return snap


async def _check_ro_root(host, ssh) -> Check:
    res = await ssh(host, "findmnt -no OPTIONS /")
    if res.rc != 0:
        return Check("ro root", False, _err(res))
    options = res.stdout.strip().split(",")
    if options[0] == "ro":
        return Check("ro root", True, "/ is read-only")
    return Check("ro root", False, f"/ is mounted {options[0] or 'unknown'}: {res.stdout.strip()}")


async def _check_units(host, ssh) -> Check:
    res = await ssh(host, "systemctl --failed --no-legend")
    if res.rc != 0:
        return Check("failed units", False, _err(res))
    out = res.stdout.strip()
    return Check("failed units", not out, out or "none")


async def _check_containers(host, before, ssh) -> Check:
    if "error" in before:
        return Check("containers", False, f"no baseline: {before['error']}")
    now, error = await _containers(host, ssh)
    if now is None:
        return Check("containers", False, error)
    problems = []
    for name, old in before.get("containers", {}).items():
        if old.get("state") != "running":
            continue
        cur = now.get(name)
        if cur is None:
            problems.append(f"{name} was running and has been removed")
        elif cur["state"] != "running":
            problems.append(f"{name} is {cur['state']}, was running")
        elif cur["restarts"] - old.get("restarts", 0) > 1:
            problems.append(
                f"{name} restarted {cur['restarts'] - old.get('restarts', 0)} times"
            )
    return Check("containers", not problems, "; ".join(problems) or f"{len(now)} checked")


async def _check_cifs(name: str, ssh, rey_printing: bool) -> Check:
    label = f"cifs {name}"
    host = HOSTS[name]
    res = await ssh(host, f"timeout 10 ls {CIFS_PATH}")
    if res.rc == 0:
        return Check(label, True, "readable")
    if "Stale file handle" not in res.stderr + res.stdout:
        return Check(label, False, _err(res))
    if name == "rey" and rey_printing:
        return Check(label, True, "stale; remount deferred — printing")
    um = await ssh(host, f"sudo umount -l -t cifs {CIFS_PATH}")
    if um.rc != 0:
        return Check(label, False, f"stale; umount failed: {_err(um)}")
    return Check(label, True, "stale; unmounted, will remount on next access")


async def run_checks(
    host: HostSpec, before: dict, ssh, *, rey_printing: bool = False
) -> list[Check]:
    checks: list[Check] = []
    if host.ro_root:
        checks.append(await _check_ro_root(host, ssh))
    if host.linux:
        checks.append(await _check_units(host, ssh))
    if host.docker:
        checks.append(await _check_containers(host, before, ssh))
    if host.name == "luke":
        started = await _samba_started(host, ssh)
        old = before.get("samba_started")
        if started is None:
            checks.append(Check("samba", False, "could not read samba StartedAt"))
        else:
            if old is None:
                checks.append(
                    Check("samba", True, "baseline StartedAt unreadable; restart not assessed")
                )
            else:
                restarted = started != old
                checks.append(
                    Check("samba", True, "restarted" if restarted else "not restarted")
                )
            if old is not None and started != old:
                for name in CIFS_HOSTS:
                    checks.append(await _check_cifs(name, ssh, rey_printing))
    return checks
