"""Environment-driven settings for one midichlorian instance."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from midichlorian.hosts import HOSTS

DEFAULT_REPO_URL = "https://github.com/nakomis/home-infra.git"
DEFAULT_CTHULHU_URL = "http://rey.local:9120/api/status"


@dataclass(frozen=True)
class Settings:
    instance: str
    served: frozenset[str]
    data_dir: Path
    repo_dir: Path
    repo_url: str
    token: str
    peers: dict[str, str]
    bind: str
    port: int
    cthulhu_url: str = DEFAULT_CTHULHU_URL


def _required(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _parse_peers(raw: str) -> dict[str, str]:
    peers: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, url = item.partition("=")
        if not sep or not name.strip() or not url.strip():
            raise ValueError(f"invalid MIDICHLORIAN_PEERS entry: {item!r}")
        peers[name.strip()] = url.strip()
    return peers


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    instance = _required(env, "MIDICHLORIAN_INSTANCE")
    served = frozenset(
        s.strip() for s in _required(env, "MIDICHLORIAN_SERVED").split(",") if s.strip()
    )
    unknown = sorted(served - set(HOSTS))
    if unknown:
        raise ValueError(f"unknown hosts in MIDICHLORIAN_SERVED: {', '.join(unknown)}")
    if instance in served:
        raise ValueError(f"instance {instance!r} must not serve itself")
    return Settings(
        instance=instance,
        served=served,
        data_dir=Path(_required(env, "MIDICHLORIAN_DATA_DIR")),
        repo_dir=Path(_required(env, "MIDICHLORIAN_REPO_DIR")),
        repo_url=env.get("MIDICHLORIAN_REPO_URL") or DEFAULT_REPO_URL,
        token=_required(env, "MIDICHLORIAN_TOKEN"),
        peers=_parse_peers(env.get("MIDICHLORIAN_PEERS", "")),
        bind=env.get("MIDICHLORIAN_BIND") or "0.0.0.0",
        port=int(env.get("MIDICHLORIAN_PORT") or 9140),
        cthulhu_url=env.get("MIDICHLORIAN_CTHULHU_URL") or DEFAULT_CTHULHU_URL,
    )
