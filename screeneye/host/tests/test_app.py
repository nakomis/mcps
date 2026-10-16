import json
from datetime import UTC, datetime

import pytest
from starlette.testclient import TestClient

from screeneye_host.app import create_app, save_frame, slugify

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64 + b"\xff\xd9"
META = {"zoom": 2.5, "lens": "Back Camera", "ev": 0, "focus": [0.5, 0.5],
        "lensPosition": 0.45, "note": "BIOS boot menu", "device": "iPhone",
        "taken": "2026-10-16T12:00:00Z"}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(root=tmp_path, max_bytes=1024 * 1024))


def post(client, image=JPEG, meta=META, ctype="image/jpeg"):
    files = {}
    if image is not None:
        files["image"] = ("frame.jpg", image, ctype)
    data = {} if meta is None else {"meta": meta if isinstance(meta, str) else json.dumps(meta)}
    return client.post("/api/frames", files=files or None, data=data)


def test_post_writes_jpeg_and_sidecar(client, tmp_path):
    r = post(client)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["path"].startswith("screeneye/")
    assert body["name"].endswith("-bios-boot-menu.jpg")
    jpg = tmp_path / body["path"]
    assert jpg.read_bytes() == JPEG
    day = jpg.parent.name
    assert datetime.strptime(day, "%Y-%m-%d")
    side = json.loads(jpg.with_suffix(".json").read_text())
    assert side["zoom"] == 2.5 and side["note"] == "BIOS boot menu"
    assert side["client"] == "testclient"
    assert side["name"] == body["name"]
    assert side["bytes"] == len(JPEG)
    assert datetime.fromisoformat(side["received"]).tzinfo is not None


def test_no_note_gives_bare_time(client):
    r = post(client, meta={**META, "note": ""})
    assert r.status_code == 201
    assert len(r.json()["name"]) == len("HHMMSS.jpg")


def test_never_overwrites(tmp_path):
    now = datetime(2026, 10, 16, 12, 34, 56, tzinfo=UTC)
    names = [save_frame(tmp_path, JPEG + bytes([i]), {"i": i}, "x", now).name for i in range(3)]
    assert names == ["123456-x.jpg", "123456-x-2.jpg", "123456-x-3.jpg"]
    day = tmp_path / "screeneye" / "2026-10-16"
    for i, n in enumerate(names):
        assert (day / n).read_bytes() == JPEG + bytes([i])
        assert json.loads((day / n).with_suffix(".json").read_text())["i"] == i


def test_lone_sidecar_is_not_overwritten(tmp_path):
    now = datetime(2026, 10, 16, 1, 2, 3)
    day = tmp_path / "screeneye" / "2026-10-16"
    day.mkdir(parents=True)
    (day / "010203.json").write_text("{}")
    assert save_frame(tmp_path, JPEG, {}, None, now).name == "010203-2.jpg"
    assert (day / "010203.json").read_text() == "{}"


@pytest.mark.parametrize("note,slug", [
    ("BIOS boot menu", "bios-boot-menu"),
    ("  Café / ünïcode!! ", "cafe-unicode"),
    ("../../etc/passwd", "etc-passwd"),
    ("😀", ""),
    (None, ""),
    ("a" * 100, "a" * 60),
])
def test_slugify(note, slug):
    assert slugify(note) == slug


def test_rejects_non_jpeg_bytes(client, tmp_path):
    r = post(client, image=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    assert r.status_code == 415 and "error" in r.json()
    assert not (tmp_path / "screeneye").exists()


def test_rejects_wrong_content_type(client):
    assert post(client, ctype="image/png").status_code == 415


def test_rejects_oversize(tmp_path):
    c = TestClient(create_app(root=tmp_path, max_bytes=1000))
    r = post(c, image=JPEG + b"\x00" * 2000)
    assert r.status_code == 413 and "error" in r.json()
    assert not (tmp_path / "screeneye").exists()


def test_rejects_missing_image(client):
    r = post(client, image=None)
    assert r.status_code in (400, 415) and "error" in r.json()


def test_rejects_missing_meta(client):
    assert post(client, meta=None).status_code == 400


@pytest.mark.parametrize("meta", ["{not json", "[1, 2]"])
def test_rejects_bad_meta(client, meta):
    r = post(client, meta=meta)
    assert r.status_code == 400 and "error" in r.json()


def test_rejects_non_multipart(client):
    r = client.post("/api/frames", content=JPEG, headers={"content-type": "image/jpeg"})
    assert r.status_code == 415


def test_unwritable_root_is_503(tmp_path):
    root = tmp_path / "ro"
    root.write_text("a file, not a directory")
    r = post(TestClient(create_app(root=root)))
    assert r.status_code == 503 and "error" in r.json()


def test_health(client, tmp_path):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["root"] == str(tmp_path)


def test_rejects_malformed_multipart(client):
    r = client.post("/api/frames", content=b"--x\r\nnonsense",
                    headers={"content-type": "multipart/form-data; boundary=x"})
    assert r.status_code == 400 and "error" in r.json()
