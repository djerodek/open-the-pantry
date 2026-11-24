// Quick reference charts (0233): baking substitutions, cooking
// substitutions and meat cooking times, in a dialog opened from the top
// bar and from each recipe. The content is reference-data.json, both
// languages, fetched the first time a chart opens; the service worker
// keeps it with the rest of the app, so the charts work offline.
//
// On by default; Settings -> Display turns it off on this device.
//
// The top bar (owner, 2026-11-23, "Option B"): from 48rem, a labelled
// "Quick reference" button beside What to eat?. On a phone, a button each
// left the search box 88 px wide at 320 px and crushed the search button,
// so there What to eat? moves into this menu, under a ⋮ button; with What
// to eat? off, the button shows the book and holds only the charts.
//
// Nothing here runs when the module loads: app.js imports this file, and
// its own constants (tx among them) aren't ready until initQuickReference().
import { tx, el, $, $$, openModal, closeModal, storageGet, storageSet, reportClientError } from "./app.js";

const PREF_KEY = "recipe-app-quick-reference";
// °F unless °C is chosen in the dialog (owner, 2026-11-23); per device.
const TEMP_KEY = "recipe-app-reference-temperature";
const CHARTS = ["baking", "cooking", "meat"];
const LB_KG = 0.45359237;
const OZ_G = 28.349523125;

// Fixed markup: an open book, and three dots.
const BOOK_ICON = '<svg class="icon icon-book" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
  '<path d="M12 6.6C10.2 5.2 7.8 4.6 4 4.6v13.2c3.8 0 6.2.6 8 2 1.8-1.4 4.2-2 8-2V4.6c-3.8 0-6.2.6-8 2zM12 6.6v13.2" ' +
  'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"/></svg>';
const MORE_ICON = '<svg class="icon icon-more" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
  '<circle cx="12" cy="5.5" r="1.8" fill="currentColor"/><circle cx="12" cy="12" r="1.8" fill="currentColor"/>' +
  '<circle cx="12" cy="18.5" r="1.8" fill="currentColor"/></svg>';

export function quickReferenceOn() {
  return storageGet(PREF_KEY) !== "false";
}

function tempUnit() {
  return storageGet(TEMP_KEY) === "C" ? "C" : "F";
}

function chartTitle(id) {
  if (id === "baking") return tx("Baking substitutions");
  if (id === "cooking") return tx("Cooking substitutions");
  return tx("Meat cooking times");
}

// ---------------------------------------------------------------------
// Text and numbers
// ---------------------------------------------------------------------
const lang = () => (window.OTP_I18N && window.OTP_I18N.lang === "fr" ? "fr" : "en");

// Quebec typography, as i18n.js does for its catalog: a no-break space
// before ":" and "%", and inside « ». The last replace is a fallback for a
// "25 °F" in the data: degrees() already writes the no-break space.
function frType(s) {
  return s.replace(/ :/g, "\u00a0:").replace(/ %/g, "\u00a0%").replace(/« /g, "«\u00a0").replace(/ »/g, "\u00a0»")
    .replace(/(\d) °/g, "$1\u00a0°");
}

// A number stays on the line with its unit: "2 mL", "1 c. à thé".
function keepUnits(s) {
  return s.replace(/([\d½¼¾⅓⅔⅛]) (?=(?:mL|g|kg|lb|oz|tsp|tbsp|cups?|tasses?|c\.|min|h)(?![\w.]))/g, "$1\u00a0")
    .replace(/c\. à (thé|soupe)/g, "c.\u00a0à\u00a0$1");
}

// The text holds both ("74°C (165°F)", "by 25°F (14°C)"); only the
// chosen unit is shown.
function oneUnit(s) {
  const want = tempUnit();
  return s.replace(/(\d+) ?°([CF]) \((\d+) ?°([CF])\)/g, (m, a, unitA, b) => degrees(unitA === want ? a : b, want));
}

// o is one of the data's {en, fr} pairs; a plain string is shown as it is.
function t(o) {
  if (o == null) return "";
  if (typeof o === "string") return o;
  const s = oneUnit(keepUnits(o[lang()] || o.en || ""));
  return lang() === "fr" ? frType(s) : s;
}

function dec(x, digits) {
  const s = x.toFixed(digits);
  return lang() === "fr" ? s.replace(".", ",") : s;
}

// 2.5 -> "2½", 0.5 -> "½" (the U.S. chart's own way of writing pounds).
function lbNum(x) {
  const whole = Math.floor(x);
  const glyph = { 0.25: "¼", 0.5: "½", 0.75: "¾" }[Math.round((x - whole) * 100) / 100];
  if (glyph) return whole ? `${whole}${glyph}` : glyph;
  return Number.isInteger(x) ? String(x) : dec(x, 1);
}

function range(values, fmt = String) {
  const parts = values.map(fmt);
  if (parts.length === 1) return parts[0];
  return lang() === "fr" ? `${parts[0]} à ${parts[1]}` : `${parts[0]}–${parts[1]}`;
}

function degrees(value, unit) {
  return lang() === "fr" ? `${value}\u00a0°${unit}` : `${value}°${unit}`;
}

// "4–6 lb (1.8–2.7 kg)": each half kept whole, so a narrow column
// breaks between them rather than inside either.
function pair(main, alt) {
  return [el("span", { class: "nowrap", text: main }), document.createTextNode(" "),
    el("span", { class: "nowrap", text: `(${alt})` })];
}

function weightNodes(row) {
  if (row.lb) {
    // Under 1 kg in grams, like the ounce rows: ½ lb is 230 g, not "0.2 kg" (Claude review of 0233).
    const kg = row.lb.map((v) => v * LB_KG);
    const metric = kg.every((v) => v < 1)
      ? `${range(kg.map((v) => Math.round(v * 100) * 10))} g`
      : `${range(kg, (v) => dec(v, 1))} kg`;
    return pair(`${range(row.lb, lbNum)} lb`, metric);
  }
  if (row.oz) return pair(`${range(row.oz)} oz`, `${range(row.oz.map((v) => Math.round((v * OZ_G) / 5) * 5))} g`);
  return [document.createTextNode("—")];
}

function ovenText(oven) {
  return degrees(range(tempUnit() === "C" ? oven.c : oven.f), tempUnit());
}

// Minutes as the U.S. chart writes them: "45–60 min", "1¼–1½ h";
// "1 h 15 à 1 h 30" in French.
function durationText(minutes) {
  if (minutes.some((m) => m < 60)) return `${range(minutes)} min`;
  if (lang() === "fr") {
    return range(minutes, (m) => `${Math.floor(m / 60)}\u00a0h${m % 60 ? `\u00a0${String(m % 60).padStart(2, "0")}` : ""}`);
  }
  return `${range(minutes, (m) => lbNum(m / 60))} h`;
}

function timeNodes(time) {
  const nodes = time.per_lb
    ? pair(`${range(time.per_lb)} min/lb`, `${range(time.per_lb.map((v) => Math.round(v / LB_KG)))} min/kg`)
    : [el("span", { class: "nowrap", text: durationText(time.total) })];
  if (time.or) nodes.push(document.createTextNode(`, ${t(time.or)}`));
  return nodes;
}

function tempNodes(temps, key, marks) {
  if (key === "cooked") return [document.createTextNode(marks.cooked.symbol)];
  return (temps[key] || []).map((x) => el("span", { class: "ref-temp" }, [
    el("strong", { class: "nowrap", text: degrees(tempUnit() === "C" ? x.c : x.f, tempUnit()) }),
    x.label ? document.createTextNode(` ${t(x.label)}`) : null,
  ]));
}

// ---------------------------------------------------------------------
// Tables: a table from 40rem; below that each row is a block with the
// column names as labels (styles.css). The roles keep it a table for
// screen readers once the display is changed.
// ---------------------------------------------------------------------
function table(headers, rows, extraClass = "") {
  return el("div", { class: "ref-table-wrap" }, [
    el("table", { class: `ref-table ${extraClass}`.trim(), role: "table" }, [
      el("thead", { role: "rowgroup" }, [el("tr", { role: "row" },
        headers.map((h) => el("th", { scope: "col", role: "columnheader", text: h })))]),
      el("tbody", { role: "rowgroup" }, rows.map((cells) => el("tr", { role: "row" },
        // One element per cell, so a stacked row lays out as label | value.
        cells.map((c, i) => el("td", { role: "cell", "data-label": headers[i] }, [
          el("div", { class: "ref-cell" }, typeof c === "string" ? [document.createTextNode(c)] : [].concat(c)),
        ]))))),
    ]),
  ]);
}

function substitutionChart(chart) {
  return [
    el("p", { class: "ref-intro", text: t(chart.intro) }),
    el("ul", { class: "ref-subs" }, chart.entries.map((e) => el("li", {}, [
      el("div", { class: "ref-item", text: t(e.item) }),
      el("ul", { class: "ref-use" }, e.use.map((u) => el("li", { text: t(u) }))),
      e.note ? el("p", { class: "ref-note", text: t(e.note) }) : null,
    ]))),
  ];
}

function meatChart(m) {
  const out = m.intro.map((p) => el("p", { class: "ref-intro", text: t(p) }));
  const H = tx("Health Canada");
  for (const section of m.sections) {
    out.push(el("h3", { class: "ref-section", text: t(section.title) }));
    if (section.note) out.push(el("p", { class: "ref-note", text: t(section.note) }));
    const used = new Set();
    const rows = section.rows.map((r) => {
      if (r.mark) used.add(r.mark);
      // The fully cooked ham's asterisk stands in its temperature column
      // only; other marks go after the cut's name.
      const symbol = r.mark && r.temp !== "cooked" ? ` ${m.marks[r.mark].symbol}` : "";
      const cut = [el("span", { class: "ref-cut", text: t(r.cut) + symbol })];
      if (r.ca) cut.push(el("span", { class: "ref-ca", text: tx("In Canada: {1}", { 1: t(r.ca) }) }));
      return [cut, weightNodes(r), el("span", { class: "nowrap", text: ovenText(r.oven) }), timeNodes(r.time),
        tempNodes(m.temps, r.temp, m.marks)];
    });
    out.push(table([tx("Cut"), tx("Weight"), tx("Oven"), tx("Time"), H], rows));
    for (const key of used) out.push(el("p", { class: "ref-footnote", text: `${m.marks[key].symbol} ${t(m.marks[key])}` }));
  }

  const turkey = m.turkey;
  out.push(el("h3", { class: "ref-section", text: t(turkey.title) }));
  out.push(el("p", { class: "ref-note", text: tx("Oven at {1}.", { 1: ovenText(turkey.oven) }) }));
  out.push(table([tx("Size"), tx("Not stuffed"), tx("Stuffed"), H], turkey.rows.map((r) => [
    [...weightNodes(r), r.breast ? document.createTextNode(`, ${tx("breast")}`) : null],
    durationText(r.unstuffed),
    r.stuffed ? durationText(r.stuffed) : tx("not usually stuffed"),
    tempNodes(m.temps, r.breast ? "74" : "82", m.marks),
  ])));

  const thaw = m.thawing;
  out.push(el("h3", { class: "ref-section", text: t(thaw.title) }));
  out.push(el("p", { class: "ref-note", text: t(thaw.note) }));
  out.push(table([tx("Size"), tx("Refrigerator"), tx("Cold water")], thaw.rows.map((r) => [
    weightNodes(r),
    `${range(r.fridge_days)} ${tx("days")}`,
    `${range(r.water_hours)} h`,
  ])));

  out.push(el("h3", { class: "ref-section", text: t(m.safe.title) }));
  for (const g of m.safe.groups) {
    out.push(el("h4", { class: "ref-group", text: t(g.title) }));
    out.push(table([tx("Food"), tx("Cook to")], g.rows.map((r) => [t(r.food), tempNodes(m.temps, r.temp, m.marks)]), "ref-table-2"));
  }
  return out;
}

function sourcesSection(data, chart, notes) {
  return [
    el("h3", { class: "ref-section", text: tx("Sources and notes") }),
    ...notes.map((n) => el("p", { class: "ref-note", text: t(n) })),
    el("ul", { class: "ref-sources" }, chart.sources.map((id) => {
      const s = data.sources[id];
      const url = typeof s.url === "string" ? s.url : (s.url[lang()] || s.url.en);
      return el("li", {}, [
        el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: t(s.title) }),
        document.createTextNode(` — ${t(s.publisher)}, ${t(s.date)}`),
      ]);
    })),
    el("p", { class: "field-hint", text: tx("Checked on {1}.", { 1: data.checked }) }),
  ];
}

// ---------------------------------------------------------------------
// The dialog
// ---------------------------------------------------------------------
let overlay = null;
let shown = null;
let dataPromise = null;

function loadData() {
  if (!dataPromise) {
    dataPromise = fetch("js/reference-data.json").then((r) => {
      if (!r.ok) throw new Error(`reference-data.json: ${r.status}`);
      return r.json();
    }).catch((err) => {
      dataPromise = null;   // tried again next time
      throw err;
    });
  }
  return dataPromise;
}

function buildDialog() {
  const heading = el("h2", { id: "reference-heading" });
  const switcher = el("div", { class: "mode-switch ref-switch", role: "group", "aria-label": tx("Chart") },
    CHARTS.map((id) => el("button", { type: "button", "data-chart": id, text: chartTitle(id), onclick: () => showChart(id) })));
  const units = el("div", { class: "ref-units" }, [
    el("span", { id: "reference-units-label", text: tx("Temperatures") }),
    el("div", { class: "mode-switch", role: "group", "aria-labelledby": "reference-units-label" },
      ["F", "C"].map((u) => el("button", { type: "button", "data-unit": u, text: `°${u}`, onclick: () => setTempUnit(u) }))),
  ]);
  const body = el("div", { id: "reference-body", class: "ref-body" });
  overlay = el("div", { class: "modal-overlay", id: "reference-overlay", hidden: "" }, [
    el("div", { class: "modal reference-modal", id: "reference-modal", role: "dialog", "aria-modal": "true",
      "aria-labelledby": "reference-heading" }, [
      el("button", { class: "modal-close", id: "reference-close", type: "button", "aria-label": tx("Close dialog"),
        text: "×", onclick: () => closeModal(overlay) }),
      heading, switcher, units, body,
    ]),
  ]);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) closeModal(overlay); });
  document.body.appendChild(overlay);
}

function showUnit() {
  $$("#reference-overlay .ref-units button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.unit === tempUnit())));
}

// Drawn again in the other unit, where the reader was.
function setTempUnit(unit) {
  storageSet(TEMP_KEY, unit);
  showUnit();
  if (shown) showChart(shown, true);
}

async function showChart(id, keepPlace = false) {
  shown = id;
  $("#reference-heading").textContent = chartTitle(id);
  $$("#reference-overlay .ref-switch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.chart === id)));
  showUnit();
  const body = $("#reference-body");
  const modal = $("#reference-modal");
  const place = modal.scrollTop;
  if (!keepPlace) {
    body.replaceChildren(el("p", { class: "field-hint", text: tx("Loading...") }));
    modal.scrollTop = 0;
  }
  let data;
  try {
    data = await loadData();
  } catch (err) {
    reportClientError(`Quick reference: ${err && err.message ? err.message : err}`, "reference.js", null, null, err);
    if (shown === id) body.replaceChildren(el("p", { class: "field-hint", text: tx("Couldn't load the charts. Try again once you're online.") }));
    return;
  }
  if (shown !== id) return;   // switched again while it loaded
  const notes = data.notes;
  const content = id === "meat"
    ? [...meatChart(data.meat), ...sourcesSection(data, data.meat, [...notes.meat, ...notes.all])]
    : [...substitutionChart(data[id]), ...sourcesSection(data, data[id], [...notes.substitutions, ...notes.all])];
  body.replaceChildren(...content);
  if (keepPlace) modal.scrollTop = place;
}

// `from` gets the focus back when the dialog closes: the menu item that
// was tapped is hidden by then.
function openChart(id, from) {
  if (from) from.focus();
  openModal(overlay);
  showChart(id);
}

// ---------------------------------------------------------------------
// Menus: the same three charts from the top bar and from a recipe.
// ---------------------------------------------------------------------
function menuControls(toggle, menu) {
  const close = () => { menu.hidden = true; toggle.setAttribute("aria-expanded", "false"); };
  toggle.addEventListener("click", () => {
    const willOpen = menu.hidden;
    menu.hidden = !willOpen;
    toggle.setAttribute("aria-expanded", String(willOpen));
  });
  // Escape closes the menu, and only the menu: not the recipe under it.
  toggle.parentElement.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || menu.hidden) return;
    e.stopPropagation();
    close();
    toggle.focus();
  });
  for (const id of CHARTS) {
    menu.appendChild(el("button", { type: "button", "data-chart": id, text: chartTitle(id),
      onclick: () => { close(); openChart(id, toggle); } }));
  }
  return close;
}

let closeTopbarMenu = () => {};

function buildTopbar() {
  const toggle = el("button", {
    id: "topbar-ref-toggle", class: "sidebar-toggle topbar-link", type: "button", "aria-haspopup": "true",
    "aria-expanded": "false", "aria-controls": "topbar-ref-menu", trustedStaticHtml: BOOK_ICON + MORE_ICON,
  }, [el("span", { class: "topbar-link-label", text: tx("Quick reference") })]);
  const menu = el("div", { class: "share-menu topbar-ref-menu", id: "topbar-ref-menu", hidden: "" }, [
    el("a", { class: "menu-picker", id: "topbar-menu-picker", hidden: "" }, [
      el("span", { text: tx("What to eat?") }),
      el("span", { class: "menu-sub", text: tx("Picks a random recipe") }),
    ]),
    el("div", { class: "menu-head", id: "topbar-menu-head", hidden: "", text: tx("Quick reference") }),
  ]);
  const wrap = el("div", { class: "share-menu-wrap topbar-ref", id: "topbar-ref" }, [toggle, menu]);
  closeTopbarMenu = menuControls(toggle, menu);
  // Before What to eat? if it's already there, so the order is the same
  // whichever arrives first; settings.js adds that link before Settings.
  ($("#picker-link") || $("#settings-toggle")).before(wrap);
}

const wide = window.matchMedia("(min-width: 48rem)");

function layoutTopbar() {
  const wrap = $("#topbar-ref");
  if (!wrap) return;
  const on = quickReferenceOn();
  const picker = $("#picker-link");
  const merged = on && !!picker && !wide.matches;
  wrap.hidden = !on;
  if (!on) closeTopbarMenu();
  if (picker) picker.hidden = merged;
  const item = $("#topbar-menu-picker");
  item.hidden = !merged;
  $("#topbar-menu-head").hidden = !merged;
  if (picker) item.href = picker.href; else item.removeAttribute("href");
  wrap.classList.toggle("is-merged", merged);
  $("#topbar-ref-toggle").setAttribute("aria-label", merged ? tx("What to eat? and quick reference") : tx("Quick reference"));
}

/** The recipe view's button (beside Keep screen on). */
export function recipeReferenceMenu() {
  const toggle = el("button", {
    class: "btn-secondary ref-toggle", type: "button", "aria-haspopup": "true", "aria-expanded": "false",
    trustedStaticHtml: BOOK_ICON,
  }, [el("span", { text: tx("Quick reference") })]);
  const menu = el("div", { class: "share-menu ref-menu", hidden: "" });
  const wrap = el("div", { class: "share-menu-wrap ref-wrap", hidden: quickReferenceOn() ? null : "" }, [toggle, menu]);
  menuControls(toggle, menu);
  return wrap;
}

export function initQuickReference() {
  buildDialog();
  buildTopbar();
  layoutTopbar();
  wide.addEventListener("change", layoutTopbar);
  // settings.js adds, moves or removes the What to eat? link.
  document.addEventListener("otp:picker-link", layoutTopbar);

  const setting = $("#quick-reference-setting");
  setting.checked = quickReferenceOn();
  setting.addEventListener("change", () => {
    storageSet(PREF_KEY, String(setting.checked));
    layoutTopbar();
    $$(".ref-wrap").forEach((w) => { w.hidden = !setting.checked; });
  });
}
