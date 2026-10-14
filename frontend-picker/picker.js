// What to eat? / On mange quoi?: the picker page (backend: app/picker.py,
// design: specs/dinner-picker-spec.md). No build step, like app.js.
//
// Choose meal types, cooking styles, main ingredients and paces (or none),
// press the big button, get one recipe that matches, as a link into the
// main app. Something else picks another without repeating any recipe
// shown since the round started; Clear resets everything.
(() => {
  "use strict";

  const { lang: LANG, setLang, tx, txn, tagLabel, groupLabel, valueLabel, translateStatic } = window.OTP_I18N;
  translateStatic(document.body);
  document.title = tx("What to eat?");

  const $ = (sel) => document.querySelector(sel);
  const STORAGE_KEY = "otp-picker-choices";
  const MAX_SHOWN = 500;            // the server ignores more exclusions than this
  const SHUFFLE_STEPS = 8, SHUFFLE_STEP_MS = 50;   // about 400 ms in all

  // The four chip groups. Tag names stay as stored (English) and are shown
  // through tagLabel(); pace values through the main app's pace strings.
  const GROUPS = [
    { key: "meal_type", label: () => groupLabel("meal_type", "Meal type") },
    { key: "cooking_style", label: () => groupLabel("cooking_style", "Cooking style") },
    { key: "main_ingredient", label: () => groupLabel("main_ingredient", "Main ingredient") },
    { key: "pace", label: () => tx("Pace") },
  ];

  const state = {
    options: null,
    chosen: Object.fromEntries(GROUPS.map((g) => [g.key, new Set()])),
    shown: [],          // recipe ids shown since the round started
    current: null,      // the recipe on screen
    busy: false,
    // Until /api/picker/options says where the main app is.
    mainUrl: `http://${location.hostname}:8090`,
  };

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined) continue;
      if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of [].concat(children)) if (c) node.appendChild(c);
    return node;
  }

  // localStorage throws when storage is blocked (some private modes); the
  // choices then last for this page only.
  function loadChoices() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      return saved && typeof saved === "object" ? saved : {};
    } catch { return {}; }
  }
  function saveChoices() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(
        Object.fromEntries(GROUPS.map((g) => [g.key, [...state.chosen[g.key]]]))));
    } catch { /* lasts this page only */ }
  }

  // Only http(s) addresses become links; anything else falls back.
  function webAddress(value, fallback) {
    try {
      const u = new URL(value);
      if (u.protocol === "http:" || u.protocol === "https:") return String(value).replace(/\/+$/, "");
    } catch { /* not an address */ }
    return fallback;
  }

  async function api(path) {
    const res = await fetch(path, { headers: { "X-App-Lang": LANG }, cache: "no-store" });
    if (!res.ok) {
      const err = new Error(String(res.status));
      err.status = res.status;
      throw err;
    }
    return res.json();
  }

  function errorText(err) {
    if (err && err.status === 429) return tx("Too many requests just now. Wait a moment and try again.");
    if (err && err.status) return tx("The server returned an error ({1}).", { 1: err.status });
    return tx("Couldn't reach Open the Pantry. Check your connection and try again.");
  }

  // ---------------------------------------------------------------------
  // Language: different ports are different origins, so the main app's
  // choice isn't visible here. This page keeps its own.
  // ---------------------------------------------------------------------
  document.querySelectorAll("[data-lang-choice]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.langChoice === LANG));
    b.addEventListener("click", () => {
      if (b.dataset.langChoice === LANG) return;
      setLang(b.dataset.langChoice);
      location.reload();
    });
  });

  // ---------------------------------------------------------------------
  // Chips
  // ---------------------------------------------------------------------
  function groupItems(key) {
    const o = state.options;
    if (!o) return [];
    if (key === "pace") return (o.pace || []).map((p) => ({ value: p.value, label: valueLabel(p.value) }));
    return (o[key] || []).map((t) => ({ value: t.id, label: tagLabel({ name: t.name, category: key }) }));
  }

  function renderChoices() {
    const wrap = $("#choices");
    wrap.replaceChildren();
    for (const g of GROUPS) {
      const items = groupItems(g.key);
      if (!items.length) continue;
      const labelId = `group-${g.key}-label`;
      const any = el("span", { class: "group-any", text: tx("Any") });
      any.hidden = state.chosen[g.key].size > 0;
      const chips = items.map((item) => el("button", {
        type: "button", class: "chip",
        "aria-pressed": String(state.chosen[g.key].has(item.value)),
        text: item.label,
        onclick: (e) => toggleChip(g.key, item.value, e.currentTarget, any),
      }));
      wrap.appendChild(el("div", { class: "chip-group", role: "group", "aria-labelledby": labelId }, [
        el("div", { class: "chip-group-head" }, [
          el("span", { class: "chip-group-label", id: labelId, text: g.label() }), any,
        ]),
        el("div", { class: "chip-row" }, chips),
      ]));
    }
  }

  function toggleChip(key, value, button, anyHint) {
    const set = state.chosen[key];
    if (set.has(value)) set.delete(value); else set.add(value);
    button.setAttribute("aria-pressed", String(set.has(value)));
    anyHint.hidden = set.size > 0;
    saveChoices();
    // New choices, new round. The recipe on screen stays until the next pick.
    state.shown = [];
  }

  // ---------------------------------------------------------------------
  // Picking
  // ---------------------------------------------------------------------
  const pickButton = $("#pick-button");
  const againButton = $("#again-button");
  const clearButton = $("#clear-button");

  function setBusy(on) {
    state.busy = on;
    // aria-disabled, not disabled: a disabled button drops the focus, and
    // focus stays on the button pressed (spec section 6).
    for (const b of [pickButton, againButton]) {
      if (on) { b.setAttribute("aria-busy", "true"); b.setAttribute("aria-disabled", "true"); }
      else { b.removeAttribute("aria-busy"); b.removeAttribute("aria-disabled"); }
    }
  }

  function query(exclude) {
    const p = new URLSearchParams();
    for (const g of GROUPS) for (const v of state.chosen[g.key]) p.append(g.key, v);
    for (const id of exclude) p.append("exclude", id);
    return `/api/picker/random?${p}`;
  }

  function matchText(n) {
    return txn(n, "{n} match", "{n} matches");
  }

  function showArea({ note = "", message = "", card = null, count = null, again = false }) {
    $("#result").hidden = false;
    $("#result-note").textContent = note;
    $("#result-note").hidden = !note;
    $("#result-message").textContent = message;
    $("#result-message").hidden = !message;
    $("#result-card-slot").replaceChildren(...(card ? [card] : []));
    $("#result-count").textContent = count === null ? "" : matchText(count);
    $("#result-count").hidden = count === null;
    againButton.hidden = !again;
  }

  function tagLine(recipe) {
    const parts = (recipe.tags || []).map((t) => tagLabel(t));
    if (recipe.cook_time_rating) parts.push(valueLabel(recipe.cook_time_rating));
    return parts.join(" · ");
  }

  function resultCard(recipe) {
    const card = el("a", { class: "result-card", href: `${state.mainUrl}/#recipe-${recipe.id}` });
    if (recipe.image_path) {
      card.appendChild(el("img", { class: "result-photo", src: `/uploads/${encodeURIComponent(recipe.image_path)}`, alt: "" }));
    }
    const line = tagLine(recipe);
    card.appendChild(el("span", { class: "result-body" }, [
      el("span", { class: "result-title-row" }, [
        el("span", { class: "result-title", text: recipe.title }),
        el("span", { class: "result-arrow", "aria-hidden": "true", text: "→" }),
      ]),
      line ? el("span", { class: "result-tags", text: line }) : null,
    ]));
    return card;
  }

  // Slot-machine titles for about 400 ms before the pick lands. The pick
  // is already decided; this only delays showing it. Skipped with reduced
  // motion, or when there's nothing else to cycle through.
  function shuffle(titles) {
    const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !titles.length) return Promise.resolve();
    const title = el("span", { class: "result-title" });
    const card = el("div", { class: "result-card result-card--shuffling", "aria-hidden": "true" }, [
      el("span", { class: "result-body" }, [el("span", { class: "result-title-row" }, [title])]),
    ]);
    $("#result").hidden = false;
    $("#result-card-slot").replaceChildren(card);
    return new Promise((resolve) => {
      let i = 0;
      const step = () => {
        title.textContent = titles[i % titles.length];
        i += 1;
        if (i < SHUFFLE_STEPS) setTimeout(step, SHUFFLE_STEP_MS);
        else setTimeout(resolve, SHUFFLE_STEP_MS);
      };
      step();
    });
  }

  function announce(text) {
    const region = $("#pick-status");
    region.textContent = "";
    // A fresh node each time, so the same text twice is read twice.
    setTimeout(() => { region.textContent = text; }, 50);
  }

  async function pick() {
    if (state.busy) return;
    setBusy(true);
    try {
      let data = await api(query(state.shown));
      let note = "";
      if (data.exhausted) {
        // Every match has been shown: say so and start a new round. The
        // recipe on screen counts as shown in it, so the new round doesn't
        // open with the same one (unless it's the only match).
        note = data.match_count === 1
          ? tx("That's the only one that matches.")
          : tx("That's all {1} — starting over.", { 1: data.match_count });
        state.shown = state.current && data.match_count > 1 ? [state.current.id] : [];
        data = await api(query(state.shown));
      }
      if (!data.recipe) {
        state.current = null;
        state.shown = [];
        showArea({ message: tx("Nothing matches these choices.") });
        announce(tx("Nothing matches these choices."));
        return;
      }
      const recipe = data.recipe;
      state.current = recipe;
      state.shown = state.shown.filter((id) => id !== recipe.id).concat(recipe.id).slice(-MAX_SHOWN);
      if (recipe.image_path) new Image().src = `/uploads/${encodeURIComponent(recipe.image_path)}`;   // load during the shuffle
      $("#result-note").hidden = true;
      $("#result-message").hidden = true;
      await shuffle(data.shuffle_titles || []);
      showArea({ note, card: resultCard(recipe), count: data.match_count, again: true });
      announce([note, `${recipe.title}, ${matchText(data.match_count)}`].filter(Boolean).join(" "));
    } catch (err) {
      // The recipe on screen, if any, stays; the message goes above the buttons.
      $("#result").hidden = false;
      $("#result-message").textContent = errorText(err);
      $("#result-message").hidden = false;
      announce(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  function clearAll() {
    for (const g of GROUPS) state.chosen[g.key].clear();
    saveChoices();
    state.shown = [];
    state.current = null;
    $("#result").hidden = true;
    $("#result-card-slot").replaceChildren();
    renderChoices();
    const first = document.querySelector("#choices .chip");
    (first || pickButton).focus();
  }

  pickButton.addEventListener("click", pick);
  againButton.addEventListener("click", pick);
  clearButton.addEventListener("click", clearAll);

  // ---------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------
  function setMainLinks() {
    $("#pantry-link").href = `${state.mainUrl}/`;
    $("#empty-library-link").href = `${state.mainUrl}/`;
  }

  async function start() {
    $("#load-error").hidden = true;
    $("#choices-loading").hidden = false;
    let options;
    try {
      options = await api("/api/picker/options");
    } catch (err) {
      $("#choices-loading").hidden = true;
      $("#load-error-text").textContent = errorText(err);
      $("#load-error").hidden = false;
      return;
    }
    state.options = options;
    state.mainUrl = webAddress(options.main_url, state.mainUrl);
    setMainLinks();
    if (!options.total) {
      $("#choices").hidden = true;
      $("#action").hidden = true;
      $("#empty-library").hidden = false;
      return;
    }
    // Remembered choices, minus any that no longer exist.
    const saved = loadChoices();
    for (const g of GROUPS) {
      const valid = new Set(groupItems(g.key).map((i) => i.value));
      state.chosen[g.key] = new Set((Array.isArray(saved[g.key]) ? saved[g.key] : []).filter((v) => valid.has(v)));
    }
    renderChoices();
    $("#empty-library").hidden = true;
    $("#choices").hidden = false;
    $("#action").hidden = false;
  }

  $("#load-retry").addEventListener("click", start);
  setMainLinks();
  start();
})();
