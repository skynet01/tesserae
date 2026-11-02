// Plain-node self-check, run by tests/test_widget_clamp_checks.py.
// How much summary each screen gets.
import assert from "node:assert/strict";
import { layoutFor, smallArtBox, summaryLines, textArea, titleSize, TITLE_MIN } from "../client.js";

const title = 88; // characters
// The 32-inch portrait panel: the picture on top, a full summary below.
assert.ok(summaryLines(1440, 2560, false, true, title) >= 4);
// A 13-inch 1600x1200: side by side, a long summary.
const big = summaryLines(1600, 1200, true, true, title);
// A 7.5-inch 800x480: side by side, a shorter summary that still fits.
const small = summaryLines(800, 480, true, true, title);
assert.ok(small >= 2 && small < big, `${small} < ${big}`);
// ... and fits: headline (4 lines at most), source line and summary within
// the height.
const titleFont = Math.max(13, Math.min(64, 0.06 * 480));
const used = 4 * titleFont * 1.12 + small * Math.max(11, 0.034 * 480) * 1.45 + 0.17 * 480;
assert.ok(used <= 480, `${used} > 480`);
// A small tile: a few lines at most.
assert.ok(summaryLines(300, 200, true, true, title) <= 5);

// Which layout a cell gets.
const side = (w, h, ratio) => layoutFor(w, h, true, ratio);
// A short landscape screen puts the picture at the left, whatever its shape.
for (const [w, h] of [[800, 480], [640, 384], [1024, 600]]) {
  for (const ratio of [9 / 16, 3 / 4, 1, 3 / 2, 16 / 9]) {
    assert.deepEqual(side(w, h, ratio), { wide: true, small: true }, `${w}x${h} ${ratio}`);
  }
}
// ... a big landscape frame keeps the poster unless the picture is tall,
assert.deepEqual(side(1920, 1080, 16 / 9), { wide: false, small: false });
assert.deepEqual(side(1600, 1200, 4 / 3), { wide: false, small: false });
assert.deepEqual(side(1600, 1200, 9 / 16), { wide: true, small: false });
// ... and a portrait or square cell, or a cell with no picture, is never side by side.
assert.deepEqual(side(480, 800, 9 / 16), { wide: false, small: false });
assert.deepEqual(side(480, 480, 1), { wide: false, small: false });
assert.deepEqual(layoutFor(800, 480, false, 9 / 16), { wide: false, small: false });
// The picture's box: about three quarters of the height, centred, the same
// padding above, below and to the left, and never more than 60% of the width.
for (const [w, h, ratio] of [[800, 480, 3 / 4], [800, 480, 9 / 16], [800, 480, 1], [800, 480, 16 / 9], [640, 384, 3 / 4], [1024, 600, 3 / 4]]) {
  const box = smallArtBox(w, h, ratio);
  assert.ok(Math.abs(box.h - 0.76 * h) < 0.01, `${w}x${h} height ${box.h}`);
  assert.ok(Math.abs(box.y - (h - box.y - box.h)) < 0.01, "centred: the same padding above and below");
  assert.equal(box.x, box.y, "the same padding left as top");
  assert.ok(box.w <= 0.6 * w + 0.01, `${w}x${h} width ${box.w}`);
}
// The 3:4 picture keeps its own shape in that box.
assert.ok(Math.abs(smallArtBox(800, 480, 3 / 4).w - 0.76 * 480 * 0.75) < 0.01);
// The summary still fits beside the smaller picture, on a square one too.
assert.ok(summaryLines(800, 480, true, true, title, 1) >= 1);

// With no summary the headline grows to fill the text area.
const headline = 88;
for (const [w, h] of [[800, 480], [640, 384], [1024, 600]]) {
  const area = textArea(w, h, true, true, 3 / 4, true);
  // The text area is the width the (smaller) picture leaves, and nearly all the height.
  assert.ok(area.w > 0.45 * w && area.h > 0.6 * h, `${w}x${h} area ${JSON.stringify(area)}`);
  const big = titleSize(w, h, true, true, 3 / 4, headline, true);
  assert.ok(big > 0.06 * h * 1.3, `${w}x${h} ${big}px fills more than a headline over a summary`);
  assert.ok(big <= 0.2 * h && big >= TITLE_MIN, `${w}x${h} ${big}px is sane`);
  // ... it fits: its lines, at the size it chose, are within the area.
  const lines = Math.ceil((headline * 0.54 * 1.06 * big) / area.w);
  assert.ok(lines * big * 1.12 <= area.h, `${w}x${h} overflows`);
  // A shorter headline gets a bigger size, a longer one a smaller, never below the minimum.
  const short = titleSize(w, h, true, true, 3 / 4, 24, true);
  const long = titleSize(w, h, true, true, 3 / 4, 400, true);
  assert.ok(short > big && big > long && long >= TITLE_MIN, `${short} ${big} ${long}`);
  // Without a source line there is more room.
  assert.ok(titleSize(w, h, true, true, 3 / 4, headline, false) >= big);
}
// A wider text area (a narrower picture, a bigger screen) never shrinks it.
assert.ok(titleSize(800, 480, true, true, 9 / 16, headline, true) >= titleSize(800, 480, true, true, 1, headline, true));
// A text poster (no picture) and the side-by-side layout on a big frame fit too.
assert.ok(titleSize(1440, 2560, false, false, 1, headline, true) <= 0.2 * 2560);
assert.ok(titleSize(1600, 1200, true, true, 9 / 16, headline, true) >= TITLE_MIN);
console.log("news_illustrated clamp_check: ok");
