"""Pure scheduling rules: what may merge, what may run, what must be skipped.

No I/O and no store access; callers pass lists of `Job`.
"""

from midichlorian.store import Job

_PENDING = ("queued", "waiting")
_PASSED = ("succeeded", "already")
_BROKEN = ("failed", "cancelled", "interrupted", "skipped")


def covers(
    done_sha_is_descendant: bool,
    done_tags: list[str] | None,
    want_tags: list[str] | None,
) -> bool:
    """Does an earlier successful deploy already satisfy a wanted one?

    A full run (tags None) covers anything; a tagged run covers only a tagged
    request whose tags are a subset of what was done.
    """
    if not done_sha_is_descendant:
        return False
    if done_tags is None:
        return True
    if want_tags is None:
        return False
    return set(want_tags) <= set(done_tags)


def merge_target(
    queued: list[Job], host: str, want_tags: list[str] | None
) -> Job | None:
    """The job a new request for `host` may merge into, or None.

    Only queued/waiting jobs, never running ones, and only the first host of
    their request (merging into a mid-chain job would make an independent
    request wait on someone else's earlier hosts). Oldest wins. `want_tags` is
    not checked here: the caller widens the tags (any queued job can absorb more).
    """
    candidates = [
        j for j in queued if j.host == host and j.state in _PENDING and j.position == 0
    ]
    return min(candidates, key=lambda j: j.id, default=None)


def _printer_held(j: Job) -> bool:
    return j.state == "waiting" and str(j.detail.get("gate", "")).startswith("printer busy")


def next_runnable(jobs: list[Job]) -> tuple[list[Job], list[Job]]:
    """Return (runnable, to_skip).

    runnable: the oldest eligible job per host, and none for a host that already has
    a running job. A job at position > 0 is only eligible once every earlier job of
    its request has succeeded or been found already deployed. One exception to
    per-host FIFO: when a host's head job is waiting on the printer gate, the oldest
    eligible override_printer job for that host is offered too, ahead of it (Martin
    authorised that run; it should not sit behind a print he chose to run through).
    Callers start at most one job per host.
    Caller contract: every non-terminal earlier-position job of a request must be
    in the input; earlier positions that are missing count as satisfied.
    to_skip: queued/waiting jobs whose request has an earlier failed, cancelled,
    interrupted or skipped job.
    """
    by_request: dict[int, list[Job]] = {}
    for j in jobs:
        by_request.setdefault(j.request_id, []).append(j)

    busy = {j.host for j in jobs if j.state == "running"}
    to_skip: list[Job] = []
    eligible: list[Job] = []
    for j in sorted(jobs, key=lambda j: j.id):
        if j.state not in _PENDING:
            continue
        earlier = [e for e in by_request[j.request_id] if e.position < j.position]
        if any(e.state in _BROKEN for e in earlier):
            to_skip.append(j)
        elif all(e.state in _PASSED for e in earlier):
            eligible.append(j)

    runnable: list[Job] = []
    heads: dict[str, Job] = {}
    passed: set[str] = set()
    for j in eligible:
        if j.host in busy:
            continue
        head = heads.get(j.host)
        if head is None:
            heads[j.host] = j
            runnable.append(j)
        elif j.override_printer and _printer_held(head) and j.host not in passed:
            passed.add(j.host)
            runnable.insert(runnable.index(head), j)
    return runnable, to_skip


_ACTIVE = ("queued", "waiting", "running")


def deploys(j: Job) -> str | None:
    """Which commit an active job will deploy: "pinned", "main@start" or "first-host".

    An unpinned job at position 0 deploys origin/main's head when it starts, not
    the sha recorded at request time; a later host of a request deploys whatever
    the request's first host did. None for a job that is no longer pending/running.
    """
    if j.state not in _ACTIVE:
        return None
    if j.detail.get("pinned_sha"):
        return "pinned"
    return "main@start" if j.position == 0 else "first-host"


def coverage(jobs: list[Job]) -> dict[int, int]:
    """Map each covered job id to the id of the earlier job that will cover it.

    Display only; scheduling ignores it. An earlier unpinned position-0 job X on
    the same host (queued, waiting or running) deploys main at its start, so a
    later queued/waiting unpinned Y whose tags X's cover will find its commit
    already deployed and end "already" — unless X fails. X's main must be at
    least as new as Y's request-time commit: X has not started, or started at or
    after Y was created. A later host of a request (position > 0) deploys what its
    first host deployed, which may be newer than Y's request: then X must not have
    started, or have started at or after that first host did (if the first host is
    not in `jobs` it has finished, so only an unstarted X will do). The earliest
    qualifying X wins (same order as the queue: by id).
    """
    out: dict[int, int] = {}
    ordered = sorted(jobs, key=lambda j: j.id)
    first = {j.request_id: j for j in ordered if j.position == 0}
    for y in ordered:
        if y.state not in _PENDING or y.detail.get("pinned_sha"):
            continue
        if y.position == 0:
            since = y.created
        else:
            head = first.get(y.request_id)
            since = head.started if head else None  # None: only an unstarted X
        for x in ordered:
            if x.id >= y.id:
                break
            if (
                x.host == y.host
                and x.state in _ACTIVE
                and x.position == 0
                and not x.detail.get("pinned_sha")
                and covers(True, x.tags, y.tags)
                and (x.started is None or (since is not None and x.started >= since))
            ):
                out[y.id] = x.id
                break
    return out
