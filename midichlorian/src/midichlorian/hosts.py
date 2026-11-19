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
    printer_sensitive: Literal["always", "never"]
    # the playbook builds images on the runner, so the runner's Docker must be up
    controller_docker: bool = False


def _host(
    name,
    *,
    ro_root=True,
    linux=True,
    docker=True,
    printer="never",
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
        controller_docker=controller_docker,
    )


HOSTS: dict[str, HostSpec] = {
    h.name: h
    for h in (
        _host("cal", controller_docker=True),
        _host("mace"),
        _host("rey", ro_root=False, printer="always", controller_docker=True),
        _host("cere", ro_root=False, linux=False, docker=False),
    )
}


def printer_hold_applies(host: str, tags: list[str] | None) -> bool:
    """Whether a run on `host` must wait for the printers to be idle."""
    return HOSTS[host].printer_sensitive == "always"
