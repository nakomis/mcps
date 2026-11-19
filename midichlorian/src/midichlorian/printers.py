"""Printer probe: Cthulhu (resin) over HTTP.

Two kinds of "can't tell" are treated differently (R25):

- the printer is genuinely unreachable or off (connection refused, timeout):
  idle, with a note, since there is nothing to disturb;
- it is there but its state cannot be read (an HTTP error, junk): busy
  ("state unreadable"), so runs wait. Only an override lifts that.
"""

from dataclasses import dataclass

import httpx

from midichlorian.config import DEFAULT_CTHULHU_URL

CTHULHU_IDLE = {"", "Idle", "Complete", "Stopped", "Cancelled"}


@dataclass(frozen=True)
class PrinterState:
    name: str
    busy: bool
    detail: str
    reachable: bool


async def cthulhu_state(
    client: httpx.AsyncClient, url: str = DEFAULT_CTHULHU_URL
) -> PrinterState:
    def unreadable(why: str) -> PrinterState:
        return PrinterState("cthulhu", True, f"Cthulhu state unreadable: {why}", True)

    try:
        resp = await client.get(url, timeout=10)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        # Nothing accepted the connection (refused, no route, no DNS, no answer):
        # nothing to disturb. Anything that fails after connecting is unreadable.
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


class Probes:
    """Injectable bundle of the printer probes the gates consult."""

    def __init__(
        self, client: httpx.AsyncClient, *, cthulhu_url: str = DEFAULT_CTHULHU_URL
    ):
        self._client = client
        self._cthulhu_url = cthulhu_url

    async def states(self) -> list[PrinterState]:
        return [await cthulhu_state(self._client, self._cthulhu_url)]
