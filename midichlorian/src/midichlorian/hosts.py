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


def _host(
    name, *, ro_root=True, linux=True, docker=True, printer="never", backup=False
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
    )


HOSTS: dict[str, HostSpec] = {
    h.name: h
    for h in (
        _host("luke", printer="samba", backup=True),
        _host("leia", printer="always"),
        _host("cal"),
        _host("mace"),
        _host("rey", ro_root=False, printer="always"),
        _host("cere", ro_root=False, linux=False, docker=False),
    )
}


def printer_hold_applies(host: str, tags: list[str] | None) -> bool:
    """Whether a run on `host` must wait for the printers to be idle.

    A "samba" host only holds for full runs (no tags) or runs including the samba tag.
    """
    mode = HOSTS[host].printer_sensitive
    if mode == "always":
        return True
    if mode == "samba":
        return not tags or "samba" in tags
    return False
