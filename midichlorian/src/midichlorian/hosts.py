"""Static registry of the hosts midichlorian can run playbooks against."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class HostSpec:
    name: str
    ssh: str
    wrapper: str
    ro_root: bool
    linux: bool
    docker: bool
    printer_sensitive: Literal["always", "samba", "never"]
    needs_backup_gate: bool
    # the playbook builds images on the runner, so the runner's Docker must be up
    controller_docker: bool = False


def _host(
    name,
    *,
    ro_root=True,
    linux=True,
    docker=True,
    printer="never",
    backup=False,
    controller_docker=False,
):
    return HostSpec(
        name=name,
        ssh=f"{name}.local",
        wrapper=f"scripts/{name}-ansible.sh",
        ro_root=ro_root,
        linux=linux,
        docker=docker,
        printer_sensitive=printer,
        needs_backup_gate=backup,
        controller_docker=controller_docker,
    )


HOSTS: dict[str, HostSpec] = {
    h.name: h
    for h in (
        _host("luke", printer="samba", backup=True, controller_docker=True),
        _host("leia", printer="always", controller_docker=True),
        _host("cal", controller_docker=True),
        _host("mace"),
        _host("rey", ro_root=False, printer="always", controller_docker=True),
        _host("cere", ro_root=False, linux=False, docker=False),
    )
}


# Luke tags known NOT to touch Samba, volatile-fs, the print-recordings units or
# Docker (any of which can bounce Samba and stale the /mnt/print-recordings CIFS
# mounts a print is recording to). A Luke run whose tags all sit here skips the
# printer hold; every other Luke run, full or tagged, waits for the printers (R26).
# Empty on purpose: fail closed. To add a tag, read every task and handler it
# reaches in home-infra's luke/ansible/playbook.yml (including notified handlers
# such as "Restart Docker" and volatile-fs's ExecStartPost) and confirm none of
# them restarts Samba, volatile-fs, print-recordings or Docker.
LUKE_HOLD_FREE_TAGS: frozenset[str] = frozenset()


def printer_hold_applies(host: str, tags: list[str] | None) -> bool:
    """Whether a run on `host` must wait for the printers to be idle.

    "always" hosts always hold. Luke ("samba") holds unless the run is tagged and
    every tag is in LUKE_HOLD_FREE_TAGS.
    """
    mode = HOSTS[host].printer_sensitive
    if mode == "always":
        return True
    if mode == "samba":
        return not (tags and set(tags) <= LUKE_HOLD_FREE_TAGS)
    return False
