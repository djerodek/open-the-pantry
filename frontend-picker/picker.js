// What to eat? / On mange quoi?: the picker page (backend: app/picker.py,
// design: specs/dinner-picker-spec.md). No build step, like app.js.
//
// The stage comes first: one big card, the shuffle rolling through titles
// in it, and the picked recipe landing there. The choices (meal types,
// cooking styles, main ingredients, paces) sit below it, all visible but
// quieter. Tapping the card opens the recipe right here, read-only;
// editing stays in the main app. Something else picks another without
// repeating any recipe shown since the round started; Clear resets.
(() => {
  "use strict";

  const { lang: LANG, setLang, tx, txn, tagLabel, groupLabel, valueLabel, translateStatic } = window.OTP_I18N;
  translateStatic(document.body);
  document.title = tx("What to eat?");

  const $ = (sel) => document.querySelector(sel);
  const STORAGE_KEY = "otp-picker-choices";
  const MAX_SHOWN = 500;            // the server ignores more exclusions than this
  // The reel slows down as it stops: about 0.8 s in all.
  const REEL_STEPS_MS = [45, 50, 55, 60, 70, 80, 95, 115, 140];

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
    // Bumped by a chip change or Clear: a pick started before it is out of
    // date and never reaches the screen (its request or shuffle may still
    // be running). cleared: Clear was pressed since the pick started.
    generation: 0,
    cleared: false,
    // Until /api/picker/options says where the main app is.
    mainUrl: `http://${location.hostname}:8090`,
  };

  const reduceMotion = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);

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

  function photoUrl(name) {
    return `/uploads/${encodeURIComponent(name)}`;
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
    // New choices, new round. The recipe on screen stays until the next pick;
    // a pick still running was for the old choices and is dropped.
    state.shown = [];
    state.generation += 1;
  }

  // ---------------------------------------------------------------------
  // The stage card. One frame for every state (waiting, shuffling, the
  // pick, no match), with the photo area always the same size, so nothing
  // on the page jumps when the pick lands.
  // ---------------------------------------------------------------------
  function photoArea(recipe) {
    const area = el("span", { class: "stage-photo" });
    if (recipe && recipe.image_path) {
      area.appendChild(el("img", { src: photoUrl(recipe.image_path), alt: "", decoding: "async" }));
    } else {
      area.classList.add("stage-photo--plate");
      area.appendChild(el("span", { class: "plate", "aria-hidden": "true" }));
    }
    return area;
  }

  function tagLine(recipe) {
    const parts = (recipe.tags || []).map((t) => tagLabel(t));
    if (recipe.cook_time_rating) parts.push(valueLabel(recipe.cook_time_rating));
    return parts.join(" · ");
  }

  // Before any pick, after Clear, and when nothing matches.
  function idleCard(title, hint) {
    return el("div", { class: "stage-card stage-card--idle" }, [
      photoArea(null),
      el("span", { class: "stage-body" }, [
        el("span", { class: "stage-title", text: title }),
        hint ? el("span", { class: "stage-sub", text: hint }) : null,
      ]),
    ]);
  }

  function showIdle() {
    $("#result-card-slot").replaceChildren(idleCard(tx("Hungry?"), tx("Press the button, or narrow it down with the choices first.")));
  }

  // The pick: the whole card is the way into the recipe.
  function resultCard(recipe) {
    const line = tagLine(recipe);
    return el("button", {
      type: "button", class: "stage-card stage-card--result",
      onclick: () => openReader(recipe.id),
    }, [
      photoArea(recipe),
      el("span", { class: "stage-body" }, [
        el("span", { class: "stage-title", text: recipe.title }),
        line ? el("span", { class: "stage-sub", text: line }) : null,
        el("span", { class: "stage-read" }, [
          el("span", { text: tx("Read the recipe") }), el("span", { "aria-hidden": "true", text: " →" }),
        ]),
      ]),
    ]);
  }

  // ---------------------------------------------------------------------
  // Picking
  // ---------------------------------------------------------------------
  const pickButton = $("#pick-button");
  const clearButton = $("#clear-button");

  function setBusy(on) {
    state.busy = on;
    // aria-disabled, not disabled: a disabled button drops the focus, and
    // focus stays on the button pressed (spec section 6).
    if (on) { pickButton.setAttribute("aria-busy", "true"); pickButton.setAttribute("aria-disabled", "true"); }
    else { pickButton.removeAttribute("aria-busy"); pickButton.removeAttribute("aria-disabled"); }
  }

  // "Pick one" until something is on the stage, then "Something else".
  function setButtonLabel() {
    pickButton.textContent = state.current ? tx("Something else") : tx("Pick one");
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

  function showArea({ note = "", message = "", card = null, count = null }) {
    $("#result-note").textContent = note;
    $("#result-note").hidden = !note;
    $("#result-message").textContent = message;
    $("#result-message").hidden = !message;
    if (card) $("#result-card-slot").replaceChildren(card);
    $("#result-count").textContent = count === null ? "" : matchText(count);
    $("#result-count").hidden = count === null;
    setButtonLabel();
  }

  // The reel: titles roll up through the card, slowing to a stop, before
  // the pick lands. The pick is already decided; this only delays showing
  // it. Skipped with reduced motion, or when there's nothing to roll.
  function shuffle(titles) {
    if (reduceMotion() || !titles.length) return Promise.resolve();
    const reel = el("span", { class: "stage-reel" });
    const card = el("div", { class: "stage-card stage-card--shuffling", "aria-hidden": "true" }, [
      photoArea(null),
      el("span", { class: "stage-body" }, [reel]),
    ]);
    $("#result-card-slot").replaceChildren(card);
    return new Promise((resolve) => {
      let i = 0;
      const step = () => {
        const ms = REEL_STEPS_MS[i];
        const item = el("span", { class: "reel-item", text: titles[i % titles.length] });
        item.style.animationDuration = `${ms}ms`;
        reel.replaceChildren(item);
        i += 1;
        setTimeout(i < REEL_STEPS_MS.length ? step : resolve, ms);
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

  // What the stage showed before a pick's shuffle replaced it, put back if
  // that pick turns out to be out of date.
  function snapshotArea() {
    const ids = ["#result-note", "#result-message", "#result-count"];
    const shown = ids.map((id) => [id, $(id).hidden]);
    const card = [...$("#result-card-slot").childNodes];
    return () => {
      for (const [id, hidden] of shown) $(id).hidden = hidden;
      $("#result-card-slot").replaceChildren(...card);
      setButtonLabel();
    };
  }

  async function pick() {
    if (state.busy) return;
    setBusy(true);
    const generation = state.generation;
    state.cleared = false;
    // A chip change or Clear since the pick started: drop it.
    const stale = () => generation !== state.generation;
    let restore = null;
    try {
      let shown = state.shown;
      let data = await api(query(shown));
      if (stale()) return;
      let note = "";
      if (data.exhausted) {
        // Every match has been shown: say so and start a new round. The
        // recipe on screen counts as shown in it, so the new round doesn't
        // open with the same one (unless it's the only match).
        note = data.match_count === 1
          ? tx("That's the only one that matches.")
          : tx("That's all {1} — starting over.", { 1: data.match_count });
        shown = state.current && data.match_count > 1 ? [state.current.id] : [];
        data = await api(query(shown));
        if (stale()) return;
      }
      if (!data.recipe) {
        state.current = null;
        state.shown = [];
        showArea({ card: idleCard(tx("Nothing matches these choices."), tx("Try fewer choices.")) });
        announce(tx("Nothing matches these choices."));
        return;
      }
      const recipe = data.recipe;
      if (recipe.image_path) new Image().src = photoUrl(recipe.image_path);   // load during the shuffle
      restore = snapshotArea();
      $("#result-note").hidden = true;
      $("#result-message").hidden = true;
      await shuffle(data.shuffle_titles || []);
      if (stale()) {
        // A chip change puts back what was on screen; Clear has already
        // reset the stage and it stays that way.
        if (!state.cleared) restore();
        return;
      }
      state.current = recipe;
      state.shown = shown.filter((id) => id !== recipe.id).concat(recipe.id).slice(-MAX_SHOWN);
      const card = resultCard(recipe);
      if (!reduceMotion()) card.classList.add("stage-card--landed");
      showArea({ note, card, count: data.match_count });
      announce([note, `${recipe.title}, ${matchText(data.match_count)}`].filter(Boolean).join(" "));
    } catch (err) {
      if (stale()) return;
      // The recipe on screen, if any, stays; the message goes above the button.
      if (restore) restore();
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
    state.generation += 1;      // a pick still running is dropped
    state.cleared = true;
    showArea({});
    showIdle();
    renderChoices();
    const first = document.querySelector("#choices .chip");
    (first || pickButton).focus();
  }

  pickButton.addEventListener("click", pick);
  clearButton.addEventListener("click", clearAll);

  // ---------------------------------------------------------------------
  // The recipe, read-only. Opens over the page; phone Back, Escape and the
  // close button all close it. Editing stays in the main app (a small link
  // at the end).
  // ---------------------------------------------------------------------
  const readerOverlay = $("#reader-overlay");
  const reader = $("#reader");
  const readerBody = $("#reader-body");
  // Print: the recipe only, on white (picker.css, @media print), like the
  // main app's printout: photo, facts, ratings, ingredients, steps, and the
  // "View original" address written out; notes and links to the app left off.
  const printButton = $("#reader-print");
  printButton.addEventListener("click", () => window.print());
  const RECIPE_HASH = /^#recipe-(\d+)$/;
  let readerTicket = 0;
  let readerOpener = null;
  let readerPushed = false;   // opened with a history entry of its own (Back closes it)

  function ddhhmmLabel(v) {
    const [d, h, m] = String(v || "").split(":").map((p) => parseInt(p, 10) || 0);
    const parts = [];
    if (d) parts.push(`${d} ${tx("d")}`);
    if (h) parts.push(`${h} h`);
    if (m) parts.push(`${m} min`);
    return parts.join(" ") || "0 min";
  }

  // "Sauce tartare:" names a part of the recipe (as in the main app).
  function isPartName(text) {
    const t = (text || "").trim();
    return t.endsWith(":") && t.split(/\s+/).length <= 6;
  }

  function sourceLink(recipe) {
    let url;
    try { url = new URL(recipe.source_url); } catch { return null; }
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return el("a", { class: "reader-link reader-source", href: url.href, target: "_blank", rel: "noopener noreferrer" },
      [el("span", { text: tx("View original") }), el("span", { "aria-hidden": "true", text: " ↗" })]);
  }

  // Ratings as text: nothing here can be changed.
  function ratingsList(recipe) {
    const rows = [];
    if (recipe.tastiness_rating) {
      const n = recipe.tastiness_rating;
      rows.push([tx("Rating"), el("span", { "aria-label": `${n}/5` }, [
        el("span", { class: "stars", "aria-hidden": "true", text: "★".repeat(n) + "☆".repeat(5 - n) }),
      ])]);
    }
    if (recipe.cook_time_rating) rows.push([tx("Pace"), valueLabel(recipe.cook_time_rating)]);
    if (recipe.difficulty_rating) rows.push([tx("Difficulty"), valueLabel(recipe.difficulty_rating)]);
    if (recipe.favorite) rows.push([tx("Favorite"), el("span", {}, [
      el("span", { class: "heart", "aria-hidden": "true", text: "♥ " }), document.createTextNode(tx("Yes")),
    ])]);
    if (!rows.length) return null;
    return el("dl", { class: "reader-ratings" }, rows.map(([label, value]) => el("div", {}, [
      el("dt", { text: label }), el("dd", {}, [typeof value === "string" ? document.createTextNode(value) : value]),
    ])));
  }

  function renderReader(recipe) {
    const facts = [
      recipe.servings ? tx("Servings: {1}", { 1: recipe.servings }) : null,
      recipe.prep_time ? tx("Prep: {1}", { 1: recipe.prep_time }) : null,
      recipe.cook_time ? tx("Cook: {1}", { 1: recipe.cook_time }) : null,
      recipe.total_time ? tx("Total: {1}", { 1: recipe.total_time }) : null,
      recipe.actual_cook_time ? tx("Real-world cook time: {1}", { 1: ddhhmmLabel(recipe.actual_cook_time) }) : null,
    ].filter(Boolean);
    const line = tagLine({ tags: recipe.tags });
    let n = 0;
    // replaceChildren() writes a null argument out as the text "null" (a
    // recipe without a photo showed it above the title), unlike el(), which
    // skips nulls: hence the filter.
    readerBody.replaceChildren(...[
      recipe.image_path ? el("img", { class: "reader-photo", src: photoUrl(recipe.image_path), alt: "" }) : null,
      el("div", { class: "reader-content" }, [
        el("h2", { id: "reader-title", class: "reader-title", text: recipe.title }),
        line ? el("p", { class: "reader-tags", text: line }) : null,
        facts.length ? el("p", { class: "reader-facts" }, facts.map((f) => el("span", { text: f }))) : null,
        ratingsList(recipe),
        recipe.notes ? el("section", { class: "reader-notes" }, [
          el("h3", { text: tx("Notes") }), el("p", { text: recipe.notes }),
        ]) : null,
        el("div", { class: "reader-columns" }, [
          el("section", { class: "reader-ingredients" }, [
            el("h3", { text: tx("Ingredients") }),
            el("ul", {}, (recipe.ingredients || []).map((t) => el("li", { class: isPartName(t) ? "list-part" : null, text: t }))),
          ]),
          el("section", { class: "reader-steps" }, [
            el("h3", { text: tx("Instructions") }),
            // Part names aren't numbered; the steps after them carry on.
            el("ol", {}, (recipe.steps || []).map((t) => isPartName(t)
              ? el("li", { class: "list-part", text: t })
              : el("li", { value: String(++n), text: t }))),
          ]),
        ]),
        el("div", { class: "reader-footer" }, [
          sourceLink(recipe),
          el("a", { class: "reader-link reader-edit", href: `${state.mainUrl}/#recipe-${recipe.id}` },
            [el("span", { text: tx("Edit in Open the Pantry") })]),
        ]),
      ]),
    ].filter(Boolean));
    printButton.hidden = false;   // only once there is a recipe to print
  }

  function readerFocusables() {
    return [...reader.querySelectorAll("button, a[href]")].filter((n) => n.offsetParent !== null);
  }

  function onReaderKey(e) {
    if (e.key === "Escape") { e.preventDefault(); closeReader(); return; }
    if (e.key !== "Tab") return;
    const f = readerFocusables();
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  async function openReader(id, { fromHash = false } = {}) {
    const ticket = ++readerTicket;
    if (readerOverlay.hidden) {
      readerOpener = document.activeElement;
      readerOverlay.hidden = false;
      document.documentElement.classList.add("reader-open");
      document.addEventListener("keydown", onReaderKey);
      if (!fromHash) {
        history.pushState({ pickerReader: id }, "", `#recipe-${id}`);
        readerPushed = true;
      }
    }
    printButton.hidden = true;
    readerBody.replaceChildren(el("p", { class: "reader-loading", role: "status", text: tx("Loading...") }));
    $("#reader-close").focus();
    let recipe = null, failure = null;
    try {
      recipe = await api(`/api/picker/recipes/${id}`);
    } catch (err) { failure = err; }
    if (ticket !== readerTicket || readerOverlay.hidden) return;   // superseded or closed
    if (!recipe) {
      readerBody.replaceChildren(el("p", { class: "reader-loading", role: "alert",
        text: failure && failure.status === 404 ? tx("Could not load recipe.") : errorText(failure) }));
      return;
    }
    renderReader(recipe);
    reader.scrollTop = 0;
  }

  // Closes the sheet. With its own history entry, going back closes it
  // (popstate below), so Back and the close button do the same thing.
  function closeReader() {
    if (readerOverlay.hidden) return;
    if (readerPushed && history.state && history.state.pickerReader) { history.back(); return; }
    hideReader();
    if (RECIPE_HASH.test(location.hash)) history.replaceState(null, "", location.pathname + location.search);
  }

  function hideReader() {
    readerTicket += 1;
    readerPushed = false;
    readerOverlay.hidden = true;
    printButton.hidden = true;
    document.documentElement.classList.remove("reader-open");
    document.removeEventListener("keydown", onReaderKey);
    readerBody.replaceChildren();
    const back = readerOpener;
    readerOpener = null;
    if (back && document.contains(back)) back.focus();
    else pickButton.focus();
  }

  window.addEventListener("popstate", () => {
    const m = RECIPE_HASH.exec(location.hash);
    if (m) openReader(Number(m[1]), { fromHash: true });
    else if (!readerOverlay.hidden) hideReader();
  });
  $("#reader-close").addEventListener("click", closeReader);
  readerOverlay.addEventListener("click", (e) => { if (e.target === readerOverlay) closeReader(); });

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
    $("#choices-loading").hidden = true;
    state.options = options;
    state.mainUrl = webAddress(options.main_url, state.mainUrl);
    setMainLinks();
    if (!options.total) {
      $("#choices-panel").hidden = true;
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
    if (!state.current) showIdle();
    setButtonLabel();
    $("#empty-library").hidden = true;
    $("#choices-panel").hidden = false;
    $("#action").hidden = false;
  }

  $("#load-retry").addEventListener("click", start);
  setMainLinks();
  start();
  // Opened at /#recipe-<id> (a shared or reloaded link): that recipe.
  const linked = RECIPE_HASH.exec(location.hash);
  if (linked) openReader(Number(linked[1]), { fromHash: true });
})();
