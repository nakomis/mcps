"""Before/after checks around a playbook run. Never raises: failures become Checks."""

import shlex
from dataclasses import dataclass

from midichlorian.hosts import HostSpec

PS_CMD = "docker ps -a --format '{{.Names}}|{{.State}}'"


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


async def snapshot(host: HostSpec, ssh) -> dict:
    snap: dict = {"containers": {}}
    if host.docker:
        containers, error = await _containers(host, ssh)
        snap["containers"] = containers or {}
        if error:
            snap["error"] = error
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


async def run_checks(host: HostSpec, before: dict, ssh) -> list[Check]:
    checks: list[Check] = []
    if host.ro_root:
        checks.append(await _check_ro_root(host, ssh))
    if host.linux:
        checks.append(await _check_units(host, ssh))
    if host.docker:
        checks.append(await _check_containers(host, before, ssh))
    return checks
