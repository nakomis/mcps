"""Local icon export: trimming, resizing, the .ico, and the preview sheet.

Everything here is Pillow-only, so it runs without fal.ai or AWS.
"""

from PIL import Image

from falai_mcp.server import (
    ICO_SIZES,
    _export_icon_set,
    _pixel_size_warnings,
    _square_on_alpha,
)


def _sprite(canvas=(200, 120), box=(50, 20, 150, 100), colour=(30, 90, 200, 255)):
    """A transparent canvas with one opaque rectangle on it."""
    img = Image.new("RGBA", canvas, (0, 0, 0, 0))
    img.paste(colour, box)
    return img


def test_square_on_alpha_crops_to_content_and_centres_it():
    out, warnings = _square_on_alpha(_sprite(), margin=0.0)

    assert out.size == (100, 100)  # longest side of the 100x80 rectangle
    assert warnings == []
    # 80 tall in a 100 canvas: 10 transparent rows above and below.
    assert out.getpixel((50, 5))[3] == 0
    assert out.getpixel((50, 50))[3] == 255
    assert out.getpixel((50, 94))[3] == 0


def test_square_on_alpha_adds_margin():
    out, _ = _square_on_alpha(_sprite(), margin=0.1)

    assert out.size == (120, 120)  # 100 plus 10% each side
    assert out.getpixel((5, 60))[3] == 0


def test_square_on_alpha_rejects_an_empty_image():
    empty = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    try:
        _square_on_alpha(empty, margin=0.0)
    except ValueError as e:
        assert "empty" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_square_on_alpha_warns_when_background_survived():
    solid = Image.new("RGBA", (64, 64), (255, 255, 255, 255))
    _, warnings = _square_on_alpha(solid, margin=0.0)

    assert any("background" in w for w in warnings)


def test_export_writes_every_size_ico_and_preview(tmp_path):
    result = _export_icon_set(_sprite(), tmp_path, [16, 32, 180], Image.LANCZOS, 0.0)

    assert sorted(result.files) == [16, 32, 180]
    for size, path in result.files.items():
        with Image.open(path) as img:
            assert img.size == (size, size)
            assert img.mode == "RGBA"

    with Image.open(result.ico) as ico:
        assert sorted(ico.info["sizes"]) == sorted((s, s) for s in ICO_SIZES)

    with Image.open(result.preview) as preview:
        assert preview.width > 180 and preview.height > 180


def test_export_keeps_transparency(tmp_path):
    result = _export_icon_set(_sprite(), tmp_path, [32], Image.LANCZOS, 0.1)

    with Image.open(result.files[32]) as img:
        assert img.getpixel((0, 0))[3] == 0
        assert img.getpixel((16, 16))[3] == 255


def test_nearest_resize_keeps_hard_edges(tmp_path):
    # A 2x2 checkerboard scaled up must stay pure colours, no blended pixels.
    checker = Image.new("RGBA", (2, 2))
    checker.putdata([(255, 0, 0, 255), (0, 0, 255, 255)] * 1 + [(0, 0, 255, 255), (255, 0, 0, 255)])
    result = _export_icon_set(checker, tmp_path, [32], Image.NEAREST, 0.0)

    with Image.open(result.files[32]) as img:
        colours = {c for _, c in img.getcolors()}
    assert colours == {(255, 0, 0, 255), (0, 0, 255, 255)}


def test_pixel_size_warnings_flag_non_multiples():
    warnings = _pixel_size_warnings(32, [16, 32, 64, 180])

    assert len(warnings) == 1
    assert "16" in warnings[0] and "180" in warnings[0]
    assert "64" not in warnings[0]


def test_pixel_size_warnings_quiet_when_everything_divides():
    assert _pixel_size_warnings(16, [16, 32, 48]) == []


def test_glyph_from_luminance_makes_dark_opaque_and_light_transparent():
    from falai_mcp.server import _glyph_from_luminance

    img = Image.new("RGB", (30, 10), (255, 255, 255))
    img.paste((0, 0, 0), (0, 0, 10, 10))        # black: opaque
    img.paste((128, 128, 128), (10, 0, 20, 10))  # grey edge: partial
    # the last third stays white: transparent, i.e. a hole

    glyph = _glyph_from_luminance(img)

    assert glyph.mode == "RGBA"
    assert glyph.getpixel((5, 5)) == (0, 0, 0, 255)
    assert 0 < glyph.getpixel((15, 5))[3] < 255
    assert glyph.getpixel((25, 5))[3] == 0


def test_glyph_ignores_off_white_background_noise():
    from falai_mcp.server import _glyph_from_luminance

    noisy = Image.new("RGB", (4, 4), (246, 244, 247))
    assert _glyph_from_luminance(noisy).getchannel("A").getextrema() == (0, 0)


def test_tint_recolours_without_changing_shape():
    from falai_mcp.server import _tint

    glyph = _sprite(colour=(0, 0, 0, 255))
    tinted = _tint(glyph, "#FF9F0A")

    assert tinted.getchannel("A").tobytes() == glyph.getchannel("A").tobytes()
    assert tinted.getpixel((100, 60)) == (255, 159, 10, 255)


def test_tint_rejects_bad_colours():
    from falai_mcp.server import _tint

    try:
        _tint(_sprite(), "orange")
    except ValueError as e:
        assert "#RRGGBB" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_glyph_render_fills_hairline_cutouts_but_keeps_big_holes():
    from falai_mcp.server import _render_glyph

    # 400px black square, a 4px white hairline across it, and a big hole.
    img = Image.new("RGBA", (400, 400), (0, 0, 0, 255))
    img.paste((0, 0, 0, 0), (0, 98, 400, 102))
    img.paste((0, 0, 0, 0), (150, 200, 250, 300))

    small = _render_glyph(img, 40)

    assert small.size == (40, 40)
    assert min(small.getpixel((x, 10))[3] for x in range(40)) > 200  # hairline gone
    assert small.getpixel((20, 25))[3] < 30  # 10px hole survives


def test_glyph_from_transparent_logo_keeps_transparency():
    from falai_mcp.server import _glyph_from_luminance

    # A logo with alpha: transparent pixels are RGB black, which must not
    # become ink.
    logo = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
    logo.paste((10, 10, 10, 255), (5, 5, 15, 15))

    alpha = _glyph_from_luminance(logo).getchannel("A")

    assert alpha.getpixel((0, 0)) == 0
    assert alpha.getpixel((10, 10)) == 255


def test_tint_names_must_be_safe_folder_names():
    from falai_mcp.server import _check_tint_names

    _check_tint_names({"warn": "#FF9F0A", "down-2": "#FF453A"})
    for bad in ["../x", "", "a/b", "icon-16.png", "preview"]:
        try:
            _check_tint_names({bad: "#000000"})
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_pixel_native_size_ignores_margin():
    from falai_mcp.server import _sprite_size

    sprite = _sprite(canvas=(64, 64), box=(10, 10, 42, 42))  # 32x32 content
    assert _sprite_size(sprite) == 32


def test_generate_icon_rejects_bad_arguments_before_paying_or_writing(tmp_path, monkeypatch):
    import falai_mcp.server as server

    def no_calls(*_a, **_k):
        raise AssertionError("fal.ai was called")

    monkeypatch.setattr(server, "_call", no_calls)
    cases = [
        dict(subject="x", model="nope"),
        dict(subject="x", style="glyph", tints={"../up": "#000000"}),
        dict(subject="x", reference_paths=[str(tmp_path / "missing.png")]),
        dict(image_path=str(tmp_path / "missing.png")),
        dict(subject="x", reference_paths=["a"] * 5),
    ]
    for kwargs in cases:
        try:
            server.generate_icon(save_dir=str(tmp_path / "out"), **kwargs)
        except (ValueError, FileNotFoundError):
            pass
        else:
            raise AssertionError(f"accepted {kwargs}")
    assert not (tmp_path / "out").exists()
