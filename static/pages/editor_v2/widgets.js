// Dashboard editor v2: step 3, the list of cells. A filled row opens in
// place with that widget's settings (autosaved through update_cell); an
// empty row, or Replace, opens the catalog (catalog.js). Drag a row onto
// another to swap the two widgets (swap_cells).
(function () {
  "use strict";
  const EV2 = window.EV2;
  if (!EV2) return;
  const root = EV2.root;
  const list = root.querySelector("[data-ev2-cells]");
  if (!list) return;

  const rows = () => [...list.querySelectorAll(".ev2-cl[data-cell-id]")];
  const rowFor = (id) => list.querySelector(`.ev2-cl[data-cell-id="${CSS.escape(id)}"]`);
  let openId = null;

  function paintRow(li, isOpen) {
    li.classList.toggle("open", isOpen);
    const body = li.querySelector(".ev2-cl-b");
    if (body) body.hidden = !isOpen;
    li.querySelector("[data-ev2-row]")?.setAttribute("aria-expanded", isOpen ? "true" : "false");
    const go = li.querySelector("[data-ev2-go]");
    if (go) go.setAttribute("aria-label", isOpen ? "Close settings" : "Open settings");
    const rep = li.querySelector("[data-ev2-replace]");
    if (rep) rep.hidden = !isOpen;
    const grip = li.querySelector(".ev2-grip:not(.none)");
    if (grip) grip.setAttribute("draggable", isOpen ? "false" : "true");
  }

  function openRow(id, opts) {
    const o = opts || {};
    const li = rowFor(id);
    if (!li) return;
    if (!li.dataset.widget && !li.dataset.bar) {
      EV2.catalog && EV2.catalog.open(id);
      return;
    }
    rows().forEach((r) => paintRow(r, r === li));
    openId = id;
    EV2.ui.cell = id;
    wireTouch(li);
    EV2.emit("ev2:cell-focus", { cellId: id });
    if (o.scroll) li.scrollIntoView({ block: o.block || "nearest", behavior: "smooth" });
  }

  function closeAll(opts) {
    const was = openId;
    rows().forEach((r) => paintRow(r, false));
    openId = null;
    EV2.ui.cell = null;
    if (was && !(opts && opts.silent)) EV2.emit("ev2:cell-focus", { cellId: null });
  }

  function ensureStep() {
    if (!EV2.steps) return;
    if (EV2.isNew) EV2.steps.setProgress(3);
    if (EV2.steps.current() !== "widgets") EV2.steps.open("widgets");
  }

  list.addEventListener("click", (ev) => {
    const li = ev.target.closest(".ev2-cl");
    if (!li) return;
    const id = li.dataset.cellId;
    if (ev.target.closest("[data-ev2-replace]")) {
      EV2.catalog && EV2.catalog.open(id, { replace: true });
      return;
    }
    if (ev.target.closest("[data-ev2-choose]")) {
      EV2.catalog && EV2.catalog.open(id);
      return;
    }
    if (ev.target.closest("[data-ev2-row], [data-ev2-go]")) {
      if (openId === id) closeAll();
      else openRow(id);
      return;
    }
    if (ev.target.closest("[data-ev2-done]")) {
      closeAll();
      li.querySelector("[data-ev2-row]")?.focus({ preventScroll: true });
      li.scrollIntoView({ block: "nearest", behavior: "smooth" });
      return;
    }
    if (ev.target.closest("[data-ev2-empty]")) {
      const form = li.querySelector("[data-ev2-cell-form]");
      form.querySelector("[data-ev2-plugin]").value = "";
      EV2.saveForm(form)
        .then(() => EV2.reload({ cell: null }))
        .catch(() => {});
      return;
    }
    const dith = ev.target.closest("[data-ev2-dither-set]");
    if (dith) {
      const box = dith.closest("[data-ev2-dither]");
      const v = dith.dataset.ev2DitherSet;
      box.querySelector("[data-ev2-dither-on]").checked = Boolean(v);
      if (v) box.querySelector("[data-ev2-dither-mode]").value = v;
      box.querySelectorAll("[data-ev2-dither-set]").forEach((b) => b.setAttribute("aria-pressed", b === dith ? "true" : "false"));
      EV2.saveForm(dith.form, 0).catch(() => {});
    }
  });

  // ------------------------------------------------------------ autosave
  function onEdit(ev) {
    const el = ev.target;
    const form = el.closest && el.closest("[data-ev2-cell-form]");
    if (!form || el.name === "plugin") return;
    // Controls with no name only drive a named one (padding pair, filters).
    if (!el.name && !el.dataset.ev2PadChoice) return;
    const delay = EV2.debounceDelay(el);
    if (ev.type === "input" && delay < 0) return; // text: save on change
    if (ev.type === "change" && delay > 0 && el.type === "range") return; // input already queued it
    EV2.saveForm(form, ev.type === "change" ? 0 : delay).catch(() => {});
  }
  list.addEventListener("input", onEdit);
  list.addEventListener("change", onEdit);

  // Zoom readout, daily-refresh reveal, padding choice.
  list.addEventListener("input", (ev) => {
    const z = ev.target.closest("[data-ev2-zoom]");
    if (z) {
      const out = z.parentElement.querySelector("[data-ev2-zoom-out]");
      if (out) out.textContent = Number(z.value).toFixed(2) + "×";
    }
    if (ev.target.matches('[data-ev2-pair^="pad-"]')) {
      const box = ev.target.closest("[data-ev2-pad]");
      box.querySelector("[data-ev2-pad-on]").checked = true;
      if (ev.target.type === "range") EV2.saveForm(ev.target.form, 450).catch(() => {});
    }
  });
  list.addEventListener("change", (ev) => {
    const t = ev.target;
    if (t.dataset.ev2Reveal) {
      const target = document.getElementById(t.dataset.ev2Reveal);
      if (target) target.hidden = !t.checked;
    }
    if (t.matches("[data-ev2-pad-choice]")) {
      const box = t.closest("[data-ev2-pad]");
      const on = box.querySelector("[data-ev2-pad-on]");
      const custom = box.querySelector("[data-ev2-pad-custom]");
      const v = t.value;
      on.checked = v !== "";
      custom.hidden = v !== "custom";
      if (v !== "" && v !== "custom") {
        box.querySelectorAll("[data-ev2-pair]").forEach((i) => (i.value = v));
      }
      if (v === "custom") custom.querySelector("input[type=range]")?.focus();
    }
    if (t.matches("[data-ev2-ov]")) refreshLookSummary(t.form);
  });

  function refreshLookSummary(form) {
    const own = [...form.querySelectorAll("[data-ev2-ov]")].filter((s) => s.value).map((s) => s.dataset.ev2Ov);
    const sum = form.querySelector("[data-ev2-look-sum]");
    if (sum) {
      sum.textContent = own.length ? "own " + own.join(" and ") : "same as dashboard";
      sum.classList.toggle("ovw", own.length > 0);
    }
    EV2.look && EV2.look.refreshOverrides();
  }

  // ------------------------------------------------------- touch actions
  function wireTouch(li) {
    if (typeof window.TouchInteraction === "undefined") return;
    li.querySelectorAll("[data-touch-mount]").forEach((mount) => {
      if (mount.dataset.touchBound) return;
      mount.dataset.touchBound = "1";
      const input = mount.parentElement.querySelector("[data-touch-input]");
      let value = {};
      try {
        value = JSON.parse(mount.dataset.touchValue || "{}") || {};
      } catch (e) {
        value = {};
      }
      mount.appendChild(
        window.TouchInteraction.render({
          value,
          pagesUrl: mount.dataset.pagesUrl,
          haActionsUrl: mount.dataset.haUrl,
          defaultAxis: mount.dataset.defaultAxis === "x" ? "x" : "y",
          onChange: (v) => {
            if (!input) return;
            const out = {};
            if (v.on_tap) out.on_tap = v.on_tap;
            if (v.on_swipe) out.on_swipe = v.on_swipe;
            if (v.on_slide) out.on_slide = v.on_slide;
            input.value = JSON.stringify(out);
            EV2.saveForm(input.form, 300).catch(() => {});
          },
        })
      );
    });
  }

  // ------------------------------------------------------ drag to swap
  let dragFrom = null;
  list.addEventListener("dragstart", (ev) => {
    const grip = ev.target.closest && ev.target.closest(".ev2-grip");
    const li = grip && grip.closest(".ev2-cl[data-swap-url]");
    if (!li || li.classList.contains("open")) {
      ev.preventDefault();
      return;
    }
    dragFrom = li;
    li.classList.add("dragging");
    if (ev.dataTransfer) {
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", li.dataset.cellId);
      ev.dataTransfer.setDragImage(li, 24, 20);
    }
  });
  const clearDrop = () => list.querySelectorAll(".ev2-cl.drop").forEach((x) => x.classList.remove("drop"));
  list.addEventListener("dragover", (ev) => {
    const li = ev.target.closest && ev.target.closest(".ev2-cl[data-swap-url]");
    if (!dragFrom || !li || li === dragFrom) return;
    ev.preventDefault();
    if (ev.dataTransfer) ev.dataTransfer.dropEffect = "move";
    clearDrop();
    li.classList.add("drop");
  });
  list.addEventListener("drop", (ev) => {
    const li = ev.target.closest && ev.target.closest(".ev2-cl[data-swap-url]");
    const from = dragFrom;
    dragFrom = null;
    clearDrop();
    if (!from || !li || li === from) return;
    ev.preventDefault();
    EV2.flush()
      .then(() => EV2.post(from.dataset.swapUrl, { target: li.dataset.cellId }, { json: true }))
      .then(() => EV2.reload({ cell: null }))
      .catch(() => {});
  });
  list.addEventListener("dragend", () => {
    dragFrom = null;
    clearDrop();
    list.querySelectorAll(".ev2-cl.dragging").forEach((x) => x.classList.remove("dragging"));
  });

  // ------------------------------------------------- preview -> steps
  EV2.on("ev2:cell-select", (d) => {
    if (!d.cellId || !rowFor(d.cellId)) return;
    ensureStep();
    const li = rowFor(d.cellId);
    if (!li.dataset.widget && !li.dataset.bar) {
      EV2.catalog && EV2.catalog.open(d.cellId);
      return;
    }
    if (openId !== d.cellId) openRow(d.cellId);
    li.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });

  function openFirstEmpty() {
    const li = rows().find((r) => r.dataset.n && !r.dataset.widget);
    if (li) EV2.catalog && EV2.catalog.open(li.dataset.cellId);
  }

  EV2.widgets = {
    openRow: (id, opts) => {
      ensureStep();
      openRow(id, opts);
    },
    closeAll,
    openFirstEmpty,
    openId: () => openId,
    rowFor,
    rows,
  };

  // Back from a reload: reopen the row that was open.
  EV2.ready(() => {
    const id = EV2.restored.cell;
    if (id && rowFor(id) && EV2.steps && EV2.steps.current() === "widgets") {
      const li = rowFor(id);
      if (li.dataset.widget || li.dataset.bar) openRow(id);
    }
  });
})();
