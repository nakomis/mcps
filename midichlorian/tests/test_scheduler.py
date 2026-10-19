from midichlorian.scheduler import covers, merge_target, next_runnable, widen_tags
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


def test_widen_tags():
    assert widen_tags(["b"], ["a"]) == ["a", "b"]
    assert widen_tags(None, ["a"]) is None
    assert widen_tags(["a"], None) is None


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
