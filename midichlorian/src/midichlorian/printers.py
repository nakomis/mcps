"""Printer probes: Cthulhu (resin) over HTTP, the Prusa via OctoPrint on Leia.

An unreachable printer is treated as idle (reachable=False), never as a hold.
"""

import json
from dataclasses import dataclass

import httpx

from midichlorian.hosts import HostSpec

CTHULHU_URL = "http://172.29.0.36:9120/api/status"
CTHULHU_IDLE = {"", "Idle", "Complete", "Stopped", "Cancelled"}
OCTOPRINT_BUSY = {"Printing", "Pausing", "Paused", "Resuming", "Cancelling", "Finishing"}
OCTOPRINT_CONTAINER = "gateway-octoprint-1"

_JOB_COMMAND = (
    "read -r OCTO_KEY; export OCTO_KEY; "
    f"docker exec -e OCTO_KEY {OCTOPRINT_CONTAINER} sh -c "
    "'curl -s -H \"X-Api-Key: $OCTO_KEY\" http://localhost:5000/api/job'"
)


@dataclass(frozen=True)
class PrinterState:
    name: str
    busy: bool
    detail: str
    reachable: bool


async def cthulhu_state(client: httpx.AsyncClient, url: str = CTHULHU_URL) -> PrinterState:
    try:
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise ValueError("unexpected payload")
    except (httpx.HTTPError, ValueError) as exc:
        return PrinterState("cthulhu", False, f"Cthulhu unreachable ({type(exc).__name__})", False)
    if not data.get("connected"):
        return PrinterState("cthulhu", False, "Cthulhu not connected", True)
    label = (data.get("print") or {}).get("statusLabel") or ""
    if label in CTHULHU_IDLE:
        return PrinterState("cthulhu", False, f"Cthulhu: {label or 'Idle'}", True)
    return PrinterState("cthulhu", True, f"Cthulhu: {label}", True)


async def octoprint_state(ssh, host: HostSpec, api_key: str | None) -> PrinterState:
    """`ssh` is an async callable (host, command, timeout=, stdin=) -> RunResult."""
    if not api_key:
        return PrinterState("octoprint", False, "no OctoPrint API key", False)

    def unreachable(why: str) -> PrinterState:
        return PrinterState("octoprint", False, f"OctoPrint unreachable ({why})", False)

    running = await ssh(
        host, f"docker inspect -f '{{{{.State.Running}}}}' {OCTOPRINT_CONTAINER}", timeout=30
    )
    if running.rc != 0:
        # A missing container is also "not running"; only ssh-level failure is unreachable.
        if "No such" in running.stderr or "no such" in running.stderr:
            return PrinterState("octoprint", False, "OctoPrint container not running", True)
        return unreachable(f"ssh rc {running.rc}")
    if running.stdout.strip() != "true":
        return PrinterState("octoprint", False, "OctoPrint container not running", True)

    job = await ssh(host, _JOB_COMMAND, timeout=30, stdin=(api_key + "\n").encode())
    if job.rc != 0:
        return unreachable(f"ssh rc {job.rc}")
    try:
        data = json.loads(job.stdout)
        state = data["state"]
        if not isinstance(state, str):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return unreachable("unparsable response")
    if state in OCTOPRINT_BUSY:
        return PrinterState("octoprint", True, f"Prusa: {state}", True)
    return PrinterState("octoprint", False, f"Prusa: {state}", True)


class Probes:
    """Injectable bundle of the printer probes the gates consult."""

    def __init__(self, client: httpx.AsyncClient, leia: HostSpec, ssh, api_key: str | None):
        self._client = client
        self._leia = leia
        self._ssh = ssh
        self._key = api_key

    async def states(self) -> list[PrinterState]:
        return [
            await cthulhu_state(self._client),
            await octoprint_state(self._ssh, self._leia, self._key),
        ]
