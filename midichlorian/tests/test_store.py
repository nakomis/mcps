import pytest

from midichlorian.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "m.db")


def make_job(store, host="luke", sha="a" * 40, tags=None, position=0, requester="martin", override=False):
    rid = store.create_request([host], sha, tags, override, requester)
    return store.add_job(rid, host, sha, tags, position, override, requester)


def test_round_trip(store):
    job = make_job(store, tags=["samba", "docker"], override=True)
    got = store.get_job(job.id)
    assert got == job
    assert got.state == "queued"
    assert got.tags == ["samba", "docker"]
    assert got.requesters == ["martin"]
    assert got.override_printer is True
    assert got.detail == {}
    assert got.started is None and got.result is None and got.log_path is None
    assert store.get_job(9999) is None


def test_full_run_tags_none(store):
    assert make_job(store, tags=None).tags is None


def test_request_jobs_share_request_and_position(store):
    rid = store.create_request(["luke", "leia"], "b" * 40, None, False, "claude")
    j0 = store.add_job(rid, "luke", "b" * 40, None, 0, False, "claude")
    j1 = store.add_job(rid, "leia", "b" * 40, None, 1, False, "claude")
    jobs = store.request_jobs(rid)
    assert [j.id for j in jobs] == [j0.id, j1.id]
    assert [j.position for j in jobs] == [0, 1]
    assert {j.request_id for j in jobs} == {rid}


def test_merge_requester_appends_without_duplicates(store):
    job = make_job(store, tags=["a"])
    store.merge_requester(job.id, "claude", None, ["a"])
    store.merge_requester(job.id, "claude", None, ["a"])
    assert store.get_job(job.id).requesters == ["martin", "claude"]


def test_merge_requester_widens_tags(store):
    job = make_job(store, tags=["b"])
    store.merge_requester(job.id, "x", None, ["a", "c"])
    got = store.get_job(job.id)
    assert got.tags == ["a", "b", "c"]
    assert got.sha == "a" * 40


def test_merge_requester_none_means_full(store):
    job = make_job(store, tags=["b"])
    store.merge_requester(job.id, "x", None, None)
    assert store.get_job(job.id).tags is None
    store.merge_requester(job.id, "y", None, ["z"])
    assert store.get_job(job.id).tags is None


def test_merge_requester_into_full_stays_full(store):
    job = make_job(store, tags=None)
    store.merge_requester(job.id, "x", None, ["a"])
    assert store.get_job(job.id).tags is None


def test_merge_requester_stores_given_sha(store):
    job = make_job(store)
    store.merge_requester(job.id, "x", "c" * 40, None)
    assert store.get_job(job.id).sha == "c" * 40


def test_set_state_fields(store):
    job = make_job(store)
    store.set_state(job.id, "running", started="t1", log_path="/l")
    store.set_state(job.id, "failed", finished="t2", result="boom", detail={"rc": 2})
    got = store.get_job(job.id)
    assert (got.state, got.started, got.finished, got.result, got.detail, got.log_path) == (
        "failed", "t1", "t2", "boom", {"rc": 2}, "/l")


def test_set_state_rejects_unknown(store):
    job = make_job(store)
    with pytest.raises(ValueError):
        store.set_state(job.id, "bogus")
    with pytest.raises(ValueError):
        store.set_state(job.id, "running", nonsense=1)
    assert store.get_job(job.id).state == "queued"


def test_jobs_filters_newest_first(store):
    a = make_job(store, host="luke")
    b = make_job(store, host="leia")
    c = make_job(store, host="luke")
    store.set_state(a.id, "succeeded")
    assert [j.id for j in store.jobs()] == [c.id, b.id, a.id]
    assert [j.id for j in store.jobs(host="luke")] == [c.id, a.id]
    assert [j.id for j in store.jobs(states=["queued"])] == [c.id, b.id]
    assert [j.id for j in store.jobs(host="luke", states=["queued", "succeeded"])] == [c.id, a.id]
    assert [j.id for j in store.jobs(limit=1)] == [c.id]


def test_recover_interrupted(store):
    a = make_job(store)
    b = make_job(store)
    c = make_job(store)
    store.set_state(a.id, "running")
    store.set_state(b.id, "running")
    store.set_state(c.id, "succeeded")
    assert sorted(store.recover_interrupted()) == [a.id, b.id]
    for i in (a.id, b.id):
        got = store.get_job(i)
        assert got.state == "interrupted" and got.finished is not None
    assert store.get_job(c.id).state == "succeeded"
    assert store.recover_interrupted() == []


def test_deployed_none_when_empty(store):
    assert store.deployed("luke") is None


def test_deployed_latest_full_and_per_tag_set(store):
    store.record_deploy("luke", "s1", None, "2026-01-01T00:00:00Z")
    store.record_deploy("luke", "s2", ["b", "a"], "2026-01-02T00:00:00Z")
    store.record_deploy("luke", "s3", None, "2026-01-03T00:00:00Z")
    store.record_deploy("luke", "s4", ["a", "b"], "2026-01-04T00:00:00Z")
    store.record_deploy("luke", "s5", ["c"], "2026-01-05T00:00:00Z")
    store.record_deploy("leia", "x", None, "2026-01-06T00:00:00Z")
    d = store.deployed("luke")
    assert d["full"] == {"sha": "s3", "at": "2026-01-03T00:00:00Z"}
    assert d["tagged"] == [
        {"sha": "s4", "tags": ["a", "b"], "at": "2026-01-04T00:00:00Z"},
        {"sha": "s5", "tags": ["c"], "at": "2026-01-05T00:00:00Z"},
    ]


def test_deployed_tagged_only(store):
    store.record_deploy("luke", "s1", ["a"], "2026-01-01T00:00:00Z")
    d = store.deployed("luke")
    assert d["full"] is None
    assert d["tagged"] == [{"sha": "s1", "tags": ["a"], "at": "2026-01-01T00:00:00Z"}]


def test_persists_across_instances(tmp_path):
    s1 = Store(tmp_path / "m.db")
    job = make_job(s1)
    assert Store(tmp_path / "m.db").get_job(job.id) == job
