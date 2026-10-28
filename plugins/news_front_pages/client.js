// news_front_pages, Spectra full-bleed image. One newspaper front page
// at the cell's full width, top-aligned: the masthead and the lead
// stories sit at the top of a front page, so a cell shorter than the
// page loses the bottom (usually adverts) rather than the headlines.
// No overlay: the masthead already names the paper and the date.
//
// The page is drawn from the paper's PDF when the server has one
// (``data.pdf_url``): PDF.js (vendored under static/pdfjs, Apache-2.0)
// draws page one onto a <canvas> at the cell's own pixel width, so body
// text is as sharp as the panel allows. The JPEG (``data.url``, about 700
// px wide) stands in when there is no PDF, PDF.js fails, or the drawing
// would take longer than PDF_BUDGET_MS. The render resolves only once the
// canvas is fully drawn (or the JPEG is in its place), and the canvas stays
// hidden until then, so a screenshot never catches half a page.
//
// ``css`` is the paper's own CSS from the widget's settings, already cut
// down by the server to a short list of safe declarations
// (sanitize_css in server.py). It goes on this paper's page (the <canvas>
// or the <img>) as its inline style, through the DOM rather than markup,
// so it styles that page alone and has nothing to break out of. The check
// below is a second line, not the first: CSS that names a URL or carries
// markup characters is dropped whole.

function escapeHtml(s) {
  return String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      })[c]
  );
}

const UNSAFE_CSS = /[<>{}\\@"'#&]|url\s*\(|image-set|expression|import|var\s*\(/i;

function paperCss(data) {
  const css = typeof data.css === "string" ? data.css : "";
  return css.length <= 2000 && !UNSAFE_CSS.test(css) ? css : "";
}

// Put the page on whole pixels. A percentage translate (the usual way to
// hide a slug line: translateY(-2%) of a 2777 px page is -55.54 px) leaves
// it between pixel rows, and the browser then resamples the whole page:
// every line of text goes half a pixel soft before the panel even dithers
// it. The ``translate`` property (which a paper's CSS can't set) adds the
// missing fraction. Layout offsets are already snapped; this is for
// transforms. Measured again once an image has loaded, and once a canvas
// has its final size.
function snapToPixels(img) {
  img.style.translate = "";
  const box = img.getBoundingClientRect();
  if (!box.width || !box.height) return;
  const dx = Math.round(box.left) - box.left;
  const dy = Math.round(box.top) - box.top;
  if (Math.abs(dx) > 0.001 || Math.abs(dy) > 0.001) {
    img.style.translate = `${dx.toFixed(3)}px ${dy.toFixed(3)}px`;
  }
}

// Narrower than this, a page drawn at the cell's width has headlines too
// small to read: the page is drawn this wide instead and centred, so the
// top of it (masthead and headlines) shows at a readable size.
export const READABLE_PX = 720;

// How the page sits in a ``w`` x ``h`` cell: "full" (the cell's width, from
// the top, as on the big portrait panel), "zoom" (READABLE_PX wide, centred,
// from the top) or "whole" (the whole page, smaller). ``fit`` is the
// "On smaller screens" setting; ``ratio`` the page's height over its width.
export function pageFit(w, h, ratio, fit) {
  if (!(w > 0) || !(h > 0) || !(ratio > 0)) return "full";
  if (fit === "whole" && w * ratio > h) return "whole";
  return w < READABLE_PX ? "zoom" : "full";
}

// ----- the PDF, drawn with PDF.js --------------------------------------------

// The whole PDF path (loading PDF.js, the file, and drawing page one) gets
// this long before the JPEG is shown instead. The host waits up to 25 s for
// every widget on the page to finish (_COMPOSE_SIGNAL_TIMEOUT_MS in
// app/renderer.py); this leaves room inside that for the rest of the page and
// the JPEG's own load.
export const PDF_BUDGET_MS = 15000;
// The canvas never gets bigger than this, either side or in all (about 64 MB
// of pixels), whatever the cell, zoom and device pixel ratio ask for.
export const MAX_CANVAS_SIDE = 8192;
export const MAX_CANVAS_AREA = 16 * 1024 * 1024;

// The canvas's pixel size for a page ``ratio`` (height over width) tall per
// unit of width, shown ``cssWidth`` CSS pixels wide where one CSS pixel is
// ``scale`` device pixels (the zoom and the device pixel ratio together), so
// one canvas pixel lands on one screen pixel. Scaled down, keeping the
// ratio, to stay inside MAX_CANVAS_SIDE and MAX_CANVAS_AREA.
export function canvasSize(cssWidth, scale, ratio) {
  if (!(cssWidth > 0) || !(scale > 0) || !(ratio > 0)) return null;
  let w = Math.max(1, Math.round(cssWidth * scale));
  let h = Math.max(1, Math.round(w * ratio));
  const k = Math.min(1, MAX_CANVAS_SIDE / w, MAX_CANVAS_SIDE / h, Math.sqrt(MAX_CANVAS_AREA / (w * h)));
  if (k < 1) {
    w = Math.max(1, Math.floor(w * k));
    h = Math.max(1, Math.floor(w * ratio));
  }
  return { w, h };
}

const PDFJS = new URL("./static/pdfjs/", import.meta.url);
let pdfjsLoad = null;

// PDF.js, imported once per page and shared by every cell. It parses the PDF
// in a module worker (pdf.worker.min.mjs); where a worker can't start,
// PDF.js runs the same code on the page instead, by itself.
function pdfjs() {
  if (!pdfjsLoad) {
    pdfjsLoad = import(new URL("pdf.min.mjs", PDFJS).href).then((lib) => {
      lib.GlobalWorkerOptions.workerSrc = new URL("pdf.worker.min.mjs", PDFJS).href;
      return lib;
    });
    pdfjsLoad.catch(() => {
      pdfjsLoad = null; // a later render may try again
    });
  }
  return pdfjsLoad;
}

function urlPrefix() {
  return globalThis.TESSERAE_URL_PREFIX || "";
}

// Draw page one of the PDF at ``url`` onto ``canvas`` (already in the cell,
// hidden, styled as the page), sized to the canvas's laid-out width. Resolves
// with how long it took (ms) once the drawing is complete; rejects when the
// PDF can't be had or drawn, or after ``budgetMs``, having stopped the work.
export async function drawPdf(canvas, url, budgetMs = PDF_BUDGET_MS) {
  const started = performance.now();
  const abort = new AbortController();
  let loading = null;
  let drawing = null;
  let timer = null;
  const late = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`took longer than ${budgetMs} ms`)), budgetMs);
  });
  const work = (async () => {
    const lib = await pdfjs();
    const response = await fetch(url, { signal: abort.signal, credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = new Uint8Array(await response.arrayBuffer());
    loading = lib.getDocument({
      data,
      // Never compile a PDF's font programs into JavaScript.
      isEvalSupported: false,
      // What some papers need, vendored beside PDF.js: JBIG2 and JPEG 2000
      // image decoders and the CMYK colour profile (wasm, iccs), and the
      // CMaps for Chinese, Japanese and Korean fonts (cmaps).
      wasmUrl: new URL("wasm/", PDFJS).href,
      iccUrl: new URL("iccs/", PDFJS).href,
      cMapUrl: new URL("cmaps/", PDFJS).href,
      cMapPacked: true,
    });
    const doc = await loading.promise;
    const page = await doc.getPage(1);
    const base = page.getViewport({ scale: 1 });
    if (!(base.width > 0 && base.height > 0)) throw new Error("page one has no size");
    const ratio = base.height / base.width;
    // Lay the canvas out at the PDF's own shape, then measure it: its box
    // on screen (zoom and transforms included) over its CSS width is how many
    // device pixels one CSS pixel covers.
    canvas.width = Math.round(base.width);
    canvas.height = Math.round(base.height);
    const box = canvas.getBoundingClientRect();
    const dpr = Number(globalThis.devicePixelRatio) || 1;
    const size = canvasSize(box.width, dpr, ratio);
    if (!size) throw new Error("the cell has no width");
    canvas.width = size.w;
    canvas.height = size.h;
    drawing = page.render({
      canvas,
      viewport: page.getViewport({ scale: size.w / base.width }),
      background: "#ffffff",
    });
    await drawing.promise;
  })();
  work.catch(() => {}); // settled by the race below; this only keeps a late failure quiet
  try {
    await Promise.race([work, late]);
    return Math.round(performance.now() - started);
  } catch (err) {
    abort.abort();
    if (drawing) drawing.cancel();
    throw err;
  } finally {
    clearTimeout(timer);
    // Ends the worker and frees the document; the pixels stay on the canvas.
    if (loading) loading.destroy().catch(() => {});
  }
}

// ----- the widget ------------------------------------------------------------

export default async function render(shadow, ctx) {
  const t = ctx?.t || ((key, fallback) => fallback ?? key);
  const data = ctx?.data ?? {};
  const css = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">`;

  if (data.error) {
    shadow.innerHTML = `
      ${css}
      <div class="w" data-widget="news_front_pages">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${t("front_pages", "Front pages")}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  if (!data.url) {
    // ``pending``: nothing saved yet and the download is on its way; the
    // host renders again (``live_refresh_s``) once it lands.
    const message = data.pending
      ? t("fetching", "Fetching today's front page…")
      : t("no_front_page", "No front page today.");
    shadow.innerHTML = `
      ${css}
      <div class="w is-bleed" data-widget="news_front_pages">
        <div class="bleed-empty">${escapeHtml(message)}</div>
      </div>`;
    return;
  }

  const alt = [data.code, data.date].filter(Boolean).join(", ");
  // The page's own size, so it is laid out (and snapped) before it loads.
  const width = Math.round(Number(data.width) || 0);
  const height = Math.round(Number(data.height) || 0);
  const size = width > 0 && height > 0 ? ` width="${width}" height="${height}"` : "";
  const fit = pageFit(
    Number(ctx?.cell?.w) || 0,
    Number(ctx?.cell?.h) || 0,
    width > 0 ? height / width : 0,
    data.fit,
  );
  shadow.innerHTML = `
    ${css}
    <style>
      .w.is-bleed {
        overflow: hidden;
      }
      .w.is-bleed > .page {
        flex: none;
        width: 100%;
        height: auto;
        object-fit: fill;
      }
      .w.is-bleed.fit-zoom > .page {
        width: ${READABLE_PX}px;
        max-width: none;
        align-self: center;
      }
      .w.is-bleed.fit-whole { align-items: center; }
      .w.is-bleed.fit-whole > .page {
        width: auto;
        height: 100%;
        max-width: 100%;
        object-fit: contain;
      }
    </style>
    <div class="w is-bleed fit-${fit}" data-widget="news_front_pages"></div>`;
  const box = shadow.querySelector(".w");
  if (!box) return;
  const style = paperCss(data);

  if (typeof data.pdf_url === "string" && data.pdf_url.startsWith("/")) {
    const canvas = document.createElement("canvas");
    canvas.className = "page";
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", alt);
    if (width > 0 && height > 0) {
      canvas.width = width;
      canvas.height = height;
    }
    canvas.style.cssText = style;
    canvas.style.visibility = "hidden"; // until the drawing is complete
    box.append(canvas);
    try {
      const ms = await drawPdf(canvas, urlPrefix() + data.pdf_url);
      canvas.dataset.drawnMs = String(ms);
      canvas.style.visibility = "";
      snapToPixels(canvas);
      return;
    } catch (err) {
      console.warn(`news_front_pages: drew the JPEG, the PDF failed (${err?.message || err})`);
      canvas.remove();
    }
  }

  box.insertAdjacentHTML(
    "beforeend",
    `<img class="page" src="${escapeHtml(data.url)}" alt="${escapeHtml(alt)}"${size}>`,
  );
  const img = box.querySelector("img.page");
  if (!img) return;
  img.style.cssText = style;
  snapToPixels(img);
  img.addEventListener("load", () => snapToPixels(img), { once: true });
}
