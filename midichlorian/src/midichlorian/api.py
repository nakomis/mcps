"""The HTTP API shared by the web UI and the MCP, and the server entry point.

Every instance serves its own hosts. An instance with peers (mace has cere) merges
the peers' GET results into its own, so the UI and the MCP see the whole estate from
one place. Job ids are per instance; every job in a response carries `instance`, and
`?instance=<name>` on the per-run endpoints proxies to that peer.
"""

import asyncio
import contextlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from time import monotonic

import httpx
import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from midichlorian import remote
from midichlorian.config import Settings, load_settings
from midichlorian.hosts import HOSTS
from midichlorian.printers import Probes
from midichlorian.runner import AnsibleRunner
from midichlorian.store import Store
from midichlorian.worker import Coordinator

logger = logging.getLogger(__name__)

PEER_TIMEOUT = 5.0
PRINTER_CACHE_SECONDS = 30.0
SERVING_CACHE_SECONDS = 60.0
LOG_PAGE_BYTES = 256 * 1024
LOG_POLL_SECONDS = 0.5
LOG_FOLLOW_CHUNK = 256 * 1024
HEARTBEAT_SECONDS = 15
WEB_DIR = Path(__file__).parent / "web"
STATIC_FILES = {
    "/": "index.html",
    "/app.js": "app.js",
    "/style.css": "style.css",
    "/favicon.ico": "favicon.ico",
    "/favicon.svg": "favicon.svg",
    "/favicon.png": "favicon.png",
}
_ACTIVE = ["queued", "waiting", "running"]
_PENDING = ["queued", "waiting"]
_FINISHED = ["succeeded", "failed", "skipped", "cancelled", "interrupted", "already"]
_TERMINAL = frozenset(_FINISHED)
_monotonic = monotonic  # indirection so tests can move the clock


class DeployRequest(BaseModel):
    hosts: list[str]
    sha: str
    tags: list[str] | None = None
    override_printer: bool = False
    requester: str


def _read_from(path: str, offset: int, limit: int) -> bytes:
    """Up to `limit` bytes of the file at `offset` (blocking: run it in a thread)."""
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(limit)
    except FileNotFoundError:
        return b""


def _version() -> str:
    try:
        return metadata.version("midichlorian")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def _job_dict(job, instance: str) -> dict:
    return {**asdict(job), "instance": instance}


def _summary(job) -> dict:
    return {
        "id": job.id,
        "state": job.state,
        "sha": job.sha,
        "tags": job.tags,
        "finished": job.finished,
        "reason": job.detail.get("reason"),
    }


class _Peers:
    """Calls to the other instances: bearer token, short timeout, failures contained."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self._urls = dict(settings.peers)
        self._client = client
        self._headers = {"Authorization": f"Bearer {settings.token}"}
        self._serving: dict[str, str] = {}  # host -> instance
        self._serving_at: dict[str, float] = {}  # instance -> when its /api/info was read

    def __bool__(self) -> bool:
        return bool(self._urls)

    def known(self, name: str) -> bool:
        return name in self._urls

    async def request(self, name: str, method: str, path: str, params: dict | None = None):
        return await self._client.request(
            method,
            self._urls[name].rstrip("/") + path,
            params=params,
            headers=self._headers,
            timeout=PEER_TIMEOUT,
        )

    async def get_json(self, name: str, path: str, params: dict | None = None):
        """The peer's JSON body, or None when it is down, rejects us or sends junk."""
        try:
            resp = await self.request(name, "GET", path, params)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("peer %s: GET %s failed (%s)", name, path, type(exc).__name__)
            return None

    async def collect(self, path: str, key: str, params: dict | None = None):
        """(items from every reachable peer, names of the unreachable ones)."""
        names = list(self._urls)
        bodies = await asyncio.gather(
            *(self.get_json(n, path, {**(params or {}), "local": "1"}) for n in names)
        )
        items, down = [], []
        for name, body in zip(names, bodies, strict=True):
            items_ok = isinstance(body, dict) and isinstance(body.get(key), list)
            if items_ok and all(isinstance(i, dict) for i in body[key]):
                items.extend(body[key])
            else:
                down.append(name)
        return items, down

    async def serving(self) -> dict[str, str]:
        """Which peer serves which host, from each peer's /api/info (cached)."""
        now = _monotonic()
        stale = [n for n in self._urls if now - self._serving_at.get(n, -1e9) > SERVING_CACHE_SECONDS]
        bodies = await asyncio.gather(*(self.get_json(n, "/api/info") for n in stale))
        for name, body in zip(stale, bodies, strict=True):
            if isinstance(body, dict) and isinstance(body.get("served"), list):
                self._serving = {h: i for h, i in self._serving.items() if i != name}
                self._serving.update({h: name for h in body["served"]})
                self._serving_at[name] = now
        return dict(self._serving)


class _PrinterCache:
    """The printers' state, probed at most once per PRINTER_CACHE_SECONDS."""

    def __init__(self, probes):
        self._probes = probes
        self._lock = asyncio.Lock()
        self._at: float | None = None
        self._value: tuple[list[dict], str | None] = ([], None)

    async def get(self) -> tuple[list[dict], str | None]:
        async with self._lock:
            now = _monotonic()
            if self._at is None or now - self._at > PRINTER_CACHE_SECONDS:
                try:
                    states = [asdict(s) for s in await self._probes.states()]
                    self._value = (states, None)
                except Exception as exc:  # noqa: BLE001 — reported in /api/hosts, never raised
                    logger.warning("printer probe failed: %s", type(exc).__name__)
                    self._value = ([], f"{type(exc).__name__}: {exc}")
                self._at = now
            return self._value


def create_app(
    settings: Settings,
    coordinator,
    peers_client: httpx.AsyncClient,
    web_dir: Path | None = None,
) -> FastAPI:
    store = coordinator.store
    instance = settings.instance
    peers = _Peers(settings, peers_client)
    printers = _PrinterCache(coordinator.probes)
    web = web_dir or WEB_DIR
    token = settings.token.encode()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Shutdown stops scheduling only. Running playbooks are never signalled: they
        # finish in their own sessions and a restart records them `interrupted` (R27).
        task = asyncio.create_task(coordinator.run_forever())
        task.add_done_callback(scheduler_ended)
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):  # logged already
                await task

    def scheduler_ended(task: asyncio.Task) -> None:
        """run_forever never returns: if it ends, scheduling has stopped. Say so, and
        fail /healthz so the supervisor restarts us."""
        if task.cancelled():
            return
        health["scheduler"] = False
        exc = task.exception()
        logger.error("the scheduler stopped: %r", exc, exc_info=exc)

    health = {"scheduler": True}
    # No /docs, /redoc or /openapi.json: they would be served without auth.
    app = FastAPI(
        title="midichlorian", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    def authorised(request: Request) -> None:
        supplied = [request.headers.get("x-midichlorian-token", "")]
        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            supplied.append(bearer.strip())
        if not any(hmac.compare_digest(s.encode(), token) for s in supplied if s):
            raise HTTPException(401, "missing or invalid token")

    api = APIRouter(prefix="/api", dependencies=[Depends(authorised)])

    def local_job(job_id: int):
        job = store.get_job(job_id)
        if job is None:
            raise HTTPException(404, f"no run {job_id} on {instance}")
        return job

    def target(name: str | None) -> str | None:
        """None for this instance, else the peer's name (404 if there is no such peer)."""
        if not name or name == instance:
            return None
        if not peers.known(name):
            raise HTTPException(404, f"unknown instance {name}")
        return name

    async def proxy(peer: str, method: str, path: str, params: dict | None = None):
        try:
            resp = await peers.request(peer, method, path, params)
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"{peer} unreachable ({type(exc).__name__})") from exc
        try:
            body = resp.json()
        except ValueError:
            raise HTTPException(502, f"{peer} sent an unreadable reply") from None
        return JSONResponse(body, status_code=resp.status_code)

    # --- reads -----------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz():
        if not health["scheduler"]:
            return JSONResponse({"ok": False, "error": "scheduler stopped"}, status_code=503)
        return {"ok": True}

    @api.get("/info")
    async def info():
        return {"instance": instance, "served": sorted(settings.served), "version": _version()}

    @api.get("/hosts")
    async def hosts(local: bool = False):
        states, states_error = await printers.get()
        out = []
        for name in sorted(settings.served, key=list(HOSTS).index):
            spec = HOSTS[name]
            running = store.jobs(host=name, states=["running"], limit=1)
            last = store.jobs(host=name, states=_FINISHED, limit=1)
            gates = {
                "printer_sensitive": spec.printer_sensitive,
                "printers": states,
                "luke_backups": "not reported in v1",
            }
            if states_error:
                gates["printers_error"] = states_error
            out.append(
                {
                    "name": name,
                    "served_by": instance,
                    "deployed": store.deployed(name),
                    "last": _summary(last[0]) if last else None,
                    "running": running[0].id if running else None,
                    "queued": len(store.jobs(host=name, states=_PENDING, limit=10_000)),
                    "gates": gates,
                }
            )
        down: list[str] = []
        if not local and peers:
            more, down = await peers.collect("/api/hosts", "hosts")
            out.extend(more)
        return {"hosts": out, "peers_unreachable": down}

    @api.get("/queue")
    async def queue(local: bool = False):
        jobs = store.jobs(states=_ACTIVE, limit=10_000)
        out = [_job_dict(j, instance) for j in sorted(jobs, key=lambda j: j.id)]
        out.sort(key=lambda j: j["state"] != "running")  # stable: running first
        down: list[str] = []
        if not local and peers:
            more, down = await peers.collect("/api/queue", "jobs")
            out.extend(more)
        return {"jobs": out, "peers_unreachable": down}

    @api.get("/runs")
    async def runs(
        host: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        local: bool = False,
    ):
        out = [_job_dict(j, instance) for j in store.jobs(host=host, limit=limit)]
        down: list[str] = []
        if not local and peers:
            params = {"limit": limit, **({"host": host} if host else {})}
            more, down = await peers.collect("/api/runs", "runs", params)
            out.extend(more)
            out.sort(key=lambda r: str(r.get("created", "")), reverse=True)  # stable
            out = out[:limit]
        return {"runs": out, "peers_unreachable": down}

    @api.get("/runs/{job_id}")
    async def run_detail(job_id: int, instance_name: str | None = Query(None, alias="instance")):
        peer = target(instance_name)
        if peer:
            return await proxy(peer, "GET", f"/api/runs/{job_id}")
        return _job_dict(local_job(job_id), instance)

    @api.get("/runs/{job_id}/log")
    async def run_log(
        job_id: int,
        since: int = Query(0, ge=0),
        follow: bool = False,
        instance_name: str | None = Query(None, alias="instance"),
    ):
        peer = target(instance_name)
        if peer:
            if follow:
                raise HTTPException(400, "follow is not proxied: stream from the serving instance")
            return await proxy(peer, "GET", f"/api/runs/{job_id}/log", {"since": since})
        job = local_job(job_id)
        if follow:
            return EventSourceResponse(_follow(job_id, since), ping=HEARTBEAT_SECONDS)
        text, nxt = _read_page(job.log_path, since)
        return {"text": text, "next": nxt}

    def _read_page(log_path: str | None, since: int) -> tuple[str, int]:
        if not log_path:
            return "", since
        try:
            with open(log_path, "rb") as f:
                f.seek(since)
                raw = f.read(LOG_PAGE_BYTES + 1)
        except FileNotFoundError:
            return "", since
        if len(raw) > LOG_PAGE_BYTES:  # a long log: cut at a line end where there is one
            raw = raw[:LOG_PAGE_BYTES]
            cut = raw.rfind(b"\n")
            if cut != -1:
                raw = raw[: cut + 1]
        return raw.decode("utf-8", errors="replace"), since + len(raw)

    async def _follow(job_id: int, since: int):
        """Existing log lines, then appended ones, until the job is terminal and fully read."""
        offset, pending = since, b""
        while True:
            job = store.get_job(job_id)
            finished = job is None or job.state in _TERMINAL
            data = b""
            if job is not None and job.log_path:
                # bounded, and off the event loop: a long log never stalls the server
                data = await asyncio.to_thread(
                    _read_from, job.log_path, offset, LOG_FOLLOW_CHUNK
                )
            more = len(data) == LOG_FOLLOW_CHUNK  # there may be more to read at once
            offset += len(data)
            pending += data
            *lines, pending = pending.split(b"\n")
            done = finished and not more
            if done and pending:
                lines.append(pending)
                pending = b""
            for line in lines:
                yield {"data": line.decode("utf-8", errors="replace").rstrip("\r")}
            if done:
                yield {"event": "end", "data": job.state if job else "gone"}
                return
            if not more:
                await asyncio.sleep(LOG_POLL_SECONDS)

    # --- writes ----------------------------------------------------------------------

    @api.post("/deploy")
    async def deploy(body: DeployRequest):
        requester = body.requester.strip()
        if not requester:
            raise HTTPException(400, "requester must not be empty")
        unknown = [h for h in body.hosts if h not in HOSTS]
        if unknown:
            raise HTTPException(400, f"unknown hosts: {', '.join(unknown)}")
        elsewhere = [h for h in body.hosts if h not in settings.served]
        if elsewhere:
            serving = await peers.serving() if peers else {}
            where = {h: serving.get(h) for h in elsewhere}
            named = ", ".join(f"{h} (served by {i or 'no known instance'})" for h, i in where.items())
            if len(elsewhere) < len(body.hosts):
                raise HTTPException(
                    400, f"hosts span instances; send each instance its own request: {named}"
                    f" and the rest on {instance}"
                )
            if len({i for i in where.values()}) > 1:
                raise HTTPException(
                    400, f"hosts span instances; send each instance its own request: {named}"
                )
            raise HTTPException(400, f"not served by {instance}: {named}")
        try:
            result = await coordinator.submit(
                body.hosts, body.sha, body.tags, body.override_printer, requester
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return asdict(result)

    @api.post("/runs/{job_id}/cancel")
    async def cancel(job_id: int, instance_name: str | None = Query(None, alias="instance")):
        peer = target(instance_name)
        if peer:
            return await proxy(peer, "POST", f"/api/runs/{job_id}/cancel")
        local_job(job_id)
        if not coordinator.cancel(job_id):
            raise HTTPException(409, "only a queued or waiting run can be cancelled")
        return _job_dict(store.get_job(job_id), instance)

    app.include_router(api)

    # --- the web UI (no auth: Leia's mTLS fronts it and injects the token) -------------

    def static_route(filename: str):
        async def serve():
            path = web / filename
            if not path.is_file():
                raise HTTPException(404)
            return FileResponse(path)

        return serve

    for url, filename in STATIC_FILES.items():
        app.add_api_route(url, static_route(filename), methods=["GET"], include_in_schema=False)

    return app


# --- entry point -----------------------------------------------------------------------

OCTOPRINT_SSM = [
    "aws", "ssm", "get-parameter",
    "--name", "/octoprint/api-key",
    "--with-decryption",
    "--query", "Parameter.Value",
    "--output", "text",
    "--profile", "nakom.is-admin",
    "--region", "eu-west-2",
]


async def resolve_octoprint_key(env=os.environ, run=remote.run) -> str | None:
    """The OctoPrint API key: from the environment, else SSM. None (with a warning that
    never contains the key) when it cannot be had; while OctoPrint is running, the probe
    then reports its state unreadable, which holds Leia's runs (R25)."""
    key = env.get("MIDICHLORIAN_OCTOPRINT_KEY", "").strip()
    if key:
        return key
    result = await run(OCTOPRINT_SSM, timeout=30, env=dict(env))
    key = result.stdout.strip()
    if result.rc != 0 or not key:
        why = result.stderr.strip() or f"rc {result.rc}"
        logger.warning("OctoPrint API key unavailable, Prusa state unreadable: %s", why)
        return None
    return key


def peer_running_check(settings: Settings, client: httpx.AsyncClient):
    """The Coordinator's `peer_running`: does the peer instance `name` have a running
    job? None when it cannot be asked (down, rejecting us, junk): the caller proceeds."""
    headers = {"Authorization": f"Bearer {settings.token}"}

    async def check(name: str) -> bool | None:
        url = settings.peers[name].rstrip("/") + "/api/queue"
        try:
            resp = await client.get(url, params={"local": "1"}, headers=headers, timeout=PEER_TIMEOUT)
            resp.raise_for_status()
            jobs = resp.json().get("jobs")
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            logger.warning("peer %s: queue check failed (%s)", name, type(exc).__name__)
            return None
        if not isinstance(jobs, list):
            return None
        return any(isinstance(j, dict) and j.get("state") == "running" for j in jobs)

    return check


def build_app(settings: Settings, octoprint_key=resolve_octoprint_key) -> FastAPI:
    """`octoprint_key` is the async key resolver; Probes calls it lazily, and again while
    the key is missing."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "midichlorian.db")
    probes = Probes(
        httpx.AsyncClient(),
        HOSTS["leia"],
        remote.ssh,
        octoprint_key,
        cthulhu_url=settings.cthulhu_url,
    )
    coordinator = Coordinator(
        settings,
        store,
        AnsibleRunner(),
        probes,
        remote.ssh,
        lambda: datetime.now(UTC),
        peer_running=peer_running_check(settings, httpx.AsyncClient()),
    )
    return create_app(settings, coordinator, httpx.AsyncClient())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    uvicorn.run(build_app(settings), host=settings.bind, port=settings.port)
