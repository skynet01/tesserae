/* Battery chart component (templates/_battery_chart.html).
 *
 * Draws every `canvas.battery-chart` on the page: the battery line plus a dot
 * at each wake-up. Inputs come from the canvas's data attributes, built by
 * app/battery_chart.py:
 *
 *   data-series       [{t_ms, pct}]                    the line
 *   data-wakes        [{t_ms, pct, label, reason}]     wake-up dots
 *   data-window-days  x axis spans this many days
 *
 * Hovering the line or a dot shows a tooltip with the time and charge, plus
 * the wake reason when the pointer is on a dot. The same tooltip opens from the
 * keyboard: Tab to the chart, Left / Right step through the wake-ups (or the
 * samples when there are none), Home / End jump to the ends, Escape closes it.
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
  var ROUTINE = "Check-in";

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
    return {
      muted: token("--t-muted", "#71706c"),
      fg: token("--t-fg", "#18181b"),
      accent: token("--t-accent", "#0d8c7e"),
      // Tooltip: a raised surface with a border, so it contrasts with the
      // card behind it in either theme (the old box used --t-fg as its
      // background with white text, which is light-on-light in dark mode).
      tipBg: token("--t-surface-raised", token("--t-surface", "#ffffff")),
      tipBorder: token("--t-border-strong", token("--t-border", "#d4d4d8")),
      // Routine dots sit on the card; fill them with its colour.
      dotFill: token("--t-inset", token("--t-surface", "#ffffff")),
    };
  }

  function isNotable(wake) {
    return !!wake && wake.label !== ROUTINE;
  }

  function fmtWhen(ms) {
    return window.tesseraeComponents.clockTime(ms, { date: true });
  }

  function build(canvas) {
    if (canvas.dataset.batteryChartBound === "1") return;
    if (typeof Chart === "undefined") return;
    var series, wakes;
    try {
      series = JSON.parse(canvas.dataset.series || "[]");
      wakes = JSON.parse(canvas.dataset.wakes || "[]");
    } catch (e) {
      return;
    }
    if (!Array.isArray(series) || series.length === 0) return;
    if (!Array.isArray(wakes)) wakes = [];
    canvas.dataset.batteryChartBound = "1";

    var pal = readPalette();
    var points = series.map(function (p) { return { x: p.t_ms, y: p.pct }; });
    var wakePoints = wakes.map(function (w) { return { x: w.t_ms, y: w.pct }; });
    var windowDays = parseInt(canvas.dataset.windowDays, 10) || 7;
    var nowMs = Date.now();
    var requestedMinX = nowMs - windowDays * DAY;
    var dataMinX = points[0].x;
    var minX = Math.max(requestedMinX, dataMinX - DAY * 0.25);
    var displayedDays = (nowMs - minX) / DAY;
    var stepMs = displayedDays <= 1 ? 4 * 3600 * 1000
               : displayedDays <= 3 ? DAY
               : displayedDays <= 14 ? 2 * DAY
               : displayedDays <= 30 ? 5 * DAY
               : 14 * DAY;
    var wakeAt = function (ms) {
      // The wake at (within a second of) this instant, if any.
      for (var i = 0; i < wakes.length; i++) {
        if (Math.abs(wakes[i].t_ms - ms) < 1000) return wakes[i];
      }
      return null;
    };
    var live = canvas.parentElement.querySelector("[data-battery-chart-live]");

    var chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        datasets: [
          {
            label: "Battery",
            data: points,
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
            pointRadius: function (ctx) { return isNotable(wakes[ctx.dataIndex]) ? 4 : 2; },
            pointHoverRadius: function (ctx) { return isNotable(wakes[ctx.dataIndex]) ? 6 : 4.5; },
            pointBorderWidth: 1.5,
            pointHoverBorderWidth: 2,
            pointBackgroundColor: function (ctx) {
              return isNotable(wakes[ctx.dataIndex]) ? pal.accent : pal.dotFill;
            },
            pointBorderColor: pal.accent,
            pointHoverBackgroundColor: pal.accent,
            pointHoverBorderColor: pal.accent,
          },
        ],
      },
      options: {
        animation: false, maintainAspectRatio: false, responsive: true,
        layout: { padding: { top: 8, right: 8, bottom: 0, left: 0 } },
        interaction: { mode: "nearest", intersect: false, axis: "x" },
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
            titleColor: pal.muted, bodyColor: pal.fg,
            displayColors: false, cornerRadius: 6, padding: 8,
            titleFont: { size: 11, weight: "normal" }, bodyFont: { size: 13, weight: "bold" },
            // On a dot the line's own point is also "nearest"; show one entry.
            filter: function (item, _i, items) {
              if (item.datasetIndex === 1) return true;
              return !items.some(function (o) { return o.datasetIndex === 1; });
            },
            callbacks: {
              title: function (items) {
                return items.length ? fmtWhen(items[0].parsed.x) : "";
              },
              label: function (item) {
                var lines = [item.parsed.y + "%"];
                var wake = item.datasetIndex === 1 ? wakes[item.dataIndex] : wakeAt(item.parsed.x);
                if (wake) lines.push(wake.label);
                return lines;
              },
            },
          },
        },
      },
    });

    // -- theme: re-read tokens when the theme / UI design flips -------------
    function restyle() {
      pal = readPalette();
      var ds = chart.data.datasets;
      ds[0].borderColor = pal.accent;
      ds[0].pointHoverBackgroundColor = pal.accent;
      ds[0].pointHoverBorderColor = pal.accent;
      ds[1].pointBorderColor = pal.accent;
      ds[1].pointHoverBackgroundColor = pal.accent;
      ds[1].pointHoverBorderColor = pal.accent;
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
      chart.update("none");
    }
    watchTheme(restyle);

    // -- keyboard: arrows walk the wake-ups (or the samples) ----------------
    var dsIndex = wakes.length ? 1 : 0;
    var count = wakes.length ? wakes.length : points.length;
    var cursor = -1;

    function show(i) {
      cursor = Math.max(0, Math.min(count - 1, i));
      var el = chart.getDatasetMeta(dsIndex).data[cursor];
      if (!el) return;
      var active = [{ datasetIndex: dsIndex, index: cursor }];
      chart.setActiveElements(active);
      chart.tooltip.setActiveElements(active, { x: el.x, y: el.y });
      chart.update("none");
      if (live) {
        var src = wakes.length ? wakes[cursor] : null;
        var when = fmtWhen(wakes.length ? src.t_ms : points[cursor].x);
        var pct = wakes.length ? src.pct : points[cursor].y;
        live.textContent = when + ", " + pct + "%" + (src ? ", " + src.label : "");
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
