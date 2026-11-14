"""Decode the real renderer output without changing pixel decisions."""

import io

import pytest
from PIL import Image

from app.panel_preview import render_panel_preview
from app.state.page_store import Panel


@pytest.mark.parametrize(
    "renderer_id,gamut",
    [
        ("esp32_bw_bin", "mono"),
        ("esp32_gray2_bin", "gray_4"),
        ("esp32_gray_bin", "gray_16"),
        ("esp32_bin", "waveshare_e6"),
        ("esp32_bin", "inky_7colour"),
        ("esp32_bin", "bwry_4"),
        ("pi_bin", "waveshare_e6"),
        ("pico_bin", "waveshare_e6"),
        ("trmnl_png_gray16", "gray_16"),
        ("trmnl_png", "mono"),
    ],
)
@pytest.mark.parametrize("size", [(32, 24), (24, 32)])
def test_preview_roundtrips_panel_pixel_values(app, renderer_id, gamut, size):
    renderer = app.config["RENDERER_REGISTRY"].get(renderer_id)
    image = Image.new("RGB", size)
    for x in range(size[0]):
        image.paste((x * 7, 100, 220 - x * 5), (x, 0, x + 1, size[1]))
    out = io.BytesIO()
    image.save(out, "PNG")
    panel = Panel(w=size[0], h=size[1], gamut=gamut)
    settings = {"dither": "floyd-steinberg", "saturation": 1.0, "contrast": 1.0}
    preview = render_panel_preview(
        out.getvalue(), renderer=renderer, panel=panel, settings=settings
    )
    with Image.open(io.BytesIO(preview)) as decoded:
        assert decoded.size == size
    original = renderer.transform(out.getvalue(), panel=panel, settings=settings)
    repacked = renderer.transform(preview, panel=panel, settings={**settings, "dither": "none"})
    assert repacked == original


def test_preview_paints_a_calibrated_device_in_its_measured_inks(app):
    """A device with a palette profile is previewed in the profile's colours, as the glass shows
    them; the pixel decisions (which ink goes where) stay the packer's."""
    from app.palette_profiles.bundled import bundled_profile

    profile = bundled_profile("paperlesspaper-spectra6")
    assert profile is not None
    renderer = app.config["RENDERER_REGISTRY"].get("esp32_bin")
    image = Image.new("RGB", (16, 8), (255, 255, 255))
    image.paste((0, 0, 0), (0, 0, 8, 8))
    out = io.BytesIO()
    image.save(out, "PNG")
    panel = Panel(w=16, h=8, gamut="waveshare_e6")
    settings = {"dither": "floyd-steinberg", "_palette_override": profile.palette.as_tuples()}
    preview = render_panel_preview(out.getvalue(), renderer=renderer, panel=panel, settings=settings)
    with Image.open(io.BytesIO(preview)) as decoded:
        assert decoded.convert("RGB").getpixel((2, 2)) == (0x1F, 0x22, 0x26)
        assert decoded.convert("RGB").getpixel((12, 2)) == (0xB9, 0xC7, 0xC9)
