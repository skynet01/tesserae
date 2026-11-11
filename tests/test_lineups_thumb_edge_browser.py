"""Lineups thumbnails leave no strip of their own background uncovered.

The border takes 1-2 px off both sides of a card sized by the display's ratio,
so a picture of exactly that ratio no longer fills what is left. With
``object-fit: contain`` the gap showed the card's (light) background as a
1 px line along one side of a dark picture. Whether a gap is left is measured
from the layout, not from screenshot pixels, which blend at a fractional
card position."""

from __future__ import annotations

import pytest

from tests.test_lineups_browser import _HAS_CHROMIUM, _open, live  # noqa: F401

pytestmark = pytest.mark.skipif(not _HAS_CHROMIUM, reason="Playwright Chromium not installed")

#: Every thumbnail's picture, at exactly its card's ratio; the part of the card
#: the picture leaves uncovered, in css px, for each visible card.
_GAPS = """
() => {
  const rows = [];
  for (const img of document.querySelectorAll('.lu-step .lu-thumb img, .lu-now-thumb .lu-thumb img')) {
    const t = img.closest('.lu-thumb');
    const w = Number(t.style.getPropertyValue('--arw'));
    const h = Number(t.style.getPropertyValue('--arh'));
    const k = 352 / Math.max(w, h);
    const c = document.createElement('canvas');
    c.width = Math.round(w * k);
    c.height = Math.round(h * k);
    img.removeAttribute('loading');
    img.src = c.toDataURL();
  }
  return new Promise((resolve) => setTimeout(() => {
    for (const img of document.querySelectorAll('.lu-step .lu-thumb img, .lu-now-thumb .lu-thumb img')) {
      const box = img.getBoundingClientRect();
      if (!box.width || !box.height || !img.naturalWidth) continue;
      const natural = img.naturalWidth / img.naturalHeight;
      const fit = getComputedStyle(img).objectFit;
      // The picture's size inside the img box, as object-fit lays it out.
      let pw = box.width;
      let ph = box.height;
      if (fit === 'contain' || fit === 'scale-down') {
        pw = Math.min(box.width, box.height * natural);
        ph = Math.min(box.height, box.width / natural);
      }
      const t = img.closest('.lu-thumb');
      rows.push({
        card: [t.getBoundingClientRect().width, t.getBoundingClientRect().height],
        ar: Math.abs(natural - Number(t.style.getPropertyValue('--arw')) / Number(t.style.getPropertyValue('--arh'))),
        gapX: box.width - pw,
        gapY: box.height - ph,
      });
    }
    resolve(rows);
  }, 400));
}
"""


@pytest.mark.parametrize("scheme", ["dark", "light"])
@pytest.mark.parametrize("width", [1440, 1000, 390])
def test_thumbnails_have_no_uncovered_edge(live: str, width: int, scheme: str) -> None:  # noqa: F811
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser, page, _api = _open(p, live, width=width, scheme=scheme, pics=True)
        try:
            rows = page.evaluate(_GAPS)
            shapes = {(round(r["card"][0]), round(r["card"][1])) for r in rows}
            # Landscape and portrait cards of several sizes, so a gap could show either way.
            assert len(rows) >= 8 and len(shapes) >= 3, (len(rows), shapes)
            for r in rows:
                assert r["ar"] < 0.01, r
                assert r["gapX"] < 0.5 and r["gapY"] < 0.5, r
        finally:
            browser.close()
