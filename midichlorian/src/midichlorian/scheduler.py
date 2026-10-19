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


def widen_tags(a: list[str] | None, b: list[str] | None) -> list[str] | None:
    """Union of two tag sets; None (a full run) wins."""
    if a is None or b is None:
        return None
    return sorted(set(a) | set(b))


def merge_target(
    queued: list[Job], host: str, want_tags: list[str] | None
) -> Job | None:
    """The job a new request for `host` may merge into, or None.

    Only queued/waiting jobs, never running ones, and only the first host of
    their request (merging into a mid-chain job would make an independent
    request wait on someone else's earlier hosts). Oldest wins.
    """
    candidates = [
        j for j in queued if j.host == host and j.state in _PENDING and j.position == 0
    ]
    return min(candidates, key=lambda j: j.id, default=None)


def next_runnable(jobs: list[Job]) -> tuple[list[Job], list[Job]]:
    """Return (runnable, to_skip).

    runnable: at most one job per host, oldest first, and none for a host that
    already has a running job. A job at position > 0 is only eligible once every
    earlier job of its request has succeeded or been found already deployed.
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
    seen = set(busy)
    for j in eligible:
        if j.host not in seen:
            seen.add(j.host)
            runnable.append(j)
    return runnable, to_skip
