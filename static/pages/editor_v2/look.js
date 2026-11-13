// Dashboard editor v2: step 4, the dashboard-wide look (theme, style, font,
// matting, padding, corners, custom CSS), autosaved through the page update
// route, plus the warning that lists cells overriding it.
(function () {
  "use strict";
  const EV2 = window.EV2;
  if (!EV2) return;
  const root = EV2.root;
  const form = root.querySelector("[data-ev2-look-form]");
  const box = root.querySelector("[data-ev2-ovbox]");
  const sum = root.querySelector('[data-ev2-sum="look"]');
  if (!form) return;

  form.addEventListener("submit", (ev) => ev.preventDefault());

  function onEdit(ev) {
    const el = ev.target;
    if (!el.name && !el.dataset.ev2Pair) return;
    const delay = EV2.debounceDelay(el);
    if (ev.type === "input" && delay < 0) return;
    if (ev.type === "change" && el.type === "range") return;
    EV2.saveForm(form, ev.type === "change" ? 0 : delay).catch(() => {});
  }
  form.addEventListener("input", onEdit);
  form.addEventListener("change", onEdit);
  EV2.afterSave(form, () => refreshSummary());

  // Matting swatches set the colour input.
  const color = form.querySelector("[data-ev2-matc]");
  function paintSwatches() {
    const v = (color.value || "").toLowerCase();
    form.querySelectorAll("[data-ev2-mat]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.ev2Mat === v ? "true" : "false"));
  }
  form.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-ev2-mat]");
    if (!b) return;
    color.value = b.dataset.ev2Mat;
    paintSwatches();
    EV2.saveForm(form, 0).catch(() => {});
  });
  color?.addEventListener("input", paintSwatches);

  function selectedText(sel) {
    const o = sel && sel.selectedOptions && sel.selectedOptions[0];
    return o ? o.textContent.trim() : "";
  }
  function overriders() {
    return EV2.widgets
      ? EV2.widgets
          .rows()
          .filter((li) => li.dataset.widget && li.dataset.n)
          .map((li) => {
            const own = [...li.querySelectorAll("[data-ev2-ov]")].filter((s) => s.value).map((s) => s.dataset.ev2Ov);
            return { li, own };
          })
          .filter((x) => x.own.length)
      : [];
  }

  function refreshSummary() {
    if (!sum) return;
    const theme = selectedText(form.querySelector('[name="theme"]'));
    const style = selectedText(form.querySelector('[name="style"]')).split(" · ")[0];
    const gap = form.querySelector('[name="gap"]').value;
    const base = `${theme} · ${style} · padding ${gap}`;
    const n = overriders().length;
    sum.classList.toggle("warn", n > 0);
    sum.replaceChildren();
    if (n) {
      const i = document.createElement("i");
      i.className = "ph ph-warning";
      i.setAttribute("aria-hidden", "true");
      sum.append(i, ` ${n} widget${n === 1 ? "" : "s"} override${n === 1 ? "s" : ""} it · `);
    }
    sum.append(base);
  }

  function refreshOverrides() {
    const o = overriders();
    refreshSummary();
    if (!box) return;
    box.hidden = !o.length;
    box.replaceChildren();
    if (!o.length) return;
    const i = document.createElement("i");
    i.className = "ph ph-warning";
    i.setAttribute("aria-hidden", "true");
    const wrap = document.createElement("div");
    const b = document.createElement("b");
    b.textContent = o.length === 1 ? "1 widget uses its own look." : `${o.length} widgets use their own look.`;
    wrap.append(b, ` Changes here won't reach ${o.length === 1 ? "it" : "them"}.`);
    const ul = document.createElement("ul");
    o.forEach(({ li, own }) => {
      const row = document.createElement("li");
      const link = document.createElement("button");
      link.type = "button";
      link.className = "ev2-link ev2-link--quiet";
      link.dataset.ev2Showov = li.dataset.cellId;
      link.textContent = li.dataset.item || li.dataset.widget;
      const what = document.createElement("span");
      what.textContent = "own " + own.join(" and ");
      row.append(link, what);
      ul.append(row);
    });
    const btns = document.createElement("div");
    btns.className = "ev2-row-btns";
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "ev2-link";
    reset.dataset.ev2Resetov = "";
    reset.textContent = "Use the dashboard look everywhere";
    btns.append(reset);
    wrap.append(ul, btns);
    box.append(i, wrap);
  }

  box?.addEventListener("click", (ev) => {
    const show = ev.target.closest("[data-ev2-showov]");
    if (show) {
      const id = show.dataset.ev2Showov;
      EV2.widgets.openRow(id, { scroll: true, block: "center" });
      const li = EV2.widgets.rowFor(id);
      const det = li && li.querySelector("details[data-ev2-look]");
      if (det) {
        det.open = true;
        det.querySelector("select")?.focus({ preventScroll: true });
      }
      return;
    }
    if (ev.target.closest("[data-ev2-resetov]")) {
      const saves = overriders().map(({ li }) => {
        li.querySelectorAll("[data-ev2-ov]").forEach((s) => (s.value = ""));
        const f = li.querySelector("[data-ev2-cell-form]");
        const s = f.querySelector("[data-ev2-look-sum]");
        if (s) {
          s.textContent = "same as dashboard";
          s.classList.remove("ovw");
        }
        return EV2.saveForm(f, 0);
      });
      Promise.all(saves).catch(() => {});
      refreshOverrides();
    }
  });

  EV2.look = { refreshOverrides, refreshSummary };
  EV2.ready(refreshOverrides);
})();
