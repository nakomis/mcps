"""Printer probes: Cthulhu (resin) over HTTP, the Prusa via OctoPrint on Leia.

Two kinds of "can't tell" are treated differently (R25):

- the printer or its service is genuinely unreachable or off (connection refused,
  timeout, ssh down, OctoPrint's container not running): idle, with a note, since
  there is nothing to disturb;
- it is there but its state cannot be read (no or wrong API key, an HTTP error,
  junk): busy ("state unreadable"), so runs wait. Only an override lifts that.
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from midichlorian.hosts import HostSpec

logger = logging.getLogger(__name__)

CTHULHU_URL = "http://172.29.0.36:9120/api/status"
CTHULHU_IDLE = {"", "Idle", "Complete", "Stopped", "Cancelled"}
OCTOPRINT_BUSY = {"Printing", "Pausing", "Paused", "Resuming", "Cancelling", "Finishing"}
OCTOPRINT_CONTAINER = "gateway-octoprint-1"

KEY_RETRY_SECONDS = 300.0  # a missing OctoPrint key is asked for again at most this often
SSH_UNREACHABLE = 255  # ssh's own failure exit code

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
    def unreadable(why: str) -> PrinterState:
        return PrinterState("cthulhu", True, f"Cthulhu state unreadable: {why}", True)

    try:
        resp = await client.get(url, timeout=10)
    except httpx.TransportError as exc:  # refused, timed out, no route: nothing to disturb
        return PrinterState("cthulhu", False, f"Cthulhu unreachable ({type(exc).__name__})", False)
    except httpx.HTTPError as exc:
        return unreadable(type(exc).__name__)
    if resp.status_code != 200:
        return unreadable(f"HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        return unreadable("unparsable response")
    if not isinstance(data, dict):
        return unreadable("unexpected payload")
    if not data.get("connected"):
        return PrinterState("cthulhu", False, "Cthulhu not connected", True)
    job = data.get("print")
    if job is not None and not isinstance(job, dict):
        return unreadable("bad status payload")
    label = job.get("statusLabel") if job is not None else None
    if label is None:
        label = ""
    if not isinstance(label, str):
        return unreadable("bad status payload")
    if label in CTHULHU_IDLE:
        return PrinterState("cthulhu", False, f"Cthulhu: {label or 'Idle'}", True)
    return PrinterState("cthulhu", True, f"Cthulhu: {label}", True)


async def octoprint_state(ssh, host: HostSpec, api_key: str | None) -> PrinterState:
    """`ssh` is an async callable (host, command, timeout=, stdin=) -> RunResult.

    The key goes to Leia on ssh's stdin and never appears in argv or in a detail.
    """

    def unreachable(why: str) -> PrinterState:
        return PrinterState("octoprint", False, f"OctoPrint unreachable ({why})", False)

    def unreadable(why: str) -> PrinterState:
        return PrinterState("octoprint", True, f"OctoPrint state unreadable: {why}", True)

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

    # OctoPrint is up from here on: anything we cannot read holds runs.
    if not api_key:
        return unreadable("no API key")
    job = await ssh(host, _JOB_COMMAND, timeout=30, stdin=(api_key + "\n").encode())
    if job.rc == SSH_UNREACHABLE:
        return unreachable(f"ssh rc {job.rc}")
    if job.rc != 0:
        return unreadable(f"job query exited {job.rc}")
    try:
        data = json.loads(job.stdout)
    except ValueError:
        return unreadable("unparsable response")
    state = data.get("state") if isinstance(data, dict) else None
    if not isinstance(state, str):  # a 401/403 body has no state
        return unreadable("no state in the response (wrong API key?)")
    if state in OCTOPRINT_BUSY:
        return PrinterState("octoprint", True, f"Prusa: {state}", True)
    return PrinterState("octoprint", False, f"Prusa: {state}", True)


class Probes:
    """Injectable bundle of the printer probes the gates consult.

    `key_resolver` is an async callable returning the OctoPrint API key or None. It is
    called on first use and, while the key is missing, again at most every
    KEY_RETRY_SECONDS, so a key that was unavailable at start-up is picked up later.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        leia: HostSpec,
        ssh,
        key_resolver: Callable[[], Awaitable[str | None]],
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._leia = leia
        self._ssh = ssh
        self._resolve = key_resolver
        self._clock = clock
        self._key: str | None = None
        self._asked_at: float | None = None

    async def _api_key(self) -> str | None:
        now = self._clock()
        if self._key is None and (
            self._asked_at is None or now - self._asked_at >= KEY_RETRY_SECONDS
        ):
            self._asked_at = now
            try:
                self._key = await self._resolve() or None
            except Exception as exc:  # noqa: BLE001 — no key just means "unreadable"
                logger.warning("OctoPrint key resolution failed (%s)", type(exc).__name__)
        return self._key

    async def states(self) -> list[PrinterState]:
        return [
            await cthulhu_state(self._client),
            await octoprint_state(self._ssh, self._leia, await self._api_key()),
        ]
