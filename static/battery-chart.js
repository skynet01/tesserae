/* Battery chart component (templates/_battery_chart.html).
 *
 * Draws every `canvas.battery-chart` on the page: the battery line, a dot at
 * each wake-up, and a hatched band where the device sent no readings.
 * Inputs come from the canvas's data attributes, built by app/battery_chart.py:
 *
 *   data-series       [{t_ms, pct}]                      the line
 *   data-wakes        [{t_ms, pct, cat, label, detail, reason}]   wake-up dots
 *   data-gaps         [{start_ms, end_ms, kind}]         spans with no readings
 *   data-window-days  x axis spans this many days
 *
 * A dot's shape and colour say why the device woke (STYLES below; the legend
 * under the chart is drawn from the same table in dx-battery.css). Hovering the
 * line, a dot or a band shows a tooltip: the time and charge, the cause, and the
 * raw device reason in muted text; a band says "Asleep 23:02-09:33 (quiet
 * hours), no readings" or "No readings 14:10-16:40 (offline)". The same tooltip
 * opens from the keyboard: Tab to the chart, Left / Right step through the
 * wake-ups (or the samples when there are none), Home / End jump to the ends,
 * Escape closes it.
 *
 * A gap is never drawn as measured data: the line breaks, and a muted dashed
 * connector joins the readings either side across the hatched band.
 *
 * Colours come from the theme tokens on <html> (--t-*), read again whenever the
 * theme or UI design changes, so the chart reads in light and dark, Classic and
 * Paper, without per-theme code.
 *
 * Needs Chart.js (UMD) and static/admin-chart.js loaded first. Safe to load
 * twice, and to call `TesseraeBatteryChart.init(root)` after inserting new
 * cards; a canvas is only ever drawn once.
 */
(function () {
  "use strict";

  var DAY = 86400 * 1000;
  var LINE = 0, WAKES = 1, BRIDGES = 2, GAPS = 3;

  /* Dot per cause. `tok` is a theme token; shape is the Chart.js pointStyle.
     Shape carries the meaning as much as colour does: Paper's accent, chart-2
     and danger are all red. Keep in step with .dx-wake-key in dx-battery.css
     and _wake_shape in _battery_chart.html. */
  var STYLES = {
    scheduled: { style: "circle", r: 2.2, hover: 4.5, tok: "--t-accent", filled: false, bw: 1.5 },
    lineup: { style: "circle", r: 3.6, hover: 5.5, tok: "--t-accent", filled: true, bw: 1.5 },
    ha: { style: "rectRot", r: 4.5, hover: 6.5, tok: "--t-chart-1", filled: true, bw: 1.5 },
    device: { style: "triangle", r: 4.5, hover: 6.5, tok: "--t-chart-2", filled: true, bw: 1.5 },
    manual: { style: "rect", r: 3.8, hover: 5.8, tok: "--t-chart-3", filled: true, bw: 1.5 },
    webhook: { style: "star", r: 5, hover: 7, tok: "--t-chart-other", filled: true, bw: 1.5 },
    problem: { style: "crossRot", r: 4.5, hover: 6.5, tok: "--t-warn", filled: false, bw: 2.6 },
    other: { style: "circle", r: 3, hover: 5, tok: "--t-chart-other", filled: true, bw: 1.5 },
  };

  function token(name, fallback) {
    if (window.TesseraeCharts) return window.TesseraeCharts.token(name, fallback);
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    return (v || fallback).trim();
  }

  function rgba(color, alpha) {
    if (window.TesseraeCharts) return window.TesseraeCharts.rgba(color, alpha);
    return "rgba(80,100,140," + alpha + ")";
  }

  function readPalette() {
    var pal = {
      muted: token("--t-muted", "#71706c"),
      fg: token("--t-fg", "#18181b"),
      accent: token("--t-accent", "#0d8c7e"),
      // Tooltip: a raised surface with a border, so it contrasts with the
      // card behind it in either theme (the old box used --t-fg as its
      // background with white text, which is light-on-light in dark mode).
      tipBg: token("--t-surface-raised", token("--t-surface", "#ffffff")),
      tipBorder: token("--t-border-strong", token("--t-border", "#d4d4d8")),
      // Hollow dots sit on the card; fill them with its colour.
      dotFill: token("--t-inset", token("--t-surface", "#ffffff")),
      cat: {},
    };
    Object.keys(STYLES).forEach(function (cat) {
      pal.cat[cat] = token(STYLES[cat].tok, pal.accent);
    });
    return pal;
  }

  function fmtWhen(ms) {
    return window.tesseraeComponents.clockTime(ms, { date: true });
  }

  /* "23:02-09:33", with dates when the span is longer than a day. */
  function fmtSpan(gap) {
    var withDate = gap.end_ms - gap.start_ms > DAY;
    var c = window.tesseraeComponents.clockTime;
    return c(gap.start_ms, { date: withDate }) + "–" + c(gap.end_ms, { date: withDate });
  }

  function gapLabel(gap) {
    return gap.kind === "asleep"
      ? "Asleep " + fmtSpan(gap) + " (quiet hours), no readings"
      : "No readings " + fmtSpan(gap) + " (offline)";
  }

  /* A small diagonal-hatch tile for the no-readings band. */
  function hatch(color) {
    var tile = document.createElement("canvas");
    tile.width = tile.height = 8;
    var t = tile.getContext("2d");
    t.strokeStyle = rgba(color, 0.4);
    t.lineWidth = 1;
    t.beginPath();
    t.moveTo(-1, 9);
    t.lineTo(9, -1);
    t.stroke();
    return tile.getContext("2d").createPattern(tile, "repeat");
  }

  function build(canvas) {
    if (canvas.dataset.batteryChartBound === "1") return;
    if (typeof Chart === "undefined") return;
    var series, wakes, gaps;
    try {
      series = JSON.parse(canvas.dataset.series || "[]");
      wakes = JSON.parse(canvas.dataset.wakes || "[]");
      gaps = JSON.parse(canvas.dataset.gaps || "[]");
    } catch (e) {
      return;
    }
    if (!Array.isArray(series) || series.length === 0) return;
    if (!Array.isArray(wakes)) wakes = [];
    if (!Array.isArray(gaps)) gaps = [];
    canvas.dataset.batteryChartBound = "1";

    var pal = readPalette();
    var windowDays = parseInt(canvas.dataset.windowDays, 10) || 7;
    var nowMs = Date.now();
    var requestedMinX = nowMs - windowDays * DAY;
    var minX = Math.max(requestedMinX, series[0].t_ms - DAY * 0.25);
    var displayedDays = (nowMs - minX) / DAY;
    var stepMs = displayedDays <= 1 ? 4 * 3600 * 1000
               : displayedDays <= 3 ? DAY
               : displayedDays <= 14 ? 2 * DAY
               : displayedDays <= 30 ? 5 * DAY
               : 14 * DAY;

    // The line breaks (a null point) wherever a gap separates two samples,
    // and a dashed bridge joins the real readings either side of it. Gap
    // anchors are invisible points mid-gap that hover resolves to.
    var gapStarts = {};
    gaps.forEach(function (g, i) { gapStarts[g.start_ms] = i; });
    var linePoints = [], navIndex = [], bridges = [], anchors = [];
    series.forEach(function (p, i) {
      navIndex.push(linePoints.length);
      linePoints.push({ x: p.t_ms, y: p.pct });
      var gi = gapStarts[p.t_ms];
      if (gi === undefined) return;
      var g = gaps[gi], next = series[i + 1];
      var endPct = next ? next.pct : p.pct;
      if (next) {
        linePoints.push({ x: (g.start_ms + g.end_ms) / 2, y: null });
        bridges.push({ x: g.start_ms, y: p.pct }, { x: g.end_ms, y: endPct }, { x: g.end_ms, y: null });
      }
      anchors[gi] = { x: (g.start_ms + g.end_ms) / 2, y: (p.pct + endPct) / 2 };
    });
    anchors = gaps.map(function (g, i) {
      return anchors[i] || { x: (g.start_ms + g.end_ms) / 2, y: series[series.length - 1].pct };
    });
    var wakePoints = wakes.map(function (w) { return { x: w.t_ms, y: w.pct }; });
    var hatchPattern = hatch(pal.muted);

    function style(i) { return STYLES[(wakes[i] || {}).cat] || STYLES.other; }
    function wakeAt(ms) {
      for (var i = 0; i < wakes.length; i++) {
        if (Math.abs(wakes[i].t_ms - ms) < 1000) return wakes[i];
      }
      return null;
    }

    // Hover target: inside a gap, the gap; else the closest wake dot or line
    // point by x (a dot wins a tie, so hovering one never reads as the line).
    // Own mode because the stock "nearest" would also pick the bridge points.
    Chart.Interaction.modes.batteryChart = function (chart, e) {
      var xs = chart.scales.x;
      var value = xs.getValueForPixel(e.x);
      for (var gi = 0; gi < gaps.length; gi++) {
        if (value > gaps[gi].start_ms && value < gaps[gi].end_ms) {
          var anchor = chart.getDatasetMeta(GAPS).data[gi];
          if (anchor) return [{ element: anchor, datasetIndex: GAPS, index: gi }];
        }
      }
      var best = null, bestD = Infinity;
      [WAKES, LINE].forEach(function (di) {
        var data = chart.getDatasetMeta(di).data;
        var raw = chart.data.datasets[di].data;
        for (var i = 0; i < data.length; i++) {
          if (raw[i].y === null) continue;
          var d = Math.abs(data[i].x - e.x);
          if (d < bestD) { bestD = d; best = { element: data[i], datasetIndex: di, index: i }; }
        }
      });
      return best ? [best] : [];
    };

    var hatchBands = {
      id: "batteryGapBands",
      beforeDatasetsDraw: function (chart) {
        var area = chart.chartArea;
        if (!area || !gaps.length) return;
        var ctx = chart.ctx, xs = chart.scales.x;
        ctx.save();
        gaps.forEach(function (g) {
          var x0 = Math.max(area.left, xs.getPixelForValue(g.start_ms));
          var x1 = Math.min(area.right, xs.getPixelForValue(g.end_ms));
          if (x1 <= x0) return;
          ctx.fillStyle = rgba(pal.muted, 0.07);
          ctx.fillRect(x0, area.top, x1 - x0, area.bottom - area.top);
          ctx.fillStyle = hatchPattern;
          ctx.fillRect(x0, area.top, x1 - x0, area.bottom - area.top);
        });
        ctx.restore();
      },
    };

    var live = canvas.parentElement.querySelector("[data-battery-chart-live]");

    var chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      plugins: [hatchBands],
      data: {
        datasets: [
          {
            label: "Battery",
            data: linePoints,
            spanGaps: false,
            borderColor: pal.accent,
            backgroundColor: function (ctx) {
              var area = ctx.chart.chartArea;
              if (!area) return "transparent";
              var g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
              g.addColorStop(0, rgba(pal.accent, 0.22));
              g.addColorStop(0.7, rgba(pal.accent, 0.04));
              g.addColorStop(1, rgba(pal.accent, 0));
              return g;
            },
            tension: 0.25, pointRadius: 0, pointHoverRadius: 4,
            pointHoverBackgroundColor: pal.accent, pointHoverBorderColor: pal.accent,
            borderWidth: 2, fill: "origin",
          },
          {
            label: "Wake-ups",
            data: wakePoints,
            showLine: false,
            order: -1, // drawn above the line
            pointStyle: function (ctx) { return style(ctx.dataIndex).style; },
            pointRadius: function (ctx) { return style(ctx.dataIndex).r; },
            pointHoverRadius: function (ctx) { return style(ctx.dataIndex).hover; },
            pointBorderWidth: function (ctx) { return style(ctx.dataIndex).bw; },
            pointHoverBorderWidth: function (ctx) { return style(ctx.dataIndex).bw; },
            pointBackgroundColor: function (ctx) {
              var st = style(ctx.dataIndex);
              return st.filled ? pal.cat[(wakes[ctx.dataIndex] || {}).cat] || pal.accent : pal.dotFill;
            },
            pointBorderColor: function (ctx) {
              return pal.cat[(wakes[ctx.dataIndex] || {}).cat] || pal.accent;
            },
            pointHoverBackgroundColor: function (ctx) {
              return pal.cat[(wakes[ctx.dataIndex] || {}).cat] || pal.accent;
            },
            pointHoverBorderColor: function (ctx) {
              return pal.cat[(wakes[ctx.dataIndex] || {}).cat] || pal.accent;
            },
          },
          {
            label: "No readings",
            data: bridges,
            spanGaps: false,
            borderColor: rgba(pal.muted, 0.85),
            borderDash: [3, 4], borderWidth: 1.5, fill: false,
            pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0, tension: 0,
          },
          {
            label: "Gap anchors",
            data: anchors,
            showLine: false,
            pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0,
          },
        ],
      },
      options: {
        animation: false, maintainAspectRatio: false, responsive: true,
        layout: { padding: { top: 8, right: 8, bottom: 0, left: 0 } },
        interaction: { mode: "batteryChart", intersect: false },
        scales: {
          x: {
            type: "linear", min: minX, max: nowMs,
            ticks: {
              stepSize: stepMs, color: pal.muted, font: { size: 11 }, padding: 6,
              callback: function (v) {
                var d = new Date(v);
                if (displayedDays <= 1) return window.tesseraeComponents.clockTime(d);
                return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
              },
            },
            grid: { display: false }, border: { color: rgba(pal.muted, 0.35) },
          },
          y: {
            min: 0, max: 100,
            ticks: {
              color: pal.muted, stepSize: 25, font: { size: 11 }, padding: 6,
              callback: function (v) { return v + "%"; },
            },
            grid: { color: rgba(pal.muted, 0.1), drawTicks: false }, border: { display: false },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: pal.tipBg, borderColor: pal.tipBorder, borderWidth: 1,
            titleColor: pal.muted, bodyColor: pal.fg, footerColor: pal.muted,
            displayColors: false, cornerRadius: 6, padding: 8,
            titleFont: { size: 11, weight: "normal" }, bodyFont: { size: 13, weight: "bold" },
            footerFont: { size: 11, weight: "normal" },
            callbacks: {
              title: function (items) {
                if (!items.length || items[0].datasetIndex === GAPS) return "";
                return fmtWhen(items[0].parsed.x);
              },
              label: function (item) {
                if (item.datasetIndex === GAPS) return gapLabel(gaps[item.dataIndex]);
                var lines = [item.parsed.y + "%"];
                var wake = item.datasetIndex === WAKES ? wakes[item.dataIndex] : wakeAt(item.parsed.x);
                if (wake) {
                  lines.push(wake.label);
                  if (wake.detail) lines.push(wake.detail);
                }
                return lines;
              },
              footer: function (items) {
                if (!items.length || items[0].datasetIndex === GAPS) return "";
                var item = items[0];
                var wake = item.datasetIndex === WAKES ? wakes[item.dataIndex] : wakeAt(item.parsed.x);
                return wake && wake.reason ? "Device reported: " + wake.reason : "";
              },
            },
          },
        },
      },
    });

    // -- theme: re-read tokens when the theme / UI design flips -------------
    function restyle() {
      pal = readPalette();
      hatchPattern = hatch(pal.muted);
      var ds = chart.data.datasets;
      ds[LINE].borderColor = pal.accent;
      ds[LINE].pointHoverBackgroundColor = pal.accent;
      ds[LINE].pointHoverBorderColor = pal.accent;
      ds[BRIDGES].borderColor = rgba(pal.muted, 0.85);
      var o = chart.options;
      o.scales.x.ticks.color = pal.muted;
      o.scales.x.border.color = rgba(pal.muted, 0.35);
      o.scales.y.ticks.color = pal.muted;
      o.scales.y.grid.color = rgba(pal.muted, 0.1);
      var tip = o.plugins.tooltip;
      tip.backgroundColor = pal.tipBg;
      tip.borderColor = pal.tipBorder;
      tip.titleColor = pal.muted;
      tip.bodyColor = pal.fg;
      tip.footerColor = pal.muted;
      chart.update("none");
    }
    watchTheme(restyle);

    // -- keyboard: arrows walk the wake-ups (or the samples) ----------------
    var dsIndex = wakes.length ? WAKES : LINE;
    var count = wakes.length ? wakes.length : series.length;
    var cursor = -1;

    function show(i) {
      cursor = Math.max(0, Math.min(count - 1, i));
      var index = wakes.length ? cursor : navIndex[cursor];
      var el = chart.getDatasetMeta(dsIndex).data[index];
      if (!el) return;
      var active = [{ datasetIndex: dsIndex, index: index }];
      chart.setActiveElements(active);
      chart.tooltip.setActiveElements(active, { x: el.x, y: el.y });
      chart.update("none");
      if (live) {
        var src = wakes.length ? wakes[cursor] : null;
        var when = fmtWhen(src ? src.t_ms : series[cursor].t_ms);
        var pct = src ? src.pct : series[cursor].pct;
        live.textContent = when + ", " + pct + "%" +
          (src ? ", " + src.label + (src.detail ? ", " + src.detail : "") : "");
      }
    }
    function hide() {
      cursor = -1;
      chart.setActiveElements([]);
      chart.tooltip.setActiveElements([], { x: 0, y: 0 });
      chart.update("none");
      if (live) live.textContent = "";
    }
    canvas.addEventListener("focus", function () {
      // A pointer press focuses too; only open the tooltip for keyboard focus.
      if (canvas.matches(":focus-visible")) show(count - 1);
    });
    canvas.addEventListener("blur", hide);
    canvas.addEventListener("keydown", function (e) {
      var next = null;
      if (e.key === "ArrowLeft") next = cursor < 0 ? count - 1 : cursor - 1;
      else if (e.key === "ArrowRight") next = cursor < 0 ? count - 1 : cursor + 1;
      else if (e.key === "Home") next = 0;
      else if (e.key === "End") next = count - 1;
      else if (e.key === "Escape") { hide(); return; }
      if (next === null) return;
      e.preventDefault();
      show(next);
    });
  }

  // One observer for every chart on the page.
  var restylers = [];
  var observing = false;
  var pending = false;
  function watchTheme(fn) {
    restylers.push(fn);
    if (observing) return;
    observing = true;
    new MutationObserver(function () {
      if (pending) return;
      pending = true;
      requestAnimationFrame(function () {
        pending = false;
        restylers.forEach(function (r) { r(); });
      });
    }).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "data-ui", "data-server-colour", "class", "style"],
    });
  }

  function init(root) {
    (root || document).querySelectorAll("canvas.battery-chart").forEach(build);
  }

  window.TesseraeBatteryChart = { init: init };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { init(); });
  } else {
    init();
  }
})();
