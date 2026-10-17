import io
import json
from datetime import UTC, datetime

import pytest
from PIL import Image
from starlette.testclient import TestClient

from screeneye_host.app import create_app, rotate_jpeg, rotation_of, save_frame, slugify

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


def test_failed_sidecar_removes_the_jpeg(tmp_path, monkeypatch):
    import builtins

    real_open = builtins.open

    def failing_open(path, mode="r", *a, **kw):
        if str(path).endswith(".json") and "x" in mode:
            raise OSError(28, "No space left on device")
        return real_open(path, mode, *a, **kw)

    monkeypatch.setattr(builtins, "open", failing_open)
    with pytest.raises(OSError):
        save_frame(tmp_path, JPEG, {}, None, datetime(2026, 10, 16, 1, 2, 3))
    assert list((tmp_path / "screeneye" / "2026-10-16").iterdir()) == []


def real_jpeg(size=(320, 180)) -> bytes:
    """A decodable JPEG: white, with a red block in the top-left corner."""
    img = Image.new("RGB", size, "white")
    img.paste((255, 0, 0), (0, 0, 40, 40))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def red_corner(data: bytes) -> tuple[str, tuple[int, int]]:
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    corners = {"tl": (5, 5), "tr": (w - 6, 5), "bl": (5, h - 6), "br": (w - 6, h - 6)}
    red = [k for k, xy in corners.items() if img.getpixel(xy)[0] > 200 and img.getpixel(xy)[1] < 60]
    assert len(red) == 1, red
    return red[0], img.size


@pytest.mark.parametrize("rotation,corner,size", [
    (0, "tl", (320, 180)), (90, "tr", (180, 320)), (180, "br", (320, 180)), (270, "bl", (180, 320)),
])
def test_rotate_jpeg_is_clockwise(rotation, corner, size):
    assert red_corner(rotate_jpeg(real_jpeg(), rotation)) == (corner, size)


def test_rotate_jpeg_keeps_quality():
    src = real_jpeg()
    out = rotate_jpeg(src, 180)
    assert Image.open(io.BytesIO(out)).quantization == Image.open(io.BytesIO(src)).quantization
    assert rotate_jpeg(src, 0) is src  # untouched, not re-encoded


@pytest.mark.parametrize("meta,want", [
    ({}, 0), ({"rotation": None}, 0), ({"rotation": 0}, 0), ({"rotation": 90}, 90),
    ({"rotation": 180.0}, 180), ({"rotation": 270}, 270),
])
def test_rotation_of(meta, want):
    assert rotation_of(meta) == want


@pytest.mark.parametrize("bad", [45, 360, -90, "90", True, [90]])
def test_rotation_of_refuses_others(bad):
    with pytest.raises(ValueError):
        rotation_of({"rotation": bad})


def test_post_rotates_upright_and_records_it(client, tmp_path):
    r = post(client, image=real_jpeg(), meta={**META, "orientation": "landscapeRight", "rotation": 180})
    assert r.status_code == 201, r.text
    jpg = tmp_path / r.json()["path"]
    assert red_corner(jpg.read_bytes()) == ("br", (320, 180))
    side = json.loads(jpg.with_suffix(".json").read_text())
    assert (side["orientation"], side["rotation"], side["rotated_deg"]) == ("landscapeRight", 180, 180)
    assert side["bytes"] == jpg.stat().st_size


def test_post_portrait_turns_a_quarter(client, tmp_path):
    r = post(client, image=real_jpeg(), meta={**META, "orientation": "portrait", "rotation": 90})
    assert r.status_code == 201, r.text
    assert red_corner((tmp_path / r.json()["path"]).read_bytes()) == ("tr", (180, 320))


def test_post_without_rotation_is_stored_as_sent(client, tmp_path):
    r = post(client)  # META has no rotation: an app from before EYE-3
    jpg = tmp_path / r.json()["path"]
    assert jpg.read_bytes() == JPEG
    assert json.loads(jpg.with_suffix(".json").read_text())["rotated_deg"] == 0


def test_post_bad_rotation_is_400(client, tmp_path):
    r = post(client, image=real_jpeg(), meta={**META, "rotation": 45})
    assert r.status_code == 400 and "rotation" in r.json()["error"]
    assert not (tmp_path / "screeneye").exists()


def test_post_undecodable_jpeg_to_rotate_is_400(client, tmp_path):
    r = post(client, meta={**META, "rotation": 90})  # JPEG magic, but no picture
    assert r.status_code == 400 and "error" in r.json()
    assert not (tmp_path / "screeneye").exists()
