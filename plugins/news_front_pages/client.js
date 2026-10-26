// news_front_pages, Spectra full-bleed image. One newspaper front page
// at the cell's full width, top-aligned: the masthead and the lead
// stories sit at the top of a front page, so a cell shorter than the
// page loses the bottom (usually adverts) rather than the headlines.
// No overlay: the masthead already names the paper and the date.
//
// ``css`` is the paper's own CSS from the widget's settings, already cut
// down by the server to a short list of safe declarations
// (sanitize_css in server.py). It goes on this paper's <img> as its
// inline style, through the DOM rather than markup, so it styles that
// image alone and has nothing to break out of. The check below is a
// second line, not the first: CSS that names a URL or carries markup
// characters is dropped whole.

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
// transforms. Measured again once the image has loaded.
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

export default function render(shadow, ctx) {
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
  shadow.innerHTML = `
    ${css}
    <style>
      .w.is-bleed {
        overflow: hidden;
      }
      .w.is-bleed > img.page {
        flex: none;
        width: 100%;
        height: auto;
        object-fit: fill;
      }
    </style>
    <div class="w is-bleed" data-widget="news_front_pages">
      <img class="page" src="${escapeHtml(data.url)}" alt="${escapeHtml(alt)}"${size}>
    </div>`;
  const img = shadow.querySelector("img.page");
  if (!img) return;
  img.style.cssText = paperCss(data);
  snapToPixels(img);
  img.addEventListener("load", () => snapToPixels(img), { once: true });
}
