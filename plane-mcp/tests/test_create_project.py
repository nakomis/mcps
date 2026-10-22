"""create_project membership (HOME-526): Martin and plane@ as admins, BAU as a member."""

from plane_mcp import server

P = {"id": "proj-1", "identifier": "NEW", "name": "New"}
MEMBERS = [{"member": {"id": f"u-{n}", "email": f"{n}@nakomis.com"}} for n in ("martin", "plane", "bau")]


def test_create_project_adds_admins_and_bau_member(monkeypatch):
    calls = []

    def request(method, path, *, params=None, json=None):
        calls.append((method, path, json))
        return dict(P) if path == "/projects/" else {}

    monkeypatch.setattr(server, "_request", request)
    monkeypatch.setattr(server, "_get_all", lambda path, **params: list(MEMBERS))
    monkeypatch.setattr(server, "_refresh_if_stale", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_project", lambda ident: P)
    monkeypatch.setattr(server, "get_project", lambda ident: {"identifier": ident})
    monkeypatch.setattr(server, "_sync_shortener_project", lambda p: {"synced": True})

    server.create_project("New", "new")

    posted = {c[2]["member"]: c[2]["role"] for c in calls if c[1] == "/projects/proj-1/project-members/"}
    assert posted == {"u-martin": 20, "u-plane": 20, "u-bau": 15}
