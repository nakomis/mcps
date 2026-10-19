from pathlib import Path

import pytest

from midichlorian.config import load_settings

BASE = {
    "MIDICHLORIAN_INSTANCE": "cere",
    "MIDICHLORIAN_SERVED": "luke,leia",
    "MIDICHLORIAN_DATA_DIR": "/d",
    "MIDICHLORIAN_REPO_DIR": "/r",
    "MIDICHLORIAN_TOKEN": "t",
}


def test_defaults_and_parsing():
    s = load_settings(
        {**BASE, "MIDICHLORIAN_PEERS": "mace=http://mace.local:9140, x=http://x"}
    )
    assert s.instance == "cere"
    assert s.served == frozenset({"luke", "leia"})
    assert s.data_dir == Path("/d") and s.repo_dir == Path("/r")
    assert s.repo_url == "https://github.com/nakomis/home-infra.git"
    assert s.peers == {"mace": "http://mace.local:9140", "x": "http://x"}
    assert (s.bind, s.port) == ("0.0.0.0", 9140)


def test_no_peers_and_overrides():
    s = load_settings(
        {**BASE, "MIDICHLORIAN_BIND": "127.0.0.1", "MIDICHLORIAN_PORT": "1"}
    )
    assert s.peers == {} and s.bind == "127.0.0.1" and s.port == 1


def test_refuses_self_service():
    with pytest.raises(ValueError):
        load_settings({**BASE, "MIDICHLORIAN_SERVED": "cere"})


def test_missing_required():
    with pytest.raises(ValueError):
        load_settings({})


def test_served_names_must_be_known_hosts():
    with pytest.raises(ValueError, match="unknown hosts in MIDICHLORIAN_SERVED: luek"):
        load_settings({**BASE, "MIDICHLORIAN_SERVED": "luek,leia"})


def test_peer_names_need_not_be_hosts():
    s = load_settings({**BASE, "MIDICHLORIAN_PEERS": "elsewhere=http://e"})
    assert s.peers == {"elsewhere": "http://e"}
