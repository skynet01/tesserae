// Plain-node self-check, run by tests/test_clamp_check.py (or: node tests/clamp_check.mjs).
// How a front page sits in cells of real screen sizes, and the PDF canvas's size.
import assert from "node:assert/strict";
import {
  MAX_CANVAS_AREA,
  MAX_CANVAS_SIDE,
  PDF_BUDGET_MS,
  READABLE_PX,
  canvasSize,
  pageFit,
} from "../client.js";

const page = 2632 / 1440; // a page drawn 1440 wide

// The 32-inch portrait panel: full width from the top, as before.
assert.equal(pageFit(1440, 2560, page, "top"), "full");
// A 13-inch 1600x1200: wide enough to read; the top of the page shows.
assert.equal(pageFit(1600, 1200, page, "top"), "full");
// A 7.5-inch 800x480: still at least READABLE_PX wide, so full width, top.
assert.equal(pageFit(800, 480, page, "top"), "full");
// A small tile: drawn READABLE_PX wide and centred, so headlines stay readable.
assert.equal(pageFit(400, 300, page, "top"), "zoom");
assert.ok(READABLE_PX >= 700);
// "The whole page, smaller" wherever the page is taller than the cell.
assert.equal(pageFit(800, 480, page, "whole"), "whole");
assert.equal(pageFit(1440, 2632, page, "whole"), "full");
// No size known: full width, as before.
assert.equal(pageFit(0, 0, page, "top"), "full");
// The PDF's canvas: one canvas pixel per screen pixel.
const nyt = 1686.31 / 879.12; // The New York Times' page one
assert.deepEqual(canvasSize(1440, 1, nyt), { w: 1440, h: 2762 });
assert.deepEqual(canvasSize(720, 2, nyt), { w: 1440, h: 2762 }); // a 2x screen
assert.deepEqual(canvasSize(1200, 1.5, page), { w: 1800, h: Math.round(1800 * page) });
// Never past the caps, the page's shape kept.
const huge = canvasSize(4000, 3, nyt);
assert.ok(huge.w * huge.h <= MAX_CANVAS_AREA && huge.h <= MAX_CANVAS_SIDE);
assert.ok(Math.abs(huge.h / huge.w - nyt) < 0.01);
const tall = canvasSize(1000, 1, 20);
assert.ok(tall.h <= MAX_CANVAS_SIDE && tall.w < 1000);
// Nothing to draw into: no size.
assert.equal(canvasSize(0, 1, nyt), null);
assert.equal(canvasSize(1440, 1, 0), null);
assert.ok(PDF_BUDGET_MS >= 5000 && PDF_BUDGET_MS <= 17000);
console.log("news_front_pages clamp_check: ok");
