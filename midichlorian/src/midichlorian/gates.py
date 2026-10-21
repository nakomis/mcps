"""Pre-run gates: AWS credentials, Docker, printers and Luke's backups."""

from dataclasses import dataclass

from midichlorian import hosts, remote
from midichlorian.hosts import printer_hold_applies

BACKUP_OK = "All databases backed up and verified"


@dataclass(frozen=True)
class GateResult:
    ok: bool
    wait: bool
    reason: str


def _ok(reason: str = "") -> GateResult:
    return GateResult(True, False, reason)


def _fail(reason: str) -> GateResult:
    return GateResult(False, False, reason)


async def check_aws(env: dict) -> GateResult:
    r = await remote.run(
        ["aws", "sts", "get-caller-identity", "--profile", "nakom.is-admin"], env=env
    )
    if r.rc == 0:
        return _ok()
    return _fail((r.stderr or r.stdout).strip() or f"aws exited {r.rc}")


async def check_docker(env: dict) -> GateResult:
    r = await remote.run(["docker", "info"], env=env)
    if r.rc == 0:
        return _ok()
    return _fail((r.stderr or r.stdout).strip() or f"docker exited {r.rc}")


async def check_printers(host: str, tags, override: bool, probes) -> GateResult:
    if not printer_hold_applies(host, tags):
        return _ok()
    if override:
        return _ok("printer hold overridden by Martin")
    busy = [s for s in await probes.states() if s.busy]
    if busy:
        return GateResult(False, True, "printer busy — " + "; ".join(s.detail for s in busy))
    return _ok()


async def run_luke_backups(ssh) -> GateResult:
    r = await ssh(hosts.HOSTS["luke"], "./run-backups.sh", timeout=1800)
    if r.rc == 0 and BACKUP_OK in r.stdout + r.stderr:
        return _ok()
    tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-20:])
    return _fail(f"Luke backups failed (rc {r.rc}):\n{tail}")
