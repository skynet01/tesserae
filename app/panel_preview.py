"""Show renderer output upright, without publishing it to a display."""

from __future__ import annotations

import io
from typing import Any

import numpy as np
from PIL import Image

from app.quantizer import align_palette_override, canonicalise_gamut, palette_for_gamut
from app.renderer_loader import Renderer
from app.state.page_store import Panel


def render_panel_preview(
    source: bytes, *, renderer: Renderer, panel: Panel, settings: dict[str, Any]
) -> bytes:
    # Preview in logical screen coordinates. Physical buffer rotation, hidden
    # columns and row reversal are transport details, not picture settings.
    logical = panel.model_copy(
        update={
            "native_w": panel.w,
            "native_h": panel.h,
            "native_declared": True,
            "col_offset": None,
            "flip": False,
            "vflip": False,
        }
    )
    artifact = renderer.transform(source, panel=logical, settings=settings)
    base_id = renderer.id.split("__", 1)[0]
    pi_portrait = base_id in ("pi_bin", "pico_bin", "pi_png") and logical.w < logical.h
    if renderer.extension != "bin":
        with Image.open(io.BytesIO(artifact)) as image:
            preview = image.convert("RGB")
        if pi_portrait:
            preview = preview.rotate(-90, expand=True)
    else:
        width, height = logical.w, logical.h
        if pi_portrait:
            width, height = height, width
        pixels = width * height
        bits = len(artifact) * 8 // pixels
        if bits not in (1, 2, 4) or len(artifact) * 8 != pixels * bits:
            raise ValueError("Unsupported panel buffer size for preview")
        packed = np.frombuffer(artifact, dtype=np.uint8)
        shifts = np.arange(8 - bits, -1, -bits, dtype=np.uint8)
        values = ((packed[:, None] >> shifts) & ((1 << bits) - 1)).ravel()
        gamut = canonicalise_gamut(logical.gamut)
        if gamut.startswith("gray_"):
            # 8-gray buffers also use 4-bit values, spread across 0..15.
            palette = [(round(index * 255 / ((1 << bits) - 1)),) * 3 for index in range(1 << bits)]
        else:
            palette = [(0, 0, 0)] * (1 << bits)
            colours = palette_for_gamut(gamut)
            # A device with a Calibration-tab profile is shown in its measured inks, so the
            # preview looks like the glass (Spectra "white" is a pale grey-teal, not #FFF).
            override = settings.get("_palette_override")
            if override and len(override) >= len(colours):
                colours = align_palette_override(
                    tuple(tuple(int(v) for v in c) for c in override), colours
                )
            wire_values = (0, 1, 2, 3, 5, 6) if gamut == "waveshare_e6" else range(len(colours))
            for wire, colour in zip(wire_values, colours, strict=True):
                if wire < len(palette):
                    palette[wire] = colour
        rgb = np.asarray(palette, dtype=np.uint8)[values].reshape((height, width, 3))
        preview = Image.fromarray(rgb)
        if pi_portrait:
            preview = preview.rotate(-90, expand=True)
    out = io.BytesIO()
    preview.save(out, "PNG")
    return out.getvalue()
