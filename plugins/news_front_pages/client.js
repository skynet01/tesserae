// news_front_pages, Spectra full-bleed image. One newspaper front page
// at the cell's full width, top-aligned: the masthead and the lead
// stories sit at the top of a front page, so a cell shorter than the
// page loses the bottom (usually adverts) rather than the headlines.
// No overlay: the masthead already names the paper and the date.
//
// ``trim`` (percent of the page height, per paper in the widget's
// settings) shifts the page up to hide the printer's slug line some
// papers carry above the masthead. A CSS percentage margin resolves
// against the width, so the server sends the page's pixel size and
// the shift is converted here.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function shiftPercent(data) {
  const trim = Math.min(Math.max(Number(data.trim) || 0, 0), 25);
  const width = Number(data.width) || 0;
  const height = Number(data.height) || 0;
  return width > 0 && height > 0 ? (trim * height) / width : 0;
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
    shadow.innerHTML = `
      ${css}
      <div class="w is-bleed" data-widget="news_front_pages">
        <div class="bleed-empty">${t("no_front_page", "No front page today.")}</div>
      </div>`;
    return;
  }

  const alt = [data.code, data.date].filter(Boolean).join(", ");
  shadow.innerHTML = `
    ${css}
    <style>
      .w.is-bleed > img.page {
        flex: none;
        width: 100%;
        height: auto;
        object-fit: fill;
        margin-top: -${shiftPercent(data).toFixed(3)}%;
      }
    </style>
    <div class="w is-bleed" data-widget="news_front_pages">
      <img class="page" src="${escapeHtml(data.url)}" alt="${escapeHtml(alt)}">
    </div>`;
}
