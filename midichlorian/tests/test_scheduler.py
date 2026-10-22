from midichlorian.scheduler import (
    coverage,
    covers,
    deploys,
    merge_target,
    next_runnable,
)
from midichlorian.store import Job


def job(id, host="luke", state="queued", request=1, position=0, tags=None):
    return Job(
        id=id,
        request_id=request,
        host=host,
        sha="abc",
        tags=tags,
        state=state,
        position=position,
        created="2026-10-19T00:00:00Z",
    )


def chain(*states, request=1, start=1):
    hosts = ["luke", "leia", "mace"]
    return [job(start + i, hosts[i], s, request, i) for i, s in enumerate(states)]


def ids(jobs):
    return [j.id for j in jobs]


def test_covers():
    assert covers(True, None, ["a"])
    assert covers(True, None, None)
    assert covers(True, ["a", "b"], ["a"])
    assert not covers(True, ["a"], ["a", "b"])
    assert not covers(True, ["a"], None)
    assert not covers(False, None, None)


def test_merge_into_queued_not_running():
    assert merge_target([job(1, state="running")], "luke", None) is None
    assert merge_target([job(1, state="queued")], "luke", None).id == 1
    assert merge_target([job(1, state="waiting")], "luke", ["x"]).id == 1


def test_merge_oldest_position_zero_same_host_only():
    jobs = [
        job(1, host="leia"),
        job(2, request=2, position=1),
        job(4),
        job(3),
    ]
    assert merge_target(jobs, "luke", None).id == 3
    assert merge_target(jobs, "mace", None) is None


def test_running_host_blocks_and_second_queues_behind():
    runnable, skip = next_runnable([job(1, state="running"), job(2, request=2)])
    assert runnable == [] and skip == []


def test_fifo_per_host_and_hosts_concurrent():
    jobs = [job(2, request=2), job(1), job(3, host="cal", request=3)]
    runnable, _ = next_runnable(jobs)
    assert sorted(ids(runnable)) == [1, 3]


def test_waiting_job_is_head():
    runnable, _ = next_runnable([job(2, request=2), job(1, state="waiting")])
    assert ids(runnable) == [1]


def test_chain_runs_in_order():
    runnable, _ = next_runnable(chain("queued", "queued", "queued"))
    assert ids(runnable) == [1]
    runnable, _ = next_runnable(chain("succeeded", "queued", "queued"))
    assert ids(runnable) == [2]
    runnable, _ = next_runnable(chain("already", "succeeded", "queued"))
    assert ids(runnable) == [3]
    runnable, skip = next_runnable(chain("running", "queued", "queued"))
    assert runnable == [] and skip == []


def test_failed_earlier_skips_later():
    for bad in ("failed", "cancelled", "interrupted", "skipped"):
        runnable, skip = next_runnable(chain(bad, "queued", "waiting"))
        assert runnable == [] and ids(skip) == [2, 3]


def test_skipped_job_does_not_block_other_request_on_host():
    jobs = chain("failed", "queued", request=1) + [job(10, host="leia", request=2)]
    runnable, skip = next_runnable(jobs)
    assert ids(runnable) == [10] and ids(skip) == [2]


def test_chain_with_second_host_failed_skips_third_only():
    runnable, skip = next_runnable(chain("succeeded", "failed", "queued"))
    assert runnable == [] and ids(skip) == [3]


def test_to_skip_never_contains_running_or_succeeded():
    _, skip = next_runnable(chain("failed", "running", "succeeded"))
    assert skip == []


def test_waiting_job_at_later_position():
    runnable, skip = next_runnable(chain("succeeded", "waiting", "queued"))
    assert ids(runnable) == [2] and skip == []
    runnable, skip = next_runnable(chain("queued", "waiting"))
    assert ids(runnable) == [1] and skip == []


def test_merge_target_empty_and_ignores_later_position():
    assert merge_target([], "luke", None) is None
    assert merge_target([job(1, position=1)], "luke", None) is None


def test_fifo_among_eligible_blocked_lower_id_does_not_block():
    blocked = job(1, host="leia", request=1, position=1)
    first = job(0, host="luke", request=1, position=0, state="running")
    other = job(5, host="leia", request=2)
    runnable, _ = next_runnable([first, blocked, other])
    assert ids(runnable) == [5]


def printer_held(id, host="leia", request=1):
    j = job(id, host, "waiting", request)
    j.detail = {"gate": "printer busy — Cthulhu: Exposure"}
    return j


def override(id, host="leia", request=2, state="queued"):
    j = job(id, host, state, request)
    j.override_printer = True
    return j


def test_override_job_passes_a_head_job_held_for_a_printer():
    runnable, _ = next_runnable([printer_held(1), override(2)])
    assert ids(runnable) == [2, 1]  # the override is offered first


def test_only_one_override_passes_and_only_a_printer_hold():
    runnable, _ = next_runnable([printer_held(1), override(2), override(3, request=3)])
    assert ids(runnable) == [2, 1]
    peer_held = job(1, "mace", "waiting")
    peer_held.detail = {"gate": "peer mace has a running job"}
    runnable, _ = next_runnable([peer_held, override(2, host="mace")])
    assert ids(runnable) == [1]
    runnable, _ = next_runnable([job(1, "leia", "queued"), override(2)])
    assert ids(runnable) == [1]  # a merely queued head is not passed


def test_override_never_passes_a_running_job():
    runnable, _ = next_runnable([job(1, "leia", "running"), override(2)])
    assert runnable == []


def cj(id, created="2026-10-19T00:00:00Z", started=None, pinned=False, **kw):
    j = job(id, **kw)
    j.created = created
    j.started = started
    if pinned:
        j.detail = {"pinned_sha": "a" * 40}
    return j


def test_coverage_basic_later_queued_job_is_covered():
    assert coverage([cj(1, state="waiting"), cj(2)]) == {2: 1}


def test_coverage_pinned_earlier_job_does_not_cover():
    assert coverage([cj(1, pinned=True), cj(2)]) == {}


def test_coverage_pinned_later_job_is_not_covered():
    assert coverage([cj(1), cj(2, pinned=True)]) == {}


def test_coverage_later_position_job_does_not_cover():
    assert coverage([cj(1, position=1, request=2), cj(2)]) == {}


def test_coverage_tags():
    assert coverage([cj(1, tags=["a"]), cj(2)]) == {}  # narrower does not cover full
    assert coverage([cj(1, tags=["a", "b"]), cj(2, tags=["a"])]) == {2: 1}
    assert coverage([cj(1, tags=["a"]), cj(2, tags=["b"])]) == {}
    assert coverage([cj(1), cj(2, tags=["a"])]) == {2: 1}


def test_coverage_running_job_started_before_request_does_not_cover():
    x = cj(1, state="running", started="2026-10-19T00:00:00Z")
    assert coverage([x, cj(2, created="2026-10-19T00:05:00Z")]) == {}


def test_coverage_running_job_started_after_request_covers():
    x = cj(1, state="running", started="2026-10-19T00:10:00Z")
    assert coverage([x, cj(2, created="2026-10-19T00:05:00Z")]) == {2: 1}


def test_coverage_other_host_not_covered():
    assert coverage([cj(1, host="leia"), cj(2, host="luke")]) == {}


def test_coverage_earliest_of_several_wins():
    assert coverage([cj(1), cj(2), cj(3)]) == {2: 1, 3: 1}


def test_coverage_finished_jobs_neither_cover_nor_are_covered():
    assert coverage([cj(1, state="succeeded"), cj(2)]) == {}
    assert coverage([cj(1), cj(2, state="failed")]) == {}


def test_coverage_ignores_override_printer():
    y = cj(2)
    y.override_printer = True
    assert coverage([cj(1), y]) == {2: 1}


def test_deploys():
    assert deploys(cj(1)) == "main@start"
    assert deploys(cj(1, pinned=True)) == "pinned"
    assert deploys(cj(1, position=1)) == "first-host"
    assert deploys(cj(1, state="succeeded")) is None
