"""Shared fixtures. No test touches the network or real secrets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cabal import secrets


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    # Keep trace logs out of /Volumes/lru and fake every secret lookup.
    monkeypatch.setenv("CABAL_TRACE_DIR", str(tmp_path / "trace"))
    monkeypatch.setattr(secrets, "get", lambda name: SimpleNamespace(value="fake", source="test"))
    monkeypatch.setattr(secrets, "get_optional", lambda name: None)


@pytest.fixture
def anyio_backend():
    return "asyncio"
