// Dashboard editor v2: the step accordion, plus steps 1 (Display) and
// 2 (Layout). New dashboards (?new=1) unlock steps in order; editing keeps
// one step open at a time with the rest collapsed to a summary line.
(function () {
  "use strict";
  const EV2 = window.EV2;
  if (!EV2) return;
  const root = EV2.root;
  const cfg = EV2.cfg;

  const sections = [...root.querySelectorAll(".ev2-step[data-step]")];
  const KEYS = sections.map((s) => s.dataset.step);
  const WAIT = {
    layout: "Pick a display first.",
    widgets: "Choose a layout first.",
    look: "Optional. Defaults look fine.",
    lineup: "Optional. Add it to a lineup that rotates it with others.",
  };
  const STEP_KEY = "ev2-step:" + EV2.pageId;

  // New mode: how far the walk has got (1..6; 6 = every step seen).
  let progress = 1;
  if (EV2.isNew) {
    try {
      progress = Math.max(1, Number(sessionStorage.getItem(STEP_KEY)) || 1);
    } catch (e) {
      progress = 1;
    }
  }
  const restored = EV2.restored || {};
  let open =
    restored.open !== undefined
      ? restored.open
      : location.hash === "#schedules" && KEYS.includes("schedules")
        ? "schedules"
        : EV2.isNew
          ? KEYS[Math.min(progress, KEYS.length) - 1]
          : "widgets";

  function stateOf(key) {
    const idx = KEYS.indexOf(key) + 1;
    if (key === open) return "open";
    if (!EV2.isNew) return "done";
    return idx <= progress ? "done" : "wait";
  }

  function render() {
    sections.forEach((sec) => {
      const key = sec.dataset.step;
      const st = stateOf(key);
      sec.dataset.state = st;
      const head = sec.querySelector(".ev2-st-h");
      const body = sec.querySelector(".ev2-st-body");
      head.setAttribute("aria-expanded", st === "open" ? "true" : "false");
      // New mode: the step you are on can't be folded away, and later ones
      // wait their turn.
      head.disabled = st === "wait" || (EV2.isNew && st === "open");
      if (body) body.hidden = st !== "open";
      const wait = sec.querySelector("[data-ev2-wait]");
      if (wait) wait.textContent = st === "wait" ? WAIT[key] || "" : "";
    });
    EV2.ui.open = open;
    renderBar();
  }

  function renderBar() {
    if (!EV2.isNew) return;
    const st = document.querySelector("[data-ev2-bar-st]");
    if (!st) return;
    const step = progress;
    const lead = step > 5 ? "All steps done" : step === 5 ? "Last step, optional" : `Step ${step} of 5`;
    const rows = [...root.querySelectorAll(".ev2-cl[data-n]")];
    const filled = rows.filter((r) => r.dataset.widget).length;
    st.innerHTML = "";
    st.append(lead + " · changes save as you go");
    if (rows.length) {
      const b = document.createElement("b");
      b.textContent = `${filled} of ${rows.length} cells filled`;
      st.append(" · ", b);
    }
  }

  function setProgress(n) {
    progress = Math.max(progress, n);
    try {
      sessionStorage.setItem(STEP_KEY, String(progress));
    } catch (e) {
      /* ignore */
    }
  }

  function openStep(key, opts) {
    const o = opts || {};
    if (!KEYS.includes(key) && key !== null) return;
    const was = open;
    if (key && EV2.isNew) setProgress(KEYS.indexOf(key) + 1);
    open = key;
    if (was === "layout" && key !== "layout") setDraw(false);
    if (was === "widgets" && key !== "widgets" && EV2.widgets) EV2.widgets.closeAll();
    render();
    if (o.scroll && key) {
      document.getElementById("ev2-step-" + key)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
    if (o.focus && key) {
      document.getElementById("ev2-h-" + key)?.focus({ preventScroll: true });
    }
  }

  root.addEventListener("click", (ev) => {
    const t = ev.target.closest("[data-ev2-toggle]");
    if (t && !t.disabled) {
      const key = t.dataset.ev2Toggle;
      openStep(open === key ? null : key);
      return;
    }
    const nx = ev.target.closest("[data-ev2-next]");
    if (nx) {
      const idx = KEYS.indexOf(nx.dataset.ev2Next);
      const next = KEYS[idx + 1] || null;
      setProgress(idx + 2);
      openStep(next, { scroll: true });
      if (next === "widgets" && EV2.widgets) EV2.widgets.openFirstEmpty();
      else if (next) document.getElementById("ev2-h-" + next)?.focus({ preventScroll: true });
    }
  });

  // ---------------------------------------------------------- 1 · Display
  function boundIds() {
    return [...root.querySelectorAll("[data-ev2-dev]")]
      .filter((c) => c.dataset.ev2Dev && c.getAttribute("aria-pressed") === "true")
      .map((c) => c.dataset.ev2Dev);
  }
  root.addEventListener("click", (ev) => {
    const card = ev.target.closest("[data-ev2-dev]");
    if (!card) return;
    const id = card.dataset.ev2Dev;
    let ids = boundIds();
    if (!id) {
      if (!ids.length) return; // already on the test display
      ids = [];
    } else {
      ids = ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id];
    }
    // Paint the choice now; the reload brings the new preview shapes.
    root.querySelectorAll("[data-ev2-dev]").forEach((c) => {
      const on = c.dataset.ev2Dev ? ids.includes(c.dataset.ev2Dev) : !ids.length;
      c.classList.toggle("on", on);
      c.setAttribute("aria-pressed", on ? "true" : "false");
    });
    EV2.post(cfg.updateUrl, { device_ids_present: "1", device_ids: ids })
      .then(() => EV2.reload())
      .catch(() => {});
  });

  function saveTestSize(w, h) {
    EV2.post(cfg.updateUrl, { test_display_w: w, test_display_h: h })
      .then(() => EV2.reload())
      .catch(() => {});
  }
  root.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-ev2-size]");
    if (!b || b.getAttribute("aria-pressed") === "true") return;
    const [w, h] = b.dataset.ev2Size.split("x").map(Number);
    b.parentElement.querySelectorAll("[data-ev2-size]").forEach((x) => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
    saveTestSize(w, h);
  });
  root.querySelectorAll("[data-ev2-custom-size]").forEach((inp) => {
    inp.addEventListener("change", () => {
      const w = parseInt(document.getElementById("ev2-tw").value, 10);
      const h = parseInt(document.getElementById("ev2-th").value, 10);
      const ok = (v) => Number.isInteger(v) && v >= 100 && v <= 4000;
      if (!ok(w) || !ok(h)) {
        EV2.setStatus("error", "Use 100 to 4000 px");
        return;
      }
      saveTestSize(w, h);
    });
  });

  // ----------------------------------------------------------- 2 · Layout
  root.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-ev2-layout]");
    if (!b) return;
    const slug = b.dataset.ev2Layout;
    if (slug === "custom") return;
    const slots = Number(b.dataset.slots || 0);
    const losing = [...root.querySelectorAll(".ev2-cl[data-n]")]
      .slice(slots)
      .filter((li) => li.dataset.widget)
      .map((li) => li.dataset.item || li.dataset.widget);
    if (
      losing.length &&
      !window.confirm(
        `${b.dataset.name} has ${slots} cell${slots === 1 ? "" : "s"}, so ` +
          `${losing.length} widget${losing.length === 1 ? "" : "s"} won't fit and will be removed ` +
          `with their settings:\n\n${losing.join("\n")}\n\nDrag them into the cells you're keeping first if you want them.`
      )
    )
      return;
    root.querySelectorAll("[data-ev2-layout]").forEach((x) => {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-pressed", x === b ? "true" : "false");
    });
    EV2.flush()
      .then(() => EV2.post(cfg.layoutUrl, { layout: slug }))
      .then(() => {
        if (EV2.isNew) setProgress(2);
        EV2.reload();
      })
      .catch(() => {});
  });

  // Edit on preview (split / remove happen on the preview, preview.js).
  const drawBtn = root.querySelector("[data-ev2-draw]");
  const drawbar = root.querySelector("[data-ev2-drawbar]");
  let drawing = false;
  function setDraw(on) {
    if (!drawBtn || drawing === on) return;
    drawing = on;
    drawBtn.setAttribute("aria-pressed", on ? "true" : "false");
    drawBtn.classList.toggle("ev2-btn--primary", on);
    drawBtn.querySelector("span").textContent = on ? "Done drawing" : "Edit on preview";
    if (drawbar) drawbar.hidden = !on;
    EV2.emit("ev2:edit-mode", { on });
    if (on) EV2.emit("ev2:snap", snapState());
  }
  drawBtn?.addEventListener("click", () => setDraw(!drawing));
  EV2.onEscape(() => {
    if (!drawing) return false;
    setDraw(false);
    drawBtn.focus();
    return true;
  });

  // Snap to grid: not in the contract's event list; published for the
  // preview as ``ev2:snap`` and on EV2.snap.
  const snap = root.querySelector("[data-ev2-snap]");
  const snapDims = root.querySelector("[data-ev2-snapdims]");
  function snapState() {
    const n = (sel, d) => {
      const v = parseInt(root.querySelector(sel)?.value, 10);
      return v >= 2 && v <= 48 ? v : d;
    };
    return { on: Boolean(snap && snap.checked), cols: n("[data-ev2-snap-cols]", 12), rows: n("[data-ev2-snap-rows]", 8) };
  }
  function publishSnap() {
    EV2.snap = snapState();
    if (snapDims) snapDims.hidden = !EV2.snap.on;
    EV2.emit("ev2:snap", EV2.snap);
  }
  snap?.addEventListener("change", publishSnap);
  root.querySelectorAll("[data-ev2-snap-cols], [data-ev2-snap-rows]").forEach((i) => i.addEventListener("change", publishSnap));
  EV2.snap = snapState();

  root.querySelector("[data-ev2-refit]")?.addEventListener("click", () => {
    const w = cfg.panelW;
    const h = cfg.panelH;
    if (
      !window.confirm(
        `Stretch every cell to fit the ${w}×${h} panel? Uneven stretching can soften lines that met exactly; you can drag them back on the preview.`
      )
    )
      return;
    EV2.flush()
      .then(() => EV2.post(cfg.refitUrl, {}))
      .then(() => EV2.reload())
      .catch(() => {});
  });

  const sbar = root.querySelector("[data-ev2-sbar]");
  sbar?.addEventListener("change", () => {
    EV2.flush()
      .then(() => EV2.post(cfg.statusBarUrl, {}))
      .then(() => EV2.reload())
      .catch(() => {
        sbar.checked = !sbar.checked;
      });
  });

  // The preview saved a resize, split or remove.
  function fracCells(cells) {
    const W = Number(cfg.panelW) || 1;
    const H = Number(cfg.panelH) || 1;
    return cells.map((c) => [c.x / W, c.y / H, c.w / W, c.h / H]);
  }
  function miniLayout(cells) {
    const gw = Number(cfg.groupW) || 1;
    const gh = Number(cfg.groupH) || 1;
    const k = Math.min(36 / gw, 22 / gh);
    const TW = Math.round(gw * k);
    const TH = Math.round(gh * k);
    const wrap = document.createElement("span");
    wrap.className = "ev2-mlay";
    wrap.style.cssText = `width:${TW}px;height:${TH}px`;
    fracCells(cells).forEach(([x, y, w, h]) => {
      const s = document.createElement("span");
      s.style.cssText = `left:${x * TW}px;top:${y * TH}px;width:${w * TW}px;height:${h * TH}px`;
      wrap.append(s);
    });
    return wrap;
  }
  EV2.on("ev2:layout-changed", (d) => {
    const cells = Array.isArray(d.cells) ? d.cells : [];
    const have = [...root.querySelectorAll(".ev2-cl[data-cell-id]")].map((li) => li.dataset.cellId).sort();
    const got = cells.map((c) => c.id).sort();
    // A split or remove changed the set of cells: re-render from the server.
    if (have.join() !== got.join()) {
      EV2.reload();
      return;
    }
    const barId = root.querySelector(".ev2-cl[data-bar]")?.dataset.cellId;
    const content = cells.filter((c) => c.id !== barId);
    const sum = root.querySelector('[data-ev2-sum="layout"]');
    if (sum) {
      const n = content.length;
      sum.textContent = `Your own layout · ${n} cell${n === 1 ? "" : "s"}` + (barId ? " · status bar" : "");
    }
    const vis = root.querySelector('[data-ev2-vis="layout"]');
    if (vis) vis.replaceChildren(miniLayout(content));
    root.querySelectorAll("[data-ev2-layout]").forEach((x) => {
      if (x.dataset.ev2Layout === "custom") return;
      x.classList.remove("on");
      x.setAttribute("aria-pressed", "false");
    });
    // Empty rows name their shape; keep that current.
    content.forEach((c) => {
      const li = root.querySelector(`.ev2-cl[data-cell-id="${CSS.escape(c.id)}"]`);
      if (!li) return;
      const r = c.w / Math.max(1, c.h);
      li.dataset.shape = r > 1.6 ? "wide" : r < 0.7 ? "tall" : "square-ish";
      const txt = li.querySelector("[data-ev2-shape-txt]");
      if (txt) txt.textContent = `Cell ${li.dataset.n} · ${li.dataset.shape}`;
    });
  });

  EV2.steps = { open: openStep, current: () => open, render, renderBar, setProgress, keys: KEYS };
  render();
})();
