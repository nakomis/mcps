import pytest

from taiga_mcp import server


@pytest.fixture(autouse=True)
def _taiga_env(monkeypatch):
    """Every test gets a working auth token without hitting the network."""
    monkeypatch.setenv("TAIGA_URL", "https://taiga.example.test")
    monkeypatch.setenv("TAIGA_AUTH_TOKEN", "test-token")
    server._token = ""
    yield
    server._token = ""
