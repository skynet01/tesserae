// news_illustrated, a movie-poster news story: the AI illustration fills the
// cell (contained on black, so speech bubbles at the edges survive), with the
// headline and summary over a dark gradient at the foot. A wide cell puts the
// text beside the picture instead, since a caption over the bottom of a short
// picture would hide most of it. Everything sizes from the cell, so a full
// 1440x2560 portrait panel and a small tile read the same way.

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

// Summary lines that fit the caption, worked out in the units the type is set
// in (cqw when tall, cqh when wide) so the answer holds at any cell size or zoom.
function summaryLines(w, h, wide, hasArt) {
  if (!w || !h) return 6;
  if (wide) return hasArt ? 12 : 14;
  const heightInCqw = (100 * h) / w;
  const room = (hasArt ? 0.3 : 0.85) * heightInCqw - 26;
  return Math.max(0, Math.min(14, Math.floor(room / 3.4)));
}

const STYLE = `
  :host { font-family: inherit; }
  .ain { position: relative; width: 100%; height: 100%; overflow: hidden; gap: 0; background: #000; color: #fff; }
  .art { position: absolute; inset: 0; }
  .art img { display: block; width: 100%; height: 100%; object-fit: contain; object-position: center top; }
  .cap {
    position: absolute; left: 0; right: 0; bottom: 0;
    display: flex; flex-direction: column; gap: 1.1cqw;
    padding: 9cqw 5cqw 4.5cqw;
    background: linear-gradient(to top, rgba(0,0,0,0.95) 0%, rgba(0,0,0,0.9) 60%, rgba(0,0,0,0.55) 85%, rgba(0,0,0,0) 100%);
  }
  .meta {
    display: flex; align-items: center; gap: 1.2em; min-width: 0;
    font-size: clamp(9px, 1.5cqw, 24px); font-weight: 800;
    letter-spacing: 0.14em; text-transform: uppercase; color: #ddd;
  }
  .badge { background: #fff; color: #000; padding: 0.15em 0.6em; white-space: nowrap; }
  .meta .when { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .meta .count { margin-left: auto; color: #aaa; white-space: nowrap; }
  .title {
    margin: 0; font-size: clamp(13px, 4.4cqw, 72px); font-weight: 800; line-height: 1.12;
    display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: 3; overflow: hidden;
  }
  .summary {
    margin: 0; font-size: clamp(11px, 2.3cqw, 36px); font-weight: 500; line-height: 1.45; color: #e8e8e8;
    display: -webkit-box; -webkit-box-orient: vertical; -webkit-line-clamp: var(--lines, 6); overflow: hidden;
  }

  /* Wide cells: picture on the left at its own 9:16, text beside it. */
  .is-wide { display: grid; grid-template-columns: auto minmax(0, 1fr); }
  .is-wide .art { position: relative; inset: auto; height: 100%; aspect-ratio: 9 / 16; max-width: 55cqw; }
  .is-wide .cap {
    position: static; justify-content: center; gap: 2.2cqh;
    padding: 6cqh 5cqh; background: #000;
  }
  .is-wide .meta { font-size: clamp(9px, 2.6cqh, 22px); }
  .is-wide .title { font-size: clamp(13px, 6cqh, 64px); -webkit-line-clamp: 4; }
  .is-wide .summary { font-size: clamp(11px, 3.4cqh, 32px); }

  /* No picture (no provider key yet): a text poster. */
  .no-art .cap { top: 0; justify-content: center; background: #000; padding: 6cqw; }
  .no-art .title { -webkit-line-clamp: 5; }

  .size-xs .meta, .size-xs .summary { display: none; }
  .size-xs .cap { padding: 14cqw 6cqw 5cqw; }
  .size-xs .title { font-size: clamp(12px, 8cqw, 18px); }
  .no-summary .summary { display: none; }

  .empty {
    position: absolute; inset: 0; display: grid; place-content: center; justify-items: center;
    gap: 0.6em; padding: 8cqmin; text-align: center; background: #000; color: #fff;
  }
  .empty i { font-size: clamp(28px, 18cqmin, 120px); }
  .empty strong { font-size: clamp(13px, 6cqmin, 40px); }
  .empty span { font-size: clamp(10px, 3.6cqmin, 24px); color: #bbb; max-width: 40em; }
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
        <div class="w-title"><i class="ph-bold ph-warning-circle"></i><h3>${escapeHtml(t("widget_title", "AI News"))}</h3></div>
        <div class="w-body"><p class="u-muted">${escapeHtml(data.error)}</p></div>
      </div>`;
    return;
  }

  if (!data.title) {
    const heading = data.running
      ? t("making", "Making today's stories…")
      : t("no_stories", "No stories yet");
    const detail = data.running
      ? ""
      : t("no_stories_detail", "Stories are made once a day after {time}, or now with Generate now on the widget's page.")
          .replace("{time}", data.run_at || "06:05");
    shadow.innerHTML = `${head}
      <div class="w is-bleed ain size-${escapeHtml(size)}" data-widget="news_illustrated">
        <div class="empty">
          <i class="ph-bold ph-newspaper-clipping" aria-hidden="true"></i>
          <strong>${escapeHtml(heading)}</strong>
          ${detail ? `<span>${escapeHtml(detail)}</span>` : ""}
        </div>
      </div>`;
    return;
  }

  const hasArt = Boolean(data.image);
  const wide = hasArt && w > h * 1.15;
  const lines = summaryLines(w, h, wide, hasArt);
  const showSummary = opts.show_summary !== false && data.summary && lines >= 2;
  const showMeta = opts.show_source !== false;
  const date = formatDate(data.date, ctx?.locale);
  const count = Number(data.count) > 1 ? `${data.index} / ${data.count}` : "";
  const classes = [
    "w", "is-bleed", "ain", `size-${size}`,
    wide ? "is-wide" : "is-tall",
    hasArt ? "" : "no-art",
    showSummary ? "" : "no-summary",
  ].filter(Boolean).join(" ");

  shadow.innerHTML = `${head}
    <div class="${escapeHtml(classes)}" data-widget="news_illustrated" style="--lines:${lines}">
      ${hasArt ? `<div class="art"><img src="${escapeHtml(data.image)}" alt="${escapeHtml(data.title)}"></div>` : ""}
      <div class="cap">
        ${showMeta ? `<div class="meta">
            ${data.source ? `<span class="badge">${escapeHtml(data.source)}</span>` : ""}
            ${date ? `<span class="when">${escapeHtml(date)}</span>` : ""}
            ${count ? `<span class="count">${escapeHtml(count)}</span>` : ""}
          </div>` : ""}
        <h2 class="title">${escapeHtml(data.title)}</h2>
        ${showSummary ? `<p class="summary">${escapeHtml(data.summary)}</p>` : ""}
      </div>
    </div>`;
}
