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
      <img class="page" src="${escapeHtml(data.url)}" alt="${escapeHtml(alt)}">
    </div>`;
  const img = shadow.querySelector("img.page");
  if (img) img.style.cssText = paperCss(data);
}
