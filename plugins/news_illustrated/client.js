// news_illustrated (AI Illustrated News), a movie-poster news story: the AI
// illustration fills the cell (contained, so speech bubbles at the edges
// survive), with the headline and summary over a gradient at the foot. A wide
// cell showing a tall picture puts the text beside it instead, since a
// caption over the bottom of a short picture would hide most of it. On a short
// landscape screen (a 7.5-inch 800x480 e-paper) the picture is always put at
// the left, filling almost the full height, with the text in the space that
// is left, whatever shape the picture is.
// Everything sizes from the cell, so a full 1440x2560 portrait panel and a
// small tile read the same way. The caption's colours and parts come from the
// widget's settings (data.look). The widget's page draws its previews with
// this very function (admin.js), so they match the frame.

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatDate(iso, locale) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ""));
  if (!m) return "";
  const day = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  try {
    return new Intl.DateTimeFormat(locale || "en", { day: "numeric", month: "long", year: "numeric" }).format(day);
  } catch {
    return iso;
  }
}

// A short landscape screen: 800x480, 640x384, 1024x600. The picture sits at
// the left, SMALL_MARGIN percent of the height in from the top, bottom and left
// edges (so it is centred vertically, with even padding round it), as wide as
// its own shape asks but never more than SMALL_ART_MAX of the width (a wider
// picture is cropped at the sides to fit that box).
export const SMALL_H = 600;
export const SMALL_MARGIN = 12;
export const SMALL_ART_MAX = 0.6;

// Which layout a cell gets: ``wide`` puts the text beside the picture, and
// ``small`` is the short landscape screen's version of that.
export function layoutFor(w, h, hasArt, artRatio) {
  const landscape = Boolean(hasArt) && w > h * 1.15;
  const small = landscape && h <= SMALL_H;
  return { wide: landscape && (small || artRatio < 1), small };
}

// The picture's box on a short landscape screen, in pixels.
export function smallArtBox(w, h, artRatio) {
  const boxH = h * (1 - (2 * SMALL_MARGIN) / 100);
  return { x: (h * SMALL_MARGIN) / 100, y: (h * SMALL_MARGIN) / 100, w: Math.min(boxH * artRatio, SMALL_ART_MAX * w), h: boxH };
}

// Summary lines that fit the caption, worked out in the units the type is set
// in (cqw when tall, cqh when wide) so the answer holds at any cell size or zoom.
const clampPx = (low, value, high) => Math.max(low, Math.min(high, value));

export function summaryLines(w, h, wide, hasArt, titleChars = 60, artRatio = 9 / 16) {
  if (!w || !h) return 6;
  if (wide) {
    // Beside the picture: what is left of the height once the headline
    // (up to four lines) and the source line are set, in summary lines, so
    // a short landscape screen gets a shorter summary rather than one cut
    // off at the foot.
    const small = hasArt && h <= SMALL_H;
    const box = small ? smallArtBox(w, h, artRatio) : null;
    const artW = !hasArt ? 0 : box ? box.x + box.w : Math.min(h * artRatio, 0.55 * w);
    const textW = Math.max(1, w - artW - 0.1 * h);
    const titleFont = clampPx(13, 0.06 * h, 64);
    const titleLines = Math.min(4, Math.ceil((titleChars * 0.62 * titleFont) / textW));
    const metaH = clampPx(9, 0.026 * h, 22) * 1.6 + 0.022 * h;
    const lineH = clampPx(11, 0.034 * h, 32) * 1.45;
    const room = h * 0.88 - metaH - titleLines * titleFont * 1.12 - 0.044 * h;
    return Math.max(0, Math.min(hasArt ? 12 : 14, Math.floor(room / lineH) - 1));
  }
  const heightInCqw = (100 * h) / w;
  const room = (hasArt ? 0.3 : 0.85) * heightInCqw - 26;
  return Math.max(0, Math.min(14, Math.floor(room / 3.4)));
}

// With no summary the headline takes the whole text area, as large as fits
// (TITLE_MIN..TITLE_MAX_H of the height). The area is worked out in pixels
// from the cell, like summaryLines; client-side the browser then measures the
// real thing and settles on the exact size (fitTitle below).
export const TITLE_MIN = 13;
export const TITLE_MAX_H = 0.2;
const TITLE_LINE = 1.12; // line-height, em
const TITLE_CHAR = 0.54; // average bold glyph width, em
const TITLE_WRAP = 1.06; // words wrap early: room lost at line ends

// The text area's size in px and the height the source line takes of it.
export function textArea(w, h, wide, hasArt, artRatio, hasMeta) {
  let areaW;
  let areaH;
  let metaH = 0;
  if (wide) {
    const box = h <= SMALL_H ? smallArtBox(w, h, artRatio) : null;
    const artW = box ? box.x + box.w : Math.min(h * artRatio, 0.55 * w);
    const padL = box ? 0.06 * h : 0.05 * h;
    areaW = w - artW - padL - 0.05 * h;
    areaH = h - 0.12 * h;
    if (hasMeta) metaH = clampPx(9, 0.026 * h, 22) * 1.6 + 0.022 * h;
  } else {
    areaW = w - 0.12 * w;
    areaH = h - 0.12 * w;
    if (hasMeta) metaH = clampPx(9, 0.015 * w, 24) * 1.6 + 0.011 * w;
  }
  return { w: Math.max(1, areaW), h: Math.max(1, areaH - metaH) };
}

// The largest headline size (px) whose lines fit the text area, for a title
// of ``chars`` characters.
export function titleSize(w, h, wide, hasArt, artRatio, chars, hasMeta) {
  const area = textArea(w, h, wide, hasArt, artRatio, hasMeta);
  const top = Math.max(TITLE_MIN, Math.min(TITLE_MAX_H * h, 0.2 * w, 200));
  for (let f = Math.floor(top); f > TITLE_MIN; f -= 1) {
    const lines = Math.max(1, Math.ceil((chars * TITLE_CHAR * TITLE_WRAP * f) / area.w));
    if (lines * f * TITLE_LINE <= area.h) return f;
  }
  return TITLE_MIN;
}

// In the browser: settle the headline's size on what is measured, the
// largest in TITLE_MIN..max whose text fits the room the caption leaves it
// (the title is a flex child that takes what the source line does not). Sets
// the size in cqh, so it keeps its proportions at any zoom.
function fitTitle(root) {
  const t = root.querySelector(".fit-title .title");
  const box = root.querySelector(".ain");
  if (!t || !box || !t.clientHeight || !box.clientHeight) return;
  t.style.flex = "";
  t.style.height = "";
  const H = box.clientHeight;
  const room = t.clientHeight;
  const apply = (px) => {
    box.style.setProperty("--title-fit", `${((100 * px) / H).toFixed(3)}cqh`);
    box.style.setProperty("--fit-lines", "99");
  };
  let lo = TITLE_MIN;
  let hi = Math.max(TITLE_MIN, Math.min(TITLE_MAX_H * H, 0.2 * box.clientWidth, 200));
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    apply(mid);
    if (t.scrollHeight <= room + 1) lo = mid;
    else hi = mid - 1;
  }
  apply(lo);
  box.style.setProperty("--fit-lines", String(Math.max(1, Math.floor(room / (lo * TITLE_LINE)))));
  // Words wrap in whole lines, so a little room is usually left over: shrink
  // the title to its text, and the caption centres the source line and the
  // headline together, the spare room above and below instead of all below.
  if (t.scrollHeight <= room + 1) {
    const range = document.createRange();
    range.selectNodeContents(t);
    const text = Math.ceil(range.getBoundingClientRect().height);
    if (text > 0 && text < room) {
      t.style.flex = "0 0 auto";
      t.style.height = `${text}px`;
    }
  }
}

// The caption's look from the settings: colours as #rrggbb, parts shown.
const LOOK = { bg: "#000000", fg: "#ffffff", tag: true, date: true, count: true, title: true, summary: true };

export function readLook(raw) {
  const out = { ...LOOK };
  if (raw && typeof raw === "object") {
    for (const key of ["bg", "fg"]) {
      if (typeof raw[key] === "string" && /^#[0-9a-f]{6}$/i.test(raw[key])) out[key] = raw[key].toLowerCase();
    }
    for (const key of ["tag", "date", "count", "title", "summary"]) {
      if (raw[key] === false) out[key] = false;
    }
  }
  return out;
}

// "#102030" at an opacity, for the gradient's stops.
function rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

const STYLE = `
  :host { font-family: inherit; }
  .ain { position: relative; width: 100%; height: 100%; overflow: hidden; gap: 0; background: var(--ain-bg, #000); color: var(--ain-fg, #fff); }
  .art { position: absolute; inset: 0; }
  .art img { display: block; width: 100%; height: 100%; object-fit: contain; object-position: center top; }
  .cap {
    position: absolute; left: 0; right: 0; bottom: 0;
    display: flex; flex-direction: column; gap: 1.1cqw;
    padding: 9cqw 5cqw 4.5cqw;
    background: var(--ain-gradient, linear-gradient(to top, rgba(0,0,0,0.95) 0%, rgba(0,0,0,0.9) 60%, rgba(0,0,0,0.55) 85%, rgba(0,0,0,0) 100%));
  }
  .meta {
    display: flex; align-items: center; gap: 1.2em; min-width: 0;
    font-size: clamp(9px, 1.5cqw, 24px); font-weight: 800;
    letter-spacing: 0.14em; text-transform: uppercase; color: var(--ain-fg, #fff);
  }
  .badge { background: var(--ain-fg, #fff); color: var(--ain-bg, #000); padding: 0.15em 0.6em; white-space: nowrap; }
  .meta .when { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; opacity: 0.87; }
  .meta .count { margin-left: auto; opacity: 0.67; white-space: nowrap; }
  .title {
    margin: 0; font-size: clamp(13px, 4.4cqw, 72px); font-weight: 800; line-height: 1.12;
    display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden;
  }
  .summary {
    margin: 0; font-size: clamp(11px, 2.3cqw, 36px); font-weight: 500; line-height: 1.45; opacity: 0.92;
    display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: var(--lines, 6); overflow: hidden;
  }

  /* Wide cells with a tall picture: the picture on the left at its own
     shape (9:16 unless the story says), text beside it. */
  /* One row the cell's height: the picture's width follows from it (9:16),
     never the other way round, which on a short landscape screen made the
     row taller than the cell and cut the summary off at the foot. */
  .is-wide { display: grid; grid-template-columns: auto minmax(0, 1fr); grid-template-rows: minmax(0, 100%); }
  .is-wide .art { position: relative; inset: auto; height: 100%; width: auto; aspect-ratio: var(--art-ratio, 9 / 16); max-width: 55cqw; min-height: 0; }
  .is-wide .art img { object-position: center; }
  /* A short landscape screen: the picture at the left, filling the height
     bar a small margin, as wide as its shape asks (60% of the width at most;
     a wider picture is cropped at the sides). Centred, so the padding is even. */
  .is-small .art {
    align-self: start; margin: 12cqh 0 0 12cqh; height: 76cqh; aspect-ratio: auto; max-width: none;
    width: min(calc(var(--art-ratio, 0.5625) * 76cqh), 60cqw);
  }
  .is-small .art img { object-fit: cover; }
  .is-small .cap { padding: 6cqh 5cqh 6cqh 6cqh; }
  /* No summary: the headline takes the whole text area, sized to fit it. */
  .ain.fit-title .title { flex: 1 1 0; min-height: 0; font-size: var(--title-fit, 6cqh); -webkit-line-clamp: var(--fit-lines, 99); }
  .is-wide .cap {
    position: static; justify-content: center; gap: 2.2cqh;
    padding: 6cqh 5cqh; background: var(--ain-bg, #000);
  }
  .is-wide .meta { font-size: clamp(9px, 2.6cqh, 22px); }
  .is-wide .title { font-size: clamp(13px, 6cqh, 64px); -webkit-line-clamp: 4; }
  .is-wide .summary { font-size: clamp(11px, 3.4cqh, 32px); }

  /* No picture (no provider key yet): a text poster. */
  .no-art .cap { top: 0; justify-content: center; background: var(--ain-bg, #000); padding: 6cqw; }
  /* Nothing to caption: no band over the picture. */
  .no-cap .cap { display: none; }
  .no-art .title { -webkit-line-clamp: 5; }

  .size-xs .meta, .size-xs .summary { display: none; }
  .size-xs .cap { padding: 14cqw 6cqw 5cqw; }
  .size-xs .title { font-size: clamp(12px, 8cqw, 18px); }
  .no-summary .summary { display: none; }

  .empty {
    position: absolute; inset: 0; display: grid; place-content: center; justify-items: center;
    gap: 0.6em; padding: 8cqmin; text-align: center; background: var(--ain-bg, #000); color: var(--ain-fg, #fff);
  }
  .empty i { font-size: clamp(28px, 18cqmin, 120px); }
  .empty strong { font-size: clamp(13px, 6cqmin, 40px); }
  .empty span { font-size: clamp(10px, 3.6cqmin, 24px); opacity: 0.75; max-width: 40em; }
  .size-xs .empty span { display: none; }
`;

export default function render(shadow, ctx) {
  const t = ctx?.t || ((key, fallback) => fallback ?? key);
  const data = ctx?.data ?? {};
  const opts = ctx?.cell?.options || {};
  const size = ctx?.cell?.size || "md";
  const w = Number(ctx?.cell?.w) || 0;
  const h = Number(ctx?.cell?.h) || 0;
  const head = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">
    <link rel="stylesheet" href="/static/icons/phosphor/bold/style.css">
    <style>${STYLE}</style>`;

  if (data.error) {
    shadow.innerHTML = `${head}
      <div class="w" data-widget="news_illustrated">
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${escapeHtml(t("widget_title", "AI Illustrated News"))}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  if (!data.title) {
    const empty = readLook(data.look);
    const heading = data.running
      ? t("making", "Making today's stories…")
      : t("no_stories", "No stories yet");
    const detail = data.running
      ? ""
      : t("no_stories_detail", "Stories are made once a day after {time}, or now with Generate now on the widget's page.")
          .replace("{time}", data.run_at || "06:05");
    shadow.innerHTML = `${head}
      <div class="w is-bleed ain size-${escapeHtml(size)}" data-widget="news_illustrated" style="--ain-bg:${empty.bg}; --ain-fg:${empty.fg}">
        <div class="empty">
          <i class="ph-bold ph-newspaper-clipping" aria-hidden="true"></i>
          <strong>${escapeHtml(heading)}</strong>
          ${detail ? `<span>${escapeHtml(detail)}</span>` : ""}
        </div>
      </div>`;
    return;
  }

  const look = readLook(data.look);
  const hasArt = Boolean(data.image);
  const imgW = Number(data.image_w) || 0;
  const imgH = Number(data.image_h) || 0;
  const artRatio = imgW > 0 && imgH > 0 ? imgW / imgH : 9 / 16;
  // Side by side when a tall picture would sit small in a wide cell, or on
  // any short landscape screen; a wide picture on a big wide frame is a
  // poster like any other.
  const { wide, small } = layoutFor(w, h, hasArt, artRatio);
  const showTitle = look.title;
  const lines = summaryLines(w, h, wide, hasArt, showTitle ? String(data.title || "").length : 0, artRatio);
  const showSummary = opts.show_summary !== false && look.summary && data.summary && lines >= 2;
  const showSource = opts.show_source !== false;
  const date = showSource && look.date ? formatDate(data.date, ctx?.locale) : "";
  const tag = showSource && look.tag ? String(data.source || "") : "";
  const count = showSource && look.count && Number(data.count) > 1 ? `${data.index} / ${data.count}` : "";
  const showMeta = Boolean(tag || date || count);
  const hasCap = showMeta || showTitle || showSummary;
  // With no summary the headline grows to fill the text area (beside the
  // picture, or on a text poster); on a picture it stays a caption.
  const fit = showTitle && !showSummary && size !== "xs" && (wide || !hasArt);
  const fitPx = fit ? titleSize(w, h, wide, hasArt, artRatio, String(data.title || "").length, showMeta) : 0;
  const classes = [
    "w", "is-bleed", "ain", `size-${size}`,
    wide ? "is-wide" : "is-tall",
    small ? "is-small" : "",
    fit ? "fit-title" : "",
    hasArt ? "" : "no-art",
    showSummary ? "" : "no-summary",
    hasCap || !hasArt ? "" : "no-cap",
  ].filter(Boolean).join(" ");
  const gradient = `linear-gradient(to top, ${rgba(look.bg, 0.95)} 0%, ${rgba(look.bg, 0.9)} 60%, ${rgba(look.bg, 0.55)} 85%, ${rgba(look.bg, 0)} 100%)`;
  const vars = `--lines:${lines}; --ain-bg:${look.bg}; --ain-fg:${look.fg}; --ain-gradient:${gradient}; --art-ratio:${artRatio.toFixed(5)}${fit && h ? `; --title-fit:${((100 * fitPx) / h).toFixed(3)}cqh` : ""}`;

  shadow.innerHTML = `${head}
    <div class="${escapeHtml(classes)}" data-widget="news_illustrated" style="${escapeHtml(vars)}">
      ${hasArt ? `<div class="art"><img src="${escapeHtml(data.image)}" alt="${escapeHtml(data.title)}"></div>` : ""}
      <div class="cap">
        ${showMeta ? `<div class="meta">
            ${tag ? `<span class="badge">${escapeHtml(tag)}</span>` : ""}
            ${date ? `<span class="when">${escapeHtml(date)}</span>` : ""}
            ${count ? `<span class="count">${escapeHtml(count)}</span>` : ""}
          </div>` : ""}
        ${showTitle ? `<h2 class="title">${escapeHtml(data.title)}</h2>` : ""}
        ${showSummary ? `<p class="summary">${escapeHtml(data.summary)}</p>` : ""}
      </div>
    </div>`;
  if (fit) {
    fitTitle(shadow);
    // The page's fonts and styles may land after this: settle again then.
    document.fonts?.ready?.then(() => fitTitle(shadow));
    for (const link of shadow.querySelectorAll("link")) link.addEventListener("load", () => fitTitle(shadow));
  }
}
