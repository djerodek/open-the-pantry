// Add a recipe (0189, moved from app.js unchanged): the Add dialog and its
// sources (a link, a PDF, photos, batches of links or PDFs), the review
// screen, "Already in your library?", and the manual form.
//
// As in settings.js: functions, element lookups and constants at the top
// level; the setup steps in initAddRecipe(), which app.js calls where the
// code used to be. app.js's helpers used here are imported from it.
//
// Keep it that way. app.js imports this module and this module imports
// app.js, so this one runs first, before app.js's own code: at the top
// level here, only function declarations, element lookups ($ and $$ are
// function declarations in app.js, ready that early) and constants. A top
// level line that calls or reads anything else from app.js (state, tx,
// el, ...) throws "can't access before initialization"; put it in the
// init function instead.
import {
  API, tx, tagLabel, $, $$, announce, announceError, el, topOverlay, openModal, closeModal, tagsFromInput, linesOf,
  ingredientsOf, jsonBody, discardDraftFile, refreshLibrary, state, loadTimeBuckets, loadRecipes, tagSourcesNote,
  ingredientText, makeDurationInput, openRecipeDetail, partHint, fetchWithTimeout, writeFailureMessage,
} from "./app.js";
// The + menu's "Check email inbox" (missing until 0190: the button threw
// a ReferenceError; review of 0185-0189).
import { runInboxScan } from "./settings.js";

// -------------------------------------------------------------------
// Add-recipe modal: source picker -> ingestion form -> review -> save
// -------------------------------------------------------------------
const addOverlay = $("#add-recipe-overlay");
const addBody = $("#add-recipe-body");

async function discardCurrentDraftFiles() {
  const files = [...state.currentDraftFiles];
  state.currentDraftFiles.clear();
  await Promise.all(files.map(discardDraftFile));
}

function trackDraftFile(filename) {
  if (filename) state.currentDraftFiles.add(filename);
}

function showSourcePicker() {
  addBody.innerHTML = "";
  const picker = el("div", { class: "source-type-picker" }, [
    el("button", { type: "button", text: tx("\ud83d\udd17 URL"), onclick: showUrlForm }),
    el("button", { type: "button", text: tx("\ud83d\udcc4 PDF"), onclick: showPdfForm }),
    el("button", { type: "button", text: tx("\ud83d\udcf7 Screenshot/Photo"), onclick: () => showImageForm() }),
    el("button", { type: "button", text: tx("\u270d\ufe0f Manual/Handwritten"), onclick: showManualForm }),
  ]);
  addBody.appendChild(picker);

  // Only offered once email ingest is switched on; otherwise it would be
  // a button that can only say "not configured".
  fetch(`${API}/email-status`).then((r) => (r.ok ? r.json() : null)).then((settings) => {
    if (!settings || !settings.enabled || !picker.isConnected) return;
    const status = el("p", { class: "field-hint scan-status", role: "status" });
    const btn = el("button", {
      type: "button", text: tx("\ud83d\udce5 Check email inbox"),
      onclick: () => runInboxScan(status, btn),
    });
    picker.appendChild(btn);
    addBody.appendChild(status);
  }).catch(() => {});
}

// One saved recipe in a batch, import or inbox-scan result, with Delete
// to undo a bad import right there.
function savedResultRow(recipeId, title, note) {
  const li = el("li", { class: "ok" });
  const text = el("span", { text: `\u2713 ${title}${note || ""}` });
  const del = el("button", {
    type: "button", class: "btn-secondary btn-small btn-danger result-delete", text: tx("Delete"),
    "aria-label": tx("Delete {1}", { 1: title }),
    onclick: async () => {
      if (!confirm(tx("Delete this recipe? You can put it back from Recently deleted for 30 days."))) return;
      del.disabled = true;
      const res = await fetchWithTimeout(`${API}/recipes/${recipeId}`, { method: "DELETE" });
      if (res.ok || res.status === 404) {
        li.className = "deleted";
        text.textContent = `\u2715 ${tx("Deleted: {1}", { 1: title })}`;
        del.remove();
        announce(tx("Moved to Recently deleted (in the side menu) for 30 days."));
        await loadTimeBuckets();
        loadRecipes();
        return;
      }
      del.disabled = false;
      announceError(writeFailureMessage(res, tx("The recipe")));
    },
  });
  li.append(text, del);
  return li;
}

// A batch import's answer: what was added and what failed, then the
// library refreshed; or the error.
async function showBatchResult(res, resultsEl) {
  const result = await res.json();
  if (!res.ok) { resultsEl.textContent = result.detail || tx("Batch import failed (error {1}).", { 1: res.status }); return; }
  renderBatchResults(resultsEl, result);
  announce(tx("{1} added, {2} failed.", { 1: result.succeeded.length, 2: result.failed.length }));
  await refreshLibrary();
}

function renderBatchResults(container, result) {
  container.innerHTML = "";
  const list = el("ul", { class: "batch-results-list" });
  for (const s of result.succeeded) {
    const note = s.duplicate_of ? ` \u2014 ${tx("a recipe with this title was already in your library")}` : "";
    list.appendChild(savedResultRow(s.recipe_id, s.title, note));
  }
  for (const k of result.skipped || []) {
    list.appendChild(el("li", { class: "skipped", text: `\u21b7 ${k.title} \u2014 ${tx("already in your library from this link; not added again")}` }));
  }
  for (const f of result.failed) {
    const where = f.url || f.filename || [f.title, f.origin].filter(Boolean).join(" \u2014 ");
    list.appendChild(el("li", { class: "fail", text: `\u2717 ${where}: ${f.error}` }));
  }
  container.appendChild(list);
}

// -------------------------------------------------------------------
// "Already in your library?" for the review screen and Add manually
// (backend: duplicates.py). Shown as soon as the screen opens (or the
// title is typed) and checked again on Save: a match stops the first Save,
// and a second Save with the same title and link goes ahead. Opening the saved one
// closes this dialog.
// -------------------------------------------------------------------
function duplicateGuard(getTitle, getUrl) {
  const box = el("div", { class: "dup-warning", role: "status", hidden: "" });
  let blockedFor = null;   // the title|link a Save was stopped for
  async function lookup() {
    const title = (getTitle() || "").trim(), url = (getUrl() || "").trim();
    if (!title && !url) return [];
    const q = new URLSearchParams();
    if (title) q.set("title", title);
    if (url) q.set("url", url);
    try {
      const res = await fetch(`${API}/recipes/duplicates?${q}`, { cache: "no-store" });
      return res.ok ? await res.json() : [];
    } catch { return []; }   // offline or failed: never blocks saving
  }
  function show(found) {
    box.replaceChildren();
    box.hidden = !found.length;
    if (!found.length) return;
    box.appendChild(el("p", { class: "dup-warning-title", text: tx("This may already be in your library:") }));
    box.appendChild(el("ul", {}, found.map((d) => el("li", {}, [
      el("span", { text: `${d.title} ` }),
      el("span", { class: "dup-reason", text: d.reason === "link" ? tx("(same link)") : tx("(same title)") }),
      document.createTextNode(" "),
      el("button", { type: "button", class: "link-button", text: tx("Open it"),
        onclick: () => { closeModal(addOverlay); openRecipeDetail(d.id); } }),
    ]))));
    box.appendChild(el("p", { class: "dup-warning-hint", text: tx("Press Save again to add it anyway.") }));
  }
  const key = () => `${(getTitle() || "").trim()}|${(getUrl() || "").trim()}`;
  // Only the latest lookup may change the warning: an older answer
  // arriving late would show matches for text no longer in the form.
  let ticket = 0;
  return {
    box,
    // On opening: show any match, without stopping anything.
    async preview() {
      const mine = ++ticket;
      const found = await lookup();
      if (mine === ticket) show(found);
    },
    // On Save: true to go ahead.
    async allowSave() {
      // The title and link as checked are read before the lookup; if
      // they were edited while it ran, the new ones are checked before
      // anything is decided (GPT review of 0093: the edited text was
      // marked as already warned about without being checked).
      let checked, found;
      for (;;) {
        checked = key();
        if (blockedFor === checked) return true;
        const mine = ++ticket;
        found = await lookup();
        if (mine === ticket) show(found);
        if (checked === key()) break;   // changed meanwhile: check what's there now
      }
      if (!found.length) return true;
      blockedFor = checked;
      box.scrollIntoView({ block: "nearest" });
      announce(tx("This may already be in your library:") + " " + found.map((d) => d.title).join(", "));
      return false;
    },
  };
}

function modeSwitchControl(onSingle, onBatch, labels = [tx("Single"), tx("Batch")]) {
  const buttons = [
    el("button", { type: "button", "aria-pressed": "true", text: labels[0] }),
    el("button", { type: "button", "aria-pressed": "false", text: labels[1] }),
  ];
  buttons[0].addEventListener("click", () => {
    buttons[0].setAttribute("aria-pressed", "true");
    buttons[1].setAttribute("aria-pressed", "false");
    onSingle();
  });
  buttons[1].addEventListener("click", () => {
    buttons[0].setAttribute("aria-pressed", "false");
    buttons[1].setAttribute("aria-pressed", "true");
    onBatch();
  });
  return el("div", { class: "mode-switch" }, buttons);
}

// A URL import that failed or came back half done (no ingredients or no
// steps): say why, and send the person to a PDF of the page -- made in
// their own browser, which loaded the whole page -- with Add from PDF one
// tap away. A partial draft can still be opened ("Continue anyway").
function showPdfFallback(message, draft) {
  addBody.innerHTML = "";
  addBody.appendChild(el("h3", { text: draft ? tx("Only part of the recipe came through") : tx("Couldn't import this page") }));
  addBody.appendChild(el("p", { class: "fallback-reason", text: message }));
  addBody.appendChild(el("p", { text: tx("A PDF of the page usually works: your browser has the whole page, and the app reads recipe text from PDFs well.") }));
  addBody.appendChild(el("p", { class: "filter-section-title", text: tx("Make a PDF") }));
  addBody.appendChild(el("ul", { class: "fallback-steps" }, [
    el("li", { text: tx("iPhone or iPad (Safari): Share → Options → PDF, then Save to Files.") }),
    el("li", { text: tx("Android (Chrome): ⋮ → Share → Print → Save as PDF.") }),
    el("li", { text: tx("Computer: Print → Save as PDF.") }),
  ]));
  addBody.appendChild(el("p", { class: "field-hint", text: tx("Or email the PDF to the recipe inbox as an attachment, if email ingest is set up.") }));
  const actions = [
    el("button", { type: "button", class: "btn-primary", text: tx("Add from PDF"), onclick: async () => {
      if (draft) await discardCurrentDraftFiles();
      showPdfForm();
    } }),
  ];
  if (draft) {
    actions.push(el("button", { type: "button", class: "btn-secondary", text: tx("Continue anyway"),
      onclick: () => showReviewScreen(draft) }));
  } else {
    actions.push(el("button", { type: "button", class: "btn-secondary", text: tx("Try another link"), onclick: () => showUrlForm() }));
  }
  addBody.appendChild(el("div", { class: "fallback-actions" }, actions));
  // The reason is on screen already; this is for screen readers.
  announce(message);
}

// A PDF or screenshot that gave only part of a recipe (no ingredients or
// no steps), or couldn't be read at all: say why, and offer the other
// ways in -- screenshots of the recipe card for OCR, a PDF, or typing it.
// A partial draft can still be opened ("Continue anyway").
// kind: "pdf", "image" (one screenshot) or "images" (several combined).
function showSourceFallback(kind, message, draft) {
  addBody.innerHTML = "";
  addBody.appendChild(el("h3", { text: draft ? tx("Only part of the recipe came through") : tx("Couldn't read this file") }));
  addBody.appendChild(el("p", { class: "fallback-reason", text: message }));
  if (kind === "pdf") {
    addBody.appendChild(el("p", { text: tx("Many recipe pages put the recipe card at the bottom, after the article. If the PDF stops before it, there's no recipe in it to read.") }));
  } else if (kind === "image") {
    addBody.appendChild(el("p", { text: tx("If the recipe takes several screenshots, add them together with Combine multiple.") }));
  } else {
    addBody.appendChild(el("p", { text: tx("Check that the screenshots cover the whole recipe card, ingredients and steps.") }));
  }
  addBody.appendChild(el("p", { class: "filter-section-title", text: tx("Other ways to add it") }));
  const ways = [];
  if (kind === "pdf") {
    ways.push(el("li", { text: tx("Screenshots of the recipe card: the app reads them with text recognition. Use Combine multiple if it takes more than one.") }));
  } else {
    ways.push(el("li", { text: tx("A PDF of the page, if it came from a website: text in a PDF reads more reliably than a screenshot.") }));
  }
  ways.push(el("li", { text: tx("Enter it by hand.") }));
  addBody.appendChild(el("ul", { class: "fallback-steps" }, ways));

  const leave = (next) => async () => {
    if (draft) await discardCurrentDraftFiles();
    next();
  };
  const actions = [];
  if (kind === "pdf") {
    actions.push(el("button", { type: "button", class: "btn-primary", text: tx("Add from screenshots"), onclick: leave(() => showImageForm(true)) }));
  } else {
    if (kind === "image") actions.push(el("button", { type: "button", class: "btn-primary", text: tx("Combine multiple"), onclick: leave(() => showImageForm(true)) }));
    actions.push(el("button", { type: "button", class: kind === "image" ? "btn-secondary" : "btn-primary", text: tx("Add from PDF"), onclick: leave(showPdfForm) }));
  }
  actions.push(el("button", { type: "button", class: "btn-secondary", text: tx("Enter manually"), onclick: leave(showManualForm) }));
  if (draft) {
    actions.push(el("button", { type: "button", class: "btn-secondary", text: tx("Continue anyway"), onclick: () => showReviewScreen(draft) }));
  }
  addBody.appendChild(el("div", { class: "fallback-actions" }, actions));
  announce(message);
}

// The reason in a failed ingest response, or a fallback sentence.
async function errorDetail(res, fallback) {
  const err = await res.json().catch(() => ({}));
  return typeof err.detail === "string" ? err.detail : fallback;
}

// prefill: a link shared from another app (sharedLinkFromAddress), put in
// the box for the person to fetch.
function showUrlForm(prefill) {
  addBody.innerHTML = "";
  addBody.appendChild(el("h3", { text: tx("Add from URL") }));
  const formArea = el("div");

  function renderSingle() {
    formArea.innerHTML = "";
    const input = el("input", { type: "url", id: "url-input", required: "", placeholder: tx("https://example.com/recipe") });
    formArea.appendChild(el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        setBusy(true, tx("Fetching recipe..."));
        try {
          const res = await fetch(`${API}/ingest/url`, jsonBody("POST", { url: input.value.trim() }));
          if (!res.ok) {
            showPdfFallback(await errorDetail(res, tx("Extraction failed.")), null);
            return;
          }
          const draft = await res.json();
          trackDraftFile(draft.image_path);
          if (draft.incomplete) showPdfFallback(draft.incomplete.message, draft);
          else showReviewScreen(draft);
        } catch {
          showPdfFallback(tx("Couldn't reach Open the Pantry. Check your connection and try again."), null);
        } finally { setBusy(false); }
      },
    }, [
      el("div", { class: "field" }, [el("label", { for: "url-input", text: tx("Recipe URL") }), input]),
      el("button", { class: "btn-primary", type: "submit", text: tx("Fetch recipe") }),
    ]));
  }

  function renderBatch() {
    formArea.innerHTML = "";
    const textarea = el("textarea", { id: "url-batch-input", placeholder: tx("One URL per line"), style: "min-height:8rem;" });
    const resultsEl = el("div");
    formArea.appendChild(el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        const urls = textarea.value.split("\n").map((s) => s.trim()).filter(Boolean);
        if (!urls.length) return;
        if (urls.length > 50) {  // MAX_BATCH_URLS on the server
          resultsEl.textContent = tx("That's {1} links; the limit is 50 per batch. Send the rest separately.", { 1: urls.length });
          return;
        }
        setBusy(true, tx("Fetching {1} recipe(s)...", { 1: urls.length }));
        try {
          const res = await fetch(`${API}/ingest/url/batch`, jsonBody("POST", { urls }));
          await showBatchResult(res, resultsEl);
        } catch {
          alert(tx("Batch import failed."));
        } finally { setBusy(false); }
      },
    }, [
      el("div", { class: "field" }, [
        el("label", { for: "url-batch-input", text: tx("Recipe URLs (one per line)") }),
        textarea,
        el("div", { class: "field-hint", text: tx("Each recipe is saved directly using auto-detected fields \u2014 open it afterward to correct anything.") }),
      ]),
      el("button", { class: "btn-primary", type: "submit", text: tx("Import all") }),
      resultsEl,
    ]));
  }

  addBody.appendChild(modeSwitchControl(renderSingle, renderBatch));
  addBody.appendChild(formArea);
  renderSingle();
  if (typeof prefill === "string" && prefill) {
    // Filled in, not fetched: a share and a link made by another site look
    // the same, and only a tap here shows the person meant it (Claude
    // review round 9: a hidden frame elsewhere could start an import).
    const input = $("#url-input");
    input.value = prefill;
    const form = formArea.querySelector("form");
    form.insertBefore(el("p", { class: "field-hint", role: "status", text: tx("Shared link: press Fetch recipe to import it.") }), form.firstChild);
  }
}

// Share to Open the Pantry. Android's share sheet (manifest.json
// share_target) and an iPhone Shortcut both open
// /?share_url=...&share_text=...&share_title=...; the first http(s) link
// in them goes into Add from URL, ready to fetch with one tap.
// The parameters are removed from the address straight away, so a reload
// or Back doesn't import it again.
function sharedLinkFromAddress() {
  const params = new URLSearchParams(location.search);
  if (!["share_url", "share_text", "share_title"].some((k) => params.has(k))) return null;
  const candidates = [params.get("share_url"), params.get("share_text"), params.get("share_title")]
    .filter(Boolean).join(" ").match(/https?:\/\/[^\s<>"]+/gi) || [];
  for (const k of ["share_url", "share_text", "share_title"]) params.delete(k);
  const rest = params.toString();
  history.replaceState(history.state, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);
  for (const c of candidates) {
    try {
      const u = new URL(c.replace(/[).,;!?'"]+$/, ""));
      if (u.protocol === "http:" || u.protocol === "https:") return u.href;
    } catch { /* not a link */ }
  }
  return "";      // shared, but nothing to fetch
}

function openSharedLink() {
  const link = sharedLinkFromAddress();
  if (link === null) return;
  state.currentDraftFiles.clear();
  if (link) {
    showUrlForm(link);
  } else {
    showUrlForm();
    announceError(tx("That share had no web link in it. Paste the recipe's address here."));
  }
  openModal(addOverlay);
  // The next step is the button (or the empty box).
  const next = link ? addBody.querySelector("form button[type=submit]") : $("#url-input");
  if (next) next.focus();
}

function showPdfForm() {
  addBody.innerHTML = "";
  addBody.appendChild(el("h3", { text: tx("Add from PDF") }));
  const formArea = el("div");

  function renderSingle() {
    formArea.innerHTML = "";
    const input = el("input", { type: "file", id: "pdf-input", accept: "application/pdf", required: "" });
    formArea.appendChild(el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!input.files.length) return;
        setBusy(true, tx("Reading PDF (this can take a moment for scanned pages)..."));
        try {
          const fd = new FormData(); fd.append("file", input.files[0]);
          const res = await fetch(`${API}/ingest/pdf`, { method: "POST", body: fd });
          if (!res.ok) { showSourceFallback("pdf", await errorDetail(res, tx("Could not process PDF.")), null); return; }
          const draft = await res.json();
          trackDraftFile(draft.stored_file);
          trackDraftFile(draft.image_path);
          if (draft.incomplete) showSourceFallback("pdf", draft.incomplete.message, draft);
          else showReviewScreen(draft);
        } catch {
          showSourceFallback("pdf", tx("Couldn't reach Open the Pantry. Check your connection and try again."), null);
        } finally { setBusy(false); }
      },
    }, [
      el("div", { class: "field" }, [
        el("label", { for: "pdf-input", text: tx("PDF file") }),
        input,
        el("div", { class: "field-hint", text: tx("Text-based PDFs are read directly; scanned pages fall back to OCR automatically.") }),
      ]),
      el("button", { class: "btn-primary", type: "submit", text: tx("Process PDF") }),
    ]));
  }

  function renderBatch() {
    formArea.innerHTML = "";
    const input = el("input", { type: "file", id: "pdf-batch-input", accept: "application/pdf", multiple: "" });
    const resultsEl = el("div");
    formArea.appendChild(el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!input.files.length) return;
        if (input.files.length > 20) {  // MAX_BATCH_PDFS on the server
          resultsEl.textContent = tx("That's {1} PDFs; the limit is 20 per batch. Send the rest separately.", { 1: input.files.length });
          return;
        }
        setBusy(true, tx("Processing {1} PDF(s)...", { 1: input.files.length }));
        try {
          const fd = new FormData();
          for (const f of input.files) fd.append("files", f);
          const res = await fetch(`${API}/ingest/pdf/batch`, { method: "POST", body: fd });
          await showBatchResult(res, resultsEl);
        } catch {
          alert(tx("Batch import failed."));
        } finally { setBusy(false); }
      },
    }, [
      el("div", { class: "field" }, [
        el("label", { for: "pdf-batch-input", text: tx("PDF files") }),
        input,
        el("div", { class: "field-hint", text: tx("Each is saved directly using auto-detected fields.") }),
      ]),
      el("button", { class: "btn-primary", type: "submit", text: tx("Import all") }),
      resultsEl,
    ]));
  }

  addBody.appendChild(modeSwitchControl(renderSingle, renderBatch));
  addBody.appendChild(formArea);
  renderSingle();
}

// combine: open on "Combine multiple" (from the PDF fallback, where the
// recipe card is usually more than one screenshot).
function showImageForm(combine = false) {
  addBody.innerHTML = "";
  addBody.appendChild(el("h3", { text: tx("Add from screenshot / photo") }));
  const formArea = el("div");

  function renderSingle() {
    formArea.innerHTML = "";
    const input = el("input", { type: "file", id: "image-input", accept: "image/*", required: "" });  // no capture=: that forced the camera; iOS now offers Photo Library, Take Photo or Choose File
    formArea.appendChild(el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!input.files.length) return;
        setBusy(true, tx("Reading image with OCR..."));
        try {
          const fd = new FormData(); fd.append("file", input.files[0]);
          const res = await fetch(`${API}/ingest/image`, { method: "POST", body: fd });
          if (!res.ok) { showSourceFallback("image", await errorDetail(res, tx("Could not process image.")), null); return; }
          const draft = await res.json();
          trackDraftFile(draft.stored_file);
          if (draft.incomplete) showSourceFallback("image", draft.incomplete.message, draft);
          else showReviewScreen(draft);
        } catch {
          showSourceFallback("image", tx("Couldn't reach Open the Pantry. Check your connection and try again."), null);
        } finally { setBusy(false); }
      },
    }, [
      el("div", { class: "field" }, [
        el("label", { for: "image-input", text: tx("Screenshot or photo") }),
        input,
        el("div", { class: "field-hint", text: tx("Best for rendered/screenshot text. Handwriting recognizes poorly \u2014 use Manual entry for handwritten cards instead.") }),
      ]),
      el("button", { class: "btn-primary", type: "submit", text: tx("Process image") }),
    ]));
  }

  function renderCombine() {
    formArea.innerHTML = "";
    const input = el("input", { type: "file", id: "image-combine-input", accept: "image/*", multiple: "", required: "" });
    formArea.appendChild(el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        if (!input.files.length) return;
        setBusy(true, tx("Reading {1} image(s) with OCR...", { 1: input.files.length }));
        try {
          const fd = new FormData();
          for (const f of input.files) fd.append("files", f);
          const res = await fetch(`${API}/ingest/images`, { method: "POST", body: fd });
          if (!res.ok) { showSourceFallback("images", await errorDetail(res, tx("Could not process images.")), null); return; }
          const draft = await res.json();
          trackDraftFile(draft.stored_file);
          if (draft.incomplete) showSourceFallback("images", draft.incomplete.message, draft);
          else showReviewScreen(draft);
        } catch {
          showSourceFallback("images", tx("Couldn't reach Open the Pantry. Check your connection and try again."), null);
        } finally { setBusy(false); }
      },
    }, [
      el("div", { class: "field" }, [
        el("label", { for: "image-combine-input", text: tx("Screenshots (select all, in reading order)") }),
        input,
        el("div", { class: "field-hint", text: tx("For a recipe that spans multiple screenshots \u2014 each is read with OCR and combined into one recipe. Most file pickers preserve the order you select files in; the first image becomes the showcase photo (changeable afterward).") }),
      ]),
      el("button", { class: "btn-primary", type: "submit", text: tx("Process & combine") }),
    ]));
  }

  const modeSwitch = modeSwitchControl(renderSingle, renderCombine, [tx("Single"), tx("Combine multiple")]);
  addBody.appendChild(modeSwitch);
  addBody.appendChild(formArea);
  if (combine) modeSwitch.querySelectorAll("button")[1].click();
  else renderSingle();
}

// Disables the controls of the dialog that's in front while it works,
// and re-enables only those it disabled: it used to reach every dialog,
// stacked ones included, and re-enabled buttons that were meant to stay
// off (DeepSeek review of 0073).
const busyDisabled = new Set();
function setBusy(isBusy, msg) {
  if (isBusy) {
    announce(msg || tx("Working..."));
    const scope = topOverlay() || document;
    $$(".modal button, .modal input, .modal textarea, .modal select", scope).forEach((n) => {
      if (!n.disabled) { n.disabled = true; busyDisabled.add(n); }
    });
  } else {
    busyDisabled.forEach((n) => { n.disabled = false; });
    busyDisabled.clear();
  }
}

// -------------------------------------------------------------------
// Review / edit screen (shared by URL / PDF / screenshot ingestion)
// DOM order is raw-text-first, then editable fields, so screen readers
// encounter the source material before the fields meant to correct it.
// -------------------------------------------------------------------
function showReviewScreen(draft) {
  addBody.innerHTML = "";
  addBody.appendChild(el("h3", { text: tx("Review & confirm") }));

  if (draft.ocr_confidence != null) {
    const tier = draft.ocr_confidence >= 80 ? "high" : draft.ocr_confidence >= 55 ? "med" : "low";
    const msg = tier === "low"
      ? tx("OCR quality was low on this input \u2014 please check the fields carefully.")
      : tx("OCR confidence: {1}%", { 1: Math.round(draft.ocr_confidence) });
    addBody.appendChild(el("div", { class: `badge badge-confidence-${tier}`, text: msg, style: "margin-bottom:0.75rem;display:inline-block;" }));
  }
  // Said before saving too, not only in a saved recipe's "Check these"
  // (reviews of 0174 and 0190).
  if (draft.text_cut) {
    addBody.appendChild(el("div", {
      class: "badge badge-confidence-low", role: "note", style: "margin-bottom:0.75rem;display:inline-block;",
      text: tx("Only the start of the text was read, because it is very long. What comes after is not in this recipe."),
    }));
  }

  const titleInput = el("input", { type: "text", id: "review-title", value: draft.title || "" });
  const servingsInput = el("input", { type: "text", id: "review-servings", maxlength: "100", placeholder: tx("e.g. 4"), value: draft.servings || "" });
  const ingredientsArea = el("textarea", { id: "review-ingredients", class: "recipe-lines" });
  ingredientsArea.value = (draft.ingredients || [])
    .map((i) => ingredientText(i))
    .join("\n");
  const stepsArea = el("textarea", { id: "review-steps", class: "recipe-lines" });
  stepsArea.value = (draft.steps || []).map((s) => (typeof s === "string" ? s : s.text)).join("\n");

  const tagsInput = el("input", {
    type: "text", id: "review-tags",
    value: (draft.suggested_tags || []).map((t) => tagLabel(t)).join(", "),
  });

  const duration = makeDurationInput("review-time");

  let useImage = !!draft.image_path;
  const imagePreviewWrap = el("div", { class: "field" });
  function renderImagePreview() {
    imagePreviewWrap.innerHTML = "";
    if (!draft.image_path || !useImage) return;
    imagePreviewWrap.appendChild(el("label", { text: tx("Showcase image (auto-detected)") }));
    imagePreviewWrap.appendChild(el("img", {
      src: `/tmp-preview/${draft.image_path}`, alt: "",
      style: "max-width:100%;max-height:12rem;border-radius:var(--radius);display:block;margin-bottom:0.4rem;",
    }));
    imagePreviewWrap.appendChild(el("button", {
      class: "btn-secondary", type: "button", text: tx("Don't use this image"),
      onclick: () => { useImage = false; renderImagePreview(); },
    }));
  }
  renderImagePreview();

  const columns = el("div", { class: "review-columns" }, [
    el("div", {}, [
      el("h4", { text: tx("Extracted text (source)") }),
      el("div", { class: "review-raw", text: draft.raw_text || "(no raw text captured)" }),
    ]),
    el("div", {}, [
      el("div", { class: "field" }, [el("label", { for: "review-title", text: tx("Title") }), titleInput]),
      el("div", { class: "field" }, [el("label", { for: "review-servings", text: tx("Servings (optional)") }), servingsInput]),
      imagePreviewWrap,
      el("div", { class: "field" }, [
        el("label", { for: "review-ingredients", text: tx("Ingredients (one per line)") }),
        ingredientsArea,
        partHint(ingredientsArea),
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "review-steps", text: tx("Steps (one per line)") }),
        stepsArea,
        partHint(stepsArea),
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "review-tags", text: tx("Tags (comma or semicolon separated)") }),
        tagsInput,
        tagSourcesNote(draft.suggested_tags),
        el("div", { class: "field-hint", text: tx("Auto-suggested from keywords \u2014 edit freely.") }),
      ]),
      el("div", { class: "field" }, [
        el("label", { text: tx("Real-world cook time (optional)") }),
        duration.element,
        el("div", { class: "field-hint", text: tx("How long it actually took you \u2014 separate from any time listed by the source. Leave blank to skip.") }),
      ]),
    ]),
  ]);
  addBody.appendChild(columns);

  const dupGuard = duplicateGuard(() => titleInput.value, () => draft.source_url);
  addBody.appendChild(dupGuard.box);
  dupGuard.preview();

  const saveBtn = el("button", {
    class: "btn-primary", type: "button", text: tx("Save recipe"),
    style: "margin-top:1rem;",
    onclick: async () => {
      if (!(await dupGuard.allowSave())) return;
      setBusy(true, tx("Saving recipe..."));
      try {
        const tagObjs = tagsFromInput(tagsInput.value);

        // draft.image_path is the recipe's actual showcase image when
        // present, regardless of source type: for screenshots it's the
        // screenshot itself; for PDFs it's a separately-extracted
        // embedded image (distinct from draft.stored_file, the source
        // PDF, which is never kept); for URLs it's a downloaded
        // thumbnail. All three are already TMP_DIR draft references set
        // correctly server-side. useImage reflects the review screen's
        // "Don't use this image" option, if the user rejected it.
        const imageRef = useImage ? (draft.image_path || null) : null;

        const payload = {
          title: titleInput.value.trim() || tx("Untitled Recipe"),
          source_type: draft.source_type,
          source_url: draft.source_url || null,
          servings: servingsInput.value.trim() || null,
          prep_time: draft.prep_time || null,
          cook_time: draft.cook_time || null,
          total_time: draft.total_time || null,
          image_path: imageRef,
          raw_text: draft.raw_text || null,
          ocr_confidence: draft.ocr_confidence != null ? draft.ocr_confidence : null,
          actual_cook_time: duration.getValue(),
          ingredients: ingredientsOf(ingredientsArea),
          steps: linesOf(stepsArea),
          tags: tagObjs,
        };

        const res = await fetch(`${API}/recipes`, jsonBody("POST", payload));
        if (!res.ok) throw new Error(tx("Could not save recipe."));

        // Every tracked draft file for this session gets cleaned up
        // except whichever one actually became the recipe's image
        // (promoted server-side already) -- e.g. a source PDF's own temp
        // file is never kept even when its extracted thumbnail is.
        const filesToDiscard = [...state.currentDraftFiles].filter((f) => f !== imageRef);
        state.currentDraftFiles.clear();
        filesToDiscard.forEach(discardDraftFile);

        closeModal(addOverlay);
        announce(tx("Recipe saved."));
        await refreshLibrary();
      } catch (err) {
        alert(err.message);
      } finally { setBusy(false); }
    },
  });
  addBody.appendChild(saveBtn);
}

// -------------------------------------------------------------------
// Manual entry form (also used for handwritten cards: photo attach +
// free-text fields, no OCR/parsing attempted)
// -------------------------------------------------------------------
function showManualForm() {
  addBody.innerHTML = "";
  addBody.appendChild(el("h3", { text: tx("Manual / handwritten entry") }));

  const titleInput = el("input", { type: "text", id: "manual-title", required: "" });
  const servingsInput = el("input", { type: "text", id: "manual-servings", maxlength: "100", placeholder: tx("e.g. 4") });
  const ingredientsArea = el("textarea", { id: "manual-ingredients", class: "recipe-lines", placeholder: tx("One ingredient per line") });
  const stepsArea = el("textarea", { id: "manual-steps", class: "recipe-lines", placeholder: tx("One step per line") });
  const tagsInput = el("input", { type: "text", id: "manual-tags", placeholder: tx("e.g. dinner, stovetop, beef") });
  const imageInput = el("input", { type: "file", id: "manual-image", accept: "image/*" });
  const duration = makeDurationInput("manual-time");

  // Checked once the title is typed (on leaving the box) and on Save.
  const dupGuard = duplicateGuard(() => titleInput.value, () => "");
  titleInput.addEventListener("change", () => dupGuard.preview());
  const form = el("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      if (!(await dupGuard.allowSave())) return;
      setBusy(true, tx("Saving recipe..."));
      let storedFile = null;
      try {
        if (imageInput.files.length) {
          const fd = new FormData(); fd.append("file", imageInput.files[0]);
          const upRes = await fetch(`${API}/upload-image`, { method: "POST", body: fd });
          if (!upRes.ok) {
            // Used to save the recipe without the photo and say nothing.
            const why = await errorDetail(upRes, `error ${upRes.status}`);
            announceError(tx("The photo couldn't be uploaded ({1}). Nothing was saved; remove the photo or try another.", { 1: why }));
            return;
          }
          storedFile = (await upRes.json()).stored_file;
        }

        const tagObjs = tagsFromInput(tagsInput.value);

        const payload = {
          title: titleInput.value.trim() || tx("Untitled Recipe"),
          source_type: "manual",
          servings: servingsInput.value.trim() || null,
          image_path: storedFile,
          actual_cook_time: duration.getValue(),
          ingredients: ingredientsOf(ingredientsArea),
          steps: linesOf(stepsArea),
          tags: tagObjs,
        };

        const res = await fetch(`${API}/recipes`, jsonBody("POST", payload));
        if (!res.ok) throw new Error(tx("Could not save recipe."));
        closeModal(addOverlay);
        announce(tx("Recipe saved."));
        await refreshLibrary();
      } catch (err) {
        if (storedFile) discardDraftFile(storedFile);
        alert(err.message);
      } finally { setBusy(false); }
    },
  }, [
    el("div", { class: "field" }, [el("label", { for: "manual-title", text: tx("Title") }), titleInput]),
    el("div", { class: "field" }, [el("label", { for: "manual-servings", text: tx("Servings (optional)") }), servingsInput]),
    el("div", { class: "field" }, [el("label", { for: "manual-ingredients", text: tx("Ingredients (one per line)") }), ingredientsArea, partHint(ingredientsArea)]),
    el("div", { class: "field" }, [el("label", { for: "manual-steps", text: tx("Steps (one per line)") }), stepsArea, partHint(stepsArea)]),
    el("div", { class: "field" }, [el("label", { for: "manual-tags", text: tx("Tags (comma or semicolon separated)") }), tagsInput]),
    el("div", { class: "field" }, [
      el("label", { text: tx("Real-world cook time (optional)") }),
      duration.element,
    ]),
    el("div", { class: "field" }, [
      el("label", { for: "manual-image", text: tx("Attach a photo of the card (optional)") }),
      imageInput,
    ]),
    dupGuard.box,
    el("button", { class: "btn-primary", type: "submit", text: tx("Save recipe") }),
  ]);
  addBody.appendChild(form);
}

// The Add button, and closing the dialog (a draft's uploads are discarded).
export function initAddRecipe() {
  $("#add-recipe-fab").addEventListener("click", () => { state.currentDraftFiles.clear(); showSourcePicker(); openModal(addOverlay); });
  $("#add-recipe-close").addEventListener("click", async () => { await discardCurrentDraftFiles(); closeModal(addOverlay); });
  addOverlay.addEventListener("click", async (e) => { if (e.target === addOverlay) { await discardCurrentDraftFiles(); closeModal(addOverlay); } });
}

export { addOverlay, discardCurrentDraftFiles, errorDetail, openSharedLink };
// Used by settings.js too.
export { savedResultRow, renderBatchResults, modeSwitchControl };
