// Loaded as a JavaScript module (index.html, type="module"; 0187): its
// own scope and strict mode, as the function wrapped around it gave it.
import {
  initBackupPanel, initLockedPanels, initPreferencePanels,
  ensureUnlocked, adminFetch, refreshHttpsSummary, refreshPickerLink,
} from "./settings.js";
import { initAddRecipe, addOverlay, discardCurrentDraftFiles, errorDetail, openSharedLink } from "./add_recipe.js";
const API = "/api";

// Interface language helpers (js/i18n.js). Static page text is
// translated first, before anything is rendered from script.
const { lang: LANG, setLang, tx, txn, tagLabel, groupLabel, valueLabel, fmtDateTime, durationLabel, translateStatic } = window.OTP_I18N;
const { isPartName } = window.OTP_AMOUNTS;   // amounts.js
translateStatic(document.body);

// Every state-changing request to the API must carry X-Requested-With;
// the server refuses writes without it (cross_site_guard in main.py), which
// stops other websites from making changes through your browser. Added
// here once, for every fetch the app makes to its own API.
const nativeFetch = window.fetch.bind(window);
// X-App-Lang tells the server which language to answer in (error
// messages, exports); it goes on every request to our own origin.
window.fetch = (input, init = {}) => {
  const url = typeof input === "string" ? input : input.url;
  const method = (init.method || (typeof input !== "string" && input.method) || "GET").toUpperCase();
  if (new URL(url, location.href).origin === location.origin) {
    const headers = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined));
    headers.set("X-App-Lang", LANG);
    if (method !== "GET" && method !== "HEAD") headers.set("X-Requested-With", "OpenThePantry");
    init = { ...init, headers };
  }
  return nativeFetch(input, init);
};

// Uncaught errors and rejected promises go to the server log
// (docker logs / data/logs/app.log, as "otp.client"), so a problem seen
// on the phone can be read on the NAS. Best effort: a report that can't
// be sent is dropped, and at most 10 are sent per page load.
let clientErrorsSent = 0;
function reportClientError(message, source, line, column, error) {
  if (clientErrorsSent >= 10) return;
  // "Script error." with no file or line is all a browser reports for
  // an error in a script from somewhere else (an extension, a content
  // blocker); there's nothing in it to act on.
  if (message === "Script error." && !source && !error) return;
  clientErrorsSent += 1;
  try {
    const body = JSON.stringify({
      message: String(message || "").slice(0, 1000),
      source: String(source || "").slice(0, 300),
      line: Number.isFinite(line) ? line : null,
      column: Number.isFinite(column) ? column : null,
      stack: String((error && error.stack) || "").slice(0, 4000),
      page: location.pathname + location.hash,
      user_agent: navigator.userAgent.slice(0, 300),
    });
    fetch(`${API}/client-error`, { method: "POST", headers: { "Content-Type": "application/json" }, body, keepalive: true })
      .catch(() => {});
  } catch { /* never let error reporting throw */ }
}
window.addEventListener("error", (e) => reportClientError(e.message, e.filename, e.lineno, e.colno, e.error));
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  reportClientError(r && r.message ? `Unhandled promise rejection: ${r.message}` : `Unhandled promise rejection: ${String(r)}`,
    "", null, null, r);
});

// -------------------------------------------------------------------
// Utilities
// -------------------------------------------------------------------
// Function declarations, not arrow functions in constants: settings.js
// looks its elements up as it loads, which is before this module's own
// code runs (0188), and only a declaration is ready by then.
function $(sel, root = document) { return root.querySelector(sel); }
function $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

// Pinch zoom on phones and tablets: Safari has ignored user-scalable=no
// since iOS 10, so the gesture is cancelled here as well; Settings -> Text
// size replaces it. Only on touch screens: Safari on a Mac sends the same
// event for a trackpad pinch, and there zooming must keep working.
if (window.matchMedia && window.matchMedia("(pointer: coarse)").matches) {
  document.addEventListener("gesturestart", (e) => e.preventDefault(), { passive: false });
}

function announce(msg) {
  $("#status-region").textContent = msg;
}

// announce() only reaches screen readers (#status-region sits at
// left: -9999px). A failed save, rating, notes or photo update, or a
// failed delete, used to leave the screen showing nothing at all --
// announceError() also puts the message in a visible toast, so a
// sighted user finds out too. Kept separate from announce() rather than
// making that one function visible, since most of its callers are
// routine status ("3 recipes found") that shouldn't pop up a toast.
let toastTimer = null;
function announceError(msg) {
  announce(msg);
  let region = $("#toast-region");
  if (!region) {
    region = el("div", { id: "toast-region", class: "toast-region" });
    document.body.appendChild(region);
  }
  region.innerHTML = "";
  // No role="alert": announce() above already reaches screen readers, and
  // both together read every error out twice.
  region.appendChild(el("div", { class: "toast toast-error" }, [
    el("span", { text: msg }),
    el("button", { class: "toast-dismiss", type: "button", "aria-label": tx("Dismiss"), text: "×", onclick: () => dismissToast() }),
  ]));
  clearTimeout(toastTimer);
  toastTimer = setTimeout(dismissToast, 8000);
}
function dismissToast() {
  clearTimeout(toastTimer);
  const region = $("#toast-region");
  if (region) region.innerHTML = "";
}

// Notes icon for recipe cards (a page with lines). Fixed markup.
const NOTE_ICON_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="none" stroke="currentColor" ' +
  'stroke-width="1.8" stroke-linejoin="round" d="M6 3h9l4 4v14H6z M14 3v5h5 M9 12h7 M9 15.5h7 M9 19h4"/></svg>';

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    // false is left out like null: setAttribute would write "false",
    // which for hidden or disabled still means on (DeepSeek review of 0180).
    if (v === null || v === undefined || v === false) continue;
    if (k === "text") node.textContent = v;
    // Only for fixed markup written in this file (the share icon). Named
    // so it can't be mistaken for a general option: recipe data must go
    // through `text`, never here.
    else if (k === "trustedStaticHtml") node.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) if (c) node.appendChild(c);
  return node;
}

// Modals can stack (Email PDF opens over a recipe), so the focus to go
// back to is kept per modal, and Escape and Tab act on the top one: the
// last open overlay in the page, which is also the one drawn on top.
const focusBeforeModal = new Map();
const topOverlay = () => $$(".modal-overlay").filter((o) => !o.hidden).pop();
function openModal(overlay) {
  focusBeforeModal.set(overlay, document.activeElement);
  overlay.hidden = false;
  const focusable = overlay.querySelector("button, input, textarea, select, a[href]");
  (focusable || overlay).focus();
  document.addEventListener("keydown", trapFocus);
}
function closeModal(overlay) {
  overlay.hidden = true;
  // Every way out of the recipe (X, Escape, backdrop, delete) clears an
  // #recipe-<id> it was opened from, so a reload doesn't reopen it.
  if (overlay.id === "recipe-detail-overlay") clearRecipeHash();
  if (overlay._onClose) { const done = overlay._onClose; overlay._onClose = null; done(); }
  if (!topOverlay()) document.removeEventListener("keydown", trapFocus);
  const back = focusBeforeModal.get(overlay);
  focusBeforeModal.delete(overlay);
  if (back && document.contains(back)) back.focus();
}
function trapFocus(e) {
  if (e.key === "Escape") {
    const openOverlay = topOverlay();
    if (openOverlay === addOverlay) discardCurrentDraftFiles();
    // Escape is a third way out of the recipe detail -- release the
    // wake lock here too, or the screen stays on after dismissal.
    if (openOverlay === detailOverlay) { closeRecipeDetail(); return; }   // also releases the wake lock
    if (openOverlay) closeModal(openOverlay);
    return;
  }
  if (e.key !== "Tab") return;
  const openOverlay = topOverlay();
  if (!openOverlay) return;
  const focusables = $$('button, input, textarea, select, a[href]', openOverlay)
    .filter((n) => !n.disabled && n.offsetParent !== null);
  if (!focusables.length) return;
  const first = focusables[0], last = focusables[focusables.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

// localStorage throws instead of returning null when storage is blocked
// (some private-browsing modes, site data turned off). A preference that
// can't be read or saved just lasts for this page; it must never stop
// the app from starting.
function storageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); } catch { /* lasts this page only */ }
}

// -------------------------------------------------------------------
// Text size control (persisted; scales the whole app via rem units)
// -------------------------------------------------------------------
// 24 and 26 are for laptops and desktops read from across the kitchen.
const SIZE_STEPS = [14, 16, 18, 20, 22, 24, 26];
function getSizeIndex() {
  const stored = parseInt(storageGet("recipe-app-font-size-idx"), 10);
  return Number.isInteger(stored) && stored >= 0 && stored < SIZE_STEPS.length ? stored : 1;
}
function applySizeIndex(idx) {
  idx = Math.max(0, Math.min(SIZE_STEPS.length - 1, idx));
  document.documentElement.style.setProperty("--base-font-size", SIZE_STEPS[idx] + "px");
  storageSet("recipe-app-font-size-idx", String(idx));
}
applySizeIndex(getSizeIndex());
$("#text-size-decrease").addEventListener("click", () => applySizeIndex(getSizeIndex() - 1));
$("#text-size-increase").addEventListener("click", () => applySizeIndex(getSizeIndex() + 1));

// -------------------------------------------------------------------
// Theme: light / dark / system, dark mode is true black (#000), not a
// gray substitute -- applied synchronously at script load (before the
// rest of init) so there's no flash of the wrong theme.
// -------------------------------------------------------------------
const THEME_META_COLOR = { light: "#C48A2E", dark: "#000000" };
function getThemeChoice() {
  const stored = storageGet("recipe-app-theme");
  return ["light", "dark", "system"].includes(stored) ? stored : "system";
}
function systemPrefersDark() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}
function applyTheme(choice) {
  if (choice === "system") {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", choice);
  }
  storageSet("recipe-app-theme", choice);
  const effective = choice === "system" ? (systemPrefersDark() ? "dark" : "light") : choice;
  const metaTheme = document.querySelector('meta[name="theme-color"]');
  if (metaTheme) metaTheme.setAttribute("content", THEME_META_COLOR[effective]);
  $$("#theme-switch button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeChoice === choice)));
}
applyTheme(getThemeChoice());
if (window.matchMedia) {
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (getThemeChoice() === "system") applyTheme("system");
  });
}
$$("#theme-switch button").forEach((btn) => {
  btn.addEventListener("click", () => applyTheme(btn.dataset.themeChoice));
});

// Language: stored per device; changing it reloads the page so every
// string, including ones already rendered, comes up in the new language.
$$("#language-switch button").forEach((btn) => {
  btn.setAttribute("aria-pressed", String(btn.dataset.langChoice === LANG));
  btn.addEventListener("click", () => {
    if (btn.dataset.langChoice === LANG) return;
    setLang(btn.dataset.langChoice);
    location.reload();
  });
});

// A tag typed in an edit form, matched back to a stored tag by its stored
// name or by the label shown for it ("Poulet" finds Chicken).
function findTagByLabel(name) {
  const want = name.trim().toLowerCase();
  return state.allTags.find((t) => t.name.toLowerCase() === want)
    || state.allTags.find((t) => tagLabel(t).toLowerCase() === want)
    || null;
}

// The tags typed in a form's box (commas or semicolons between them):
// the stored tag each matches, else a new custom one. suggested: the
// Suggest button's picks, by lower-case name, which win.
function tagsFromInput(value, suggested = null) {
  return value.split(/[,;]/).map((s) => s.trim()).filter(Boolean).map((name) => {
    const sug = suggested && suggested.get(name.toLowerCase());
    if (sug) return { name: sug.name, category: sug.category, subgroup: sug.subgroup || null };
    const known = findTagByLabel(name);
    return known
      ? { name: known.name, category: known.category, subgroup: known.subgroup }
      : { name, category: "custom", subgroup: null };
  });
}

// A form's ingredients or steps box, one per non-blank line.
function linesOf(area) {
  return area.value.split("\n").filter((l) => l.trim());
}
const ingredientsOf = (area) => linesOf(area).map((line) => ({ raw_line: line, name: line }));

// A JSON request's init: fetch(url, jsonBody("POST", payload)).
function jsonBody(method, body) {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

// A file uploaded for a draft, removed from the server once it isn't
// wanted. Best effort: one left behind is swept with the drafts folder.
function discardDraftFile(name) {
  return fetch(`${API}/ingest/draft/${encodeURIComponent(name)}`, { method: "DELETE" }).catch(() => {});
}

// After recipes were added or changed in bulk: the tag list, the time
// filters and the list itself.
async function refreshLibrary() {
  await loadTags(); await loadTimeBuckets(); loadRecipes();
}

// -------------------------------------------------------------------
// Settings modal
// -------------------------------------------------------------------
const settingsOverlay = $("#settings-overlay");

// -------------------------------------------------------------------
// Screen wake lock -- keeps the display on while reading a recipe
// (hands are busy / covered in flour). Two controls, per the same
// preference: a global default in Settings, and a per-recipe toggle
// in the detail view that can override it either way for one session.
//
// The API has two sharp edges this handles:
//   1. The browser RELEASES the lock automatically whenever the tab is
//      backgrounded or the screen locks, and does NOT restore it. So a
//      visibilitychange handler re-acquires on return -- otherwise it
//      silently stops working the first time you switch apps.
//   2. Browsers only offer it on a secure page (https:// or localhost).
//      Opened as http://<NAS address>:8090 the API isn't there, so the
//      Settings switch and the per-recipe switch are hidden entirely
//      (Settings -> HTTPS gives the app a secure address).
//      (A silent-video workaround was tried for http://; it didn't keep
//      an iPhone awake, so it was removed rather than left half-working.)
// -------------------------------------------------------------------
const wakeLockSupported = "wakeLock" in navigator && window.isSecureContext !== false;
let wakeLockSentinel = null;
let wakeLockWanted = false;  // whether we *want* a lock right now (a recipe is open with it enabled)
const wakeLockListeners = new Set();  // UI callbacks notified when the actual state changes

function getWakeLockDefault() {
  return storageGet("recipe-app-wakelock-default") === "true";
}
function setWakeLockDefault(on) {
  storageSet("recipe-app-wakelock-default", String(on));
}
function wakeLockActive() {
  return wakeLockSentinel !== null;
}
function notifyWakeLockListeners() {
  wakeLockListeners.forEach((fn) => { try { fn(); } catch { /* a bad listener shouldn't break the rest */ } });
}

async function acquireWakeLock() {
  if (!wakeLockSupported || wakeLockSentinel) return;
  try {
    wakeLockSentinel = await navigator.wakeLock.request("screen");
    // Fires on both our own release() and the browser's automatic one.
    wakeLockSentinel.addEventListener("release", () => {
      wakeLockSentinel = null;
      notifyWakeLockListeners();
    });
  } catch {
    // Denied (low battery, OS policy, or a non-visible document).
    // Nothing to do but reflect the real state in the UI.
    wakeLockSentinel = null;
  }
  notifyWakeLockListeners();
}

async function releaseWakeLock() {
  wakeLockWanted = false;
  if (wakeLockSentinel) {
    try { await wakeLockSentinel.release(); } catch { /* already gone */ }
    wakeLockSentinel = null;
  }
  notifyWakeLockListeners();
}

async function setWakeLockWanted(on) {
  wakeLockWanted = on;
  if (on) await acquireWakeLock();
  else await releaseWakeLock();
}

if (wakeLockSupported) {
  $("#wakelock-setting").hidden = false;
  document.addEventListener("visibilitychange", () => {
    // Re-acquire on return to the tab, but only if a recipe is still
    // open with the lock enabled.
    if (document.visibilityState === "visible" && wakeLockWanted && !wakeLockActive()) {
      acquireWakeLock();
    }
  });

  const wakeLockDefaultInput = $("#wakelock-default");
  wakeLockDefaultInput.checked = getWakeLockDefault();
  wakeLockDefaultInput.addEventListener("change", async () => {
    setWakeLockDefault(wakeLockDefaultInput.checked);
    // Apply immediately if a recipe is already open.
    if (!detailOverlay.hidden) await setWakeLockWanted(wakeLockDefaultInput.checked);
  });
}


// Settings -> Backup and Restore (settings.js).
initBackupPanel();

const shareSheetOverlay = $("#share-sheet-overlay");
$("#share-sheet-close").addEventListener("click", () => closeModal(shareSheetOverlay));
shareSheetOverlay.addEventListener("click", (e) => { if (e.target === shareSheetOverlay) closeModal(shareSheetOverlay); });

const emailSheetOverlay = $("#email-sheet-overlay");
$("#email-sheet-close").addEventListener("click", () => closeModal(emailSheetOverlay));
emailSheetOverlay.addEventListener("click", (e) => { if (e.target === emailSheetOverlay) closeModal(emailSheetOverlay); });

const ratingSheetOverlay = $("#rating-sheet-overlay");
$("#rating-sheet-close").addEventListener("click", () => closeModal(ratingSheetOverlay));
ratingSheetOverlay.addEventListener("click", (e) => { if (e.target === ratingSheetOverlay) closeModal(ratingSheetOverlay); });

// Settings -> settings password, email, HTTPS, Logs (settings.js).
initLockedPanels();

// -------------------------------------------------------------------
// Share -> Email PDF
// -------------------------------------------------------------------
const MAX_EMAIL_RECIPIENTS = 5;

function setMailState(settings) {
  state.mail = {
    canSend: Boolean(settings.can_send),
    from: settings.username || "",
    recent: settings.recent_recipients || [],
  };
}

// At start-up only whether sending is set up (open, no password): enough
// to offer Share -> Email PDF. The From address and recent recipients
// come from the email settings, read once the password has been entered.
async function loadMailState() {
  try {
    const res = await fetch(`${API}/email-status`);
    if (res.ok) state.mail.canSend = Boolean((await res.json()).can_send);
  } catch { /* offline: the Email PDF action just stays hidden */ }
}
async function loadMailDetails() {
  const res = await adminFetch(`${API}/email-settings`);
  if (!res.ok) return false;
  setMailState(await res.json());
  return true;
}

async function saveRecentRecipients(list) {
  const res = await adminFetch(`${API}/email-settings/recipients`, jsonBody("PUT", { recipients: list }));
  if (!res.ok) {
    throw new Error(await errorDetail(res, tx("Could not save.")));
  }
  state.mail.recent = (await res.json()).recipients;
}

// Settings -> Email ingest: the addresses offered in the Email PDF form.
function recentRecipientsEditor() {
  const wrap = el("div", { class: "recipients-editor" });
  const status = el("div", { class: "field-hint", role: "status" });
  const render = () => {
    wrap.innerHTML = "";
    wrap.appendChild(el("div", { class: "filter-section-title", text: tx("Recent recipients") }));
    wrap.appendChild(el("div", { class: "field-hint", text: tx("Offered when you email a recipe from Share. Addresses are added here each time you send.") }));
    const list = el("ul", { class: "recipients-list" });
    for (const addr of state.mail.recent) {
      list.appendChild(el("li", {}, [
        el("span", { text: addr }),
        el("button", {
          type: "button", class: "btn-secondary btn-small", text: tx("Remove"),
          "aria-label": tx("Remove {1}", { 1: addr }),
          onclick: async () => {
            try {
              await saveRecentRecipients(state.mail.recent.filter((a) => a !== addr));
              announce(tx("Removed {1}.", { 1: addr }));
              render();
            } catch (err) { status.textContent = err.message; }
          },
        }),
      ]));
    }
    if (!state.mail.recent.length) list.appendChild(el("li", { class: "field-hint", text: tx("None yet.") }));
    wrap.appendChild(list);
    const input = el("input", { type: "email", placeholder: tx("name@example.com"), "aria-label": tx("Add an address"), autocomplete: "email" });
    const add = el("button", {
      type: "button", class: "btn-secondary", text: tx("Add"),
      onclick: async () => {
        const addr = input.value.trim();
        if (!addr) return;
        try {
          await saveRecentRecipients([addr, ...state.mail.recent]);
          announce(tx("Added {1}.", { 1: addr }));
          render();
        } catch (err) { status.textContent = err.message; }
      },
    });
    wrap.appendChild(el("div", { class: "recipients-add" }, [input, add]));
    wrap.appendChild(status);
  };
  render();
  return wrap;
}

function splitAddresses(text) {
  return text.split(/[,;\s]+/).map((a) => a.trim()).filter(Boolean);
}

// The Contact Picker API: Chrome on Android. Elsewhere the button is left
// out and the recent-recipient list is the shortcut.
const contactPickerAvailable = () =>
  "contacts" in navigator && "ContactsManager" in window && typeof navigator.contacts.select === "function";

function openEmailSheet(recipe) {
  const overlay = emailSheetOverlay;
  $("#email-sheet-title").textContent = recipe.title;
  const body = $("#email-sheet-body");
  body.innerHTML = "";

  const status = el("div", { class: "field-hint email-sheet-status", role: "status" });
  const toInput = el("input", {
    type: "email", id: "email-sheet-to", multiple: "", autocomplete: "email", inputmode: "email",
    placeholder: tx("name@example.com, …"),
  });
  const addAddress = (addr) => {
    const current = splitAddresses(toInput.value);
    if (current.some((a) => a.toLowerCase() === addr.toLowerCase())) return;
    toInput.value = [...current, addr].join(", ");
  };

  body.appendChild(el("div", { class: "field" }, [
    el("label", { for: "email-sheet-from", text: tx("From") }),
    el("input", { type: "text", id: "email-sheet-from", value: state.mail.from, readonly: "", tabindex: "-1" }),
  ]));
  body.appendChild(el("div", { class: "field" }, [
    el("label", { for: "email-sheet-to", text: tx("To") }),
    toInput,
    el("div", { class: "field-hint", text: tx("Up to {1} addresses, separated by commas.", { 1: MAX_EMAIL_RECIPIENTS }) }),
  ]));

  if (contactPickerAvailable()) {
    body.appendChild(el("button", {
      type: "button", class: "btn-secondary email-contacts", text: tx("Choose from contacts"),
      onclick: async () => {
        try {
          const picked = await navigator.contacts.select(["email"], { multiple: true });
          for (const c of picked) if (c.email && c.email[0]) addAddress(c.email[0]);
        } catch { /* closed without choosing */ }
      },
    }));
  }

  if (state.mail.recent.length) {
    body.appendChild(el("div", { class: "email-recent" }, [
      el("div", { class: "field-hint", text: tx("Recent:") }),
      el("div", { class: "email-recent-list" }, state.mail.recent.slice(0, 8).map((addr) =>
        el("button", { type: "button", class: "chip", text: addr,
          "aria-label": tx("Add {1}", { 1: addr }), onclick: () => addAddress(addr) }))),
    ]));
  }

  const messageInput = el("textarea", { id: "email-sheet-message", rows: "3", maxlength: "2000" });
  body.appendChild(el("div", { class: "field" }, [
    el("label", { for: "email-sheet-message", text: tx("Message (optional)") }),
    messageInput,
  ]));

  let notesBox = null;
  if (recipe.notes) {
    notesBox = el("input", { type: "checkbox", id: "email-sheet-notes" });
    body.appendChild(el("label", { class: "checkbox-row", for: "email-sheet-notes" }, [
      notesBox, el("span", { text: tx("Include my notes") })]));
  }

  const sendBtn = el("button", {
    type: "button", class: "btn-primary", text: tx("Send"),
    onclick: async () => {
      const to = splitAddresses(toInput.value);
      if (!to.length) { status.textContent = tx("Add at least one recipient."); toInput.focus(); return; }
      if (to.length > MAX_EMAIL_RECIPIENTS) {
        status.textContent = tx("Up to {1} addresses, separated by commas.", { 1: MAX_EMAIL_RECIPIENTS });
        return;
      }
      sendBtn.disabled = true;
      status.textContent = tx("Sending…");
      try {
        const res = await adminFetch(`${API}/recipes/${recipe.id}/email`, jsonBody("POST", { to, message: messageInput.value, include_notes: Boolean(notesBox && notesBox.checked) }));
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          // The full reason (often a long SMTP diagnosis) goes in the
          // toast; the form keeps a short line so it stays usable.
          const msg = typeof data.detail === "string" ? data.detail : tx("The email wasn't sent (error {1}).", { 1: res.status });
          status.textContent = tx("The email wasn't sent (error {1}).", { 1: res.status });
          announceError(msg);
          return;
        }
        state.mail.recent = data.recent_recipients || state.mail.recent;
        closeModal(overlay);
        announce(tx("Sent to {1}.", { 1: data.sent_to.join(", ") }));
      } catch {
        const msg = tx("Couldn't reach Open the Pantry. Check your connection and try again.");
        status.textContent = msg;
        announceError(msg);
      } finally {
        sendBtn.disabled = false;
      }
    },
  });
  body.appendChild(el("div", { class: "email-sheet-actions" }, [
    el("button", { type: "button", class: "btn-secondary", text: tx("Cancel"), onclick: () => closeModal(overlay) }),
    sendBtn,
  ]));
  body.appendChild(status);
  openModal(overlay);
  toInput.focus();
}

// -------------------------------------------------------------------
// Sidebar toggle
// -------------------------------------------------------------------
const sidebar = $("#sidebar");
const sidebarToggle = $("#sidebar-toggle");

// 720px matches the media query that turns the sidebar into an overlay
// drawer. Above it the sidebar is a permanent column, and the
// dismiss-on-outside-click behaviour below must NOT apply -- closing the
// navigation because someone clicked a recipe would be absurd.
const DRAWER_MAX_WIDTH = 720;
const isDrawer = () => window.innerWidth <= DRAWER_MAX_WIDTH;
const sidebarOpen = () => sidebar.getAttribute("data-collapsed") !== "true";

function setSidebar(open) {
  sidebar.setAttribute("data-collapsed", String(!open));
  sidebarToggle.setAttribute("aria-expanded", String(open));
}

sidebarToggle.addEventListener("click", () => setSidebar(!sidebarOpen()));

// Tapping anywhere outside the open drawer closes it. Previously the only
// way out was a second tap on the hamburger, which is not where your
// thumb is after you have just picked a filter.
//
// This listens on the CAPTURE phase, which matters: renderSidebar() tears
// down and rebuilds the whole tag tree inside its own click handler, so by
// the time a bubbled event reached document the clicked node had already
// been detached and sidebar.contains(target) was false. Every tap on a
// category header therefore closed the drawer. Capturing runs before the
// target's own handler, while the node is still in the tree.
document.addEventListener("click", (e) => {
  if (!isDrawer() || !sidebarOpen()) return;
  const t = e.target;
  if (sidebar.contains(t) || sidebarToggle.contains(t)) return;
  // A modal sits above the drawer; clicks in one are not "outside" in any
  // sense the user means.
  if (t.closest && t.closest(".modal-overlay")) return;
  setSidebar(false);
}, true);

// Escape closes it too, and focus goes back to the control that opened it
// so keyboard users aren't stranded. Modals handle their own Escape in
// trapFocus(); this only fires when none of them are open.
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || !isDrawer() || !sidebarOpen()) return;
  if ($$(".modal-overlay").some((o) => !o.hidden)) return;
  setSidebar(false);
  sidebarToggle.focus();
});

if (isDrawer()) setSidebar(false);

// -------------------------------------------------------------------
// State
// -------------------------------------------------------------------
const state = {
  query: "",
  filterTags: new Set(),   // stacked AND tag filters -- shared by sidebar and filter panel
  maxMinutes: null,        // time filter ceiling (minutes), null = no limit
  pace: new Set(),         // pace-rating filter: 'quick' | 'moderate' | 'long', OR logic
  toReview: false,         // only recipes saved without review (batch, email)
  reviewCount: 0,          // how many there are (sidebar)
  binCount: 0,             // recipes in Recently deleted (sidebar)
  grouping: "all",         // 'all' | 'meal_type' | 'cooking_style' | 'main_ingredient'
  allTags: [],
  timeBuckets: [],
  selectMode: false,
  selectedIds: new Set(),
  currentDraftFiles: new Set(),  // temp file(s) awaiting save/discard for the open add-recipe session
  // "titles", "photos" (the default) or "tiles" (owner, 2026-11-18). Before
  // tiles it was an on/off switch, recipe-app-show-thumbnails; its "false"
  // still means titles until a style is chosen.
  listStyle: ["titles", "photos", "tiles"].includes(storageGet("recipe-app-list-style"))
    ? storageGet("recipe-app-list-style")
    : (storageGet("recipe-app-show-thumbnails") === "false" ? "titles" : "photos"),
  showTicks: storageGet("recipe-app-checkboxes") === "true",             // default off
  // Which sidebar category groups are open. Default is ALL CLOSED: fully
  // expanded, the tree runs well past a phone screen. This has to live in
  // state rather than the DOM because renderSidebar() tears the tree down
  // and rebuilds it on every tag click -- keeping it in the markup would
  // re-open every group the moment you picked a filter.
  expandedGroups: loadExpandedGroups(),
  // Sort is persisted: it is a standing preference about how you like to
  // read your library, not a per-visit choice.
  sort: storageGet("recipe-app-sort") || "created",
  direction: storageGet("recipe-app-sort-dir") || "desc",
  // Share -> Email PDF: whether sending is set up (read at start-up), the
  // From address and recent recipients (read once unlocked).
  mail: { canSend: false, from: "", recent: [] },
};
let uiTimeLevel1 = null;   // UI-only: which coarse hour bucket is expanded in the time filter

// -------------------------------------------------------------------
// Clearing filters
//
// One function, three entry points (toolbar, filter panel, sidebar) so
// they cannot drift apart. It clears the SEARCH BOX as well as the tag
// and time filters: all three narrow the list, they combine, and a
// forgotten search term is exactly the one people don't think to look
// for when the list seems wrong.
// -------------------------------------------------------------------
function activeFilterCount() {
  return state.filterTags.size
    + (state.maxMinutes != null ? 1 : 0)
    + (state.pace.size ? 1 : 0)
    + (state.toReview ? 1 : 0)
    + (state.query ? 1 : 0);
}

function clearAllFilters() {
  state.filterTags.clear();
  state.maxMinutes = null;
  uiTimeLevel1 = null;
  state.pace.clear();
  state.toReview = false;
  state.query = "";
  const searchInput = $("#search-input");
  if (searchInput) searchInput.value = "";
  syncSearchClear();
  renderSidebar();
  if (!$("#filter-panel").hidden) renderFilterPanel();
  syncClearFiltersButton();
  loadRecipes();
  announce(tx("Filters cleared."));
}

// The toolbar button carries the count and hides itself when nothing is
// active -- a permanently visible "Clear filters" that usually does
// nothing is just noise.
function syncClearFiltersButton() {
  const btn = $("#clear-filters-btn");
  if (!btn) return;
  const n = activeFilterCount();
  btn.hidden = n === 0;
  btn.textContent = tx("Clear filters ({1})", { 1: n });
}

// -------------------------------------------------------------------
// Tag sidebar
// -------------------------------------------------------------------

// Persisted set of open group keys. A missing/corrupt entry means "none
// open", which is the intended first-run state.
function loadExpandedGroups() {
  try {
    const raw = localStorage.getItem("recipe-app-expanded-groups");
    if (!raw) return new Set();
    const arr = JSON.parse(raw);
    return new Set(Array.isArray(arr) ? arr : []);
  } catch { return new Set(); }
}

function saveExpandedGroups() {
  try {
    localStorage.setItem("recipe-app-expanded-groups", JSON.stringify([...state.expandedGroups]));
  } catch { /* private mode / storage disabled -- collapsing still works for this session */ }
}

function toggleGroup(key) {
  if (state.expandedGroups.has(key)) state.expandedGroups.delete(key);
  else state.expandedGroups.add(key);
  saveExpandedGroups();
  renderSidebar();
}

// A collapsible section: a header button that owns its own body. The body
// is a sibling rather than a child so the header stays a single, simple
// hit target.
function collapsibleSection(key, label, opts, children) {
  const forced = opts.forceOpen;
  const open = forced || state.expandedGroups.has(key);
  const body = el("div", { class: "tag-group-body", id: `group-body-${key}` }, children);
  if (!open) body.hidden = true;

  const header = el("button", {
    type: "button",
    class: opts.sub ? "tag-group-toggle tag-group-toggle-sub" : "tag-group-toggle",
    "aria-expanded": String(open),
    "aria-controls": `group-body-${key}`,
    // Kept open while one of its filters is active: closing it would hide
    // a filter that's narrowing the list. The tap says so instead of
    // doing nothing.
    onclick: () => (forced ? announceError(tx("This group stays open while one of its filters is on.")) : toggleGroup(key)),
  }, [
    el("span", { class: "tag-group-chevron", "aria-hidden": "true", text: "›" }),
    el("span", { class: "tag-group-label", text: label }),
    // When a group is closed, its active filters are invisible. The count
    // keeps them discoverable so you can't forget a filter is narrowing
    // the list.
    opts.activeCount ? el("span", { class: "tag-group-count", text: String(opts.activeCount) }) : null,
  ]);
  if (forced) {
    header.setAttribute("data-forced-open", "true");
    header.setAttribute("aria-disabled", "true");
    header.title = tx("This group stays open while one of its filters is on.");
  }

  return el("div", { class: "tag-group" }, [header, body]);
}

// Tag groups (Meal Type, Cooking Style, Main Ingredient, Custom, and any
// added in Settings) come from the server; this list is only the
// fallback if that request fails.
const BUILTIN_GROUPS = [
  { key: "meal_type", label: tx("Meal Type"), builtin: true },
  { key: "cooking_style", label: tx("Cooking Style"), builtin: true },
  { key: "main_ingredient", label: tx("Main Ingredient"), builtin: true },
  { key: "custom", label: tx("Custom"), builtin: true },
];
function tagGroups() {
  return (state.tagGroups && state.tagGroups.length ? state.tagGroups : BUILTIN_GROUPS).map((g) => [g.key, groupLabel(g.key, g.label)]);
}

// A failure here (the NAS restarting, a dropped connection) leaves the
// tags as they were and lets the page carry on to the recipe list, which
// shows its own error with a retry. It used to stop the start-up there,
// with an empty page and no message.
async function loadTags() {
  try {
    const [res, gres] = await Promise.all([fetch(`${API}/tags`), fetch(`${API}/tag-groups`).catch(() => null)]);
    if (res.ok) {
      const tags = await res.json();
      if (Array.isArray(tags)) state.allTags = tags;
    }
    if (gres && gres.ok) state.tagGroups = await gres.json();
  } catch { /* offline: keep what we have */ }
  renderGroupingToggle();
  renderSidebar();
}

// "All" plus one button per group that has tags ("Custom" stays in the
// sidebar and filter panel only, as before).
function renderGroupingToggle() {
  const wrap = $(".grouping-toggle");
  if (!wrap) return;
  const groups = tagGroups().filter(([k]) => k !== "custom" && state.allTags.some((t) => t.category === k));
  if (state.grouping !== "all" && !groups.some(([k]) => k === state.grouping)) state.grouping = "all";
  wrap.innerHTML = "";
  for (const [key, label] of [["all", tx("All")], ...groups]) {
    wrap.appendChild(el("button", { type: "button", "data-group": key, "aria-pressed": String(state.grouping === key), text: label }));
  }
}

async function loadTimeBuckets() {
  try {
    const res = await fetch(`${API}/time-buckets`);
    if (res.ok) {
      const buckets = await res.json();
      if (Array.isArray(buckets)) state.timeBuckets = buckets;
    }
  } catch { /* offline: keep what we have */ }
}

function renderSidebar() {
  const container = $("#tag-tree");
  container.innerHTML = "";
  const categories = tagGroups();

  // Expand/collapse everything at once -- with all groups closed by
  // default this is the one control that makes the whole tree browsable
  // without hunting through headers.
  const present = categories
    .filter(([k]) => state.allTags.some((t) => t.category === k))
    .map(([k]) => k);
  const anyOpen = present.some((k) => state.expandedGroups.has(k));

  // Clear filters sits in its own bordered region ABOVE the tag tree, not
  // among the categories: in a list of tappable tag names an identically
  // shaped button is easy to hit by accident while scanning.
  if (activeFilterCount()) {
    container.appendChild(el("div", { class: "sidebar-clear-region" }, [
      el("button", {
        type: "button", class: "btn-secondary btn-clear-filters",
        text: tx("Clear filters ({1})", { 1: activeFilterCount() }),
        onclick: clearAllFilters,
      }),
    ]));
  }

  // Recipes saved without review (batch links, batch PDFs, email). Shown
  // only while there are some, or while the filter is on.
  if (state.reviewCount || state.toReview) {
    container.appendChild(el("div", { class: "sidebar-review-region" }, [
      el("button", {
        type: "button", class: "tag-node review-filter", "aria-pressed": String(state.toReview),
        text: tx("To review ({1})", { 1: state.reviewCount }),
        onclick: () => {
          state.toReview = !state.toReview;
          renderSidebar();
          syncClearFiltersButton();
          loadRecipes();
        },
      }),
    ]));
  }

  // Recently deleted: only while something is waiting there.
  if (state.binCount) {
    container.appendChild(el("div", { class: "sidebar-review-region" }, [
      el("button", {
        type: "button", class: "tag-node review-filter", "aria-haspopup": "dialog", "aria-controls": "bin-overlay",
        text: tx("Recently deleted ({1})", { 1: state.binCount }),
        onclick: openBin,
      }),
    ]));
  }

  container.appendChild(el("div", { class: "tag-tree-actions" }, [
    el("button", {
      type: "button", class: "tag-tree-expand-all",
      text: anyOpen ? tx("Collapse all") : tx("Expand all"),
      onclick: () => {
        if (anyOpen) state.expandedGroups.clear();
        else {
          present.forEach((k) => state.expandedGroups.add(k));
          // Sub-sections are keyed separately, so open them too.
          state.allTags.filter((t) => t.subgroup)
            .forEach((t) => state.expandedGroups.add(`${t.category}:${t.subgroup}`));
        }
        saveExpandedGroups();
        renderSidebar();
      },
    }),
  ]));

  for (const [catKey, catLabel] of categories) {
    const tagsInCat = state.allTags.filter((t) => t.category === catKey);
    if (!tagsInCat.length) continue;

    const activeCount = tagsInCat.filter((t) => state.filterTags.has(t.name)).length;
    const children = [];

    const mainTags = tagsInCat.filter((t) => !t.subgroup);
    for (const t of mainTags) children.push(makeTagButton(t));

    const subgroups = [...new Set(tagsInCat.filter((t) => t.subgroup).map((t) => t.subgroup))];
    for (const sg of subgroups) {
      const sgTags = tagsInCat.filter((t2) => t2.subgroup === sg);
      const sgKey = `${catKey}:${sg}`;
      const sgActive = sgTags.filter((t) => state.filterTags.has(t.name)).length;
      children.push(collapsibleSection(
        sgKey,
        sg === "cocktail" ? tx("Cocktail Prep") : sg,
        { sub: true, activeCount: sgActive, forceOpen: sgActive > 0 },
        sgTags.map(makeTagButton),
      ));
    }

    // A group holding an active filter is forced open regardless of the
    // saved state -- a filter you can't see is a filter you can't clear.
    container.appendChild(collapsibleSection(
      catKey, catLabel, { activeCount, forceOpen: activeCount > 0 }, children,
    ));
  }
}

function toggleTagFilter(tagName) {
  if (state.filterTags.has(tagName)) state.filterTags.delete(tagName);
  else state.filterTags.add(tagName);
  renderSidebar();
  if (!$("#filter-panel").hidden) renderFilterPanel();
  syncClearFiltersButton();
  loadRecipes();
}

function makeTagButton(tag) {
  const pressed = state.filterTags.has(tag.name);
  return el("button", {
    class: "tag-node",
    type: "button",
    "aria-pressed": String(pressed),
    text: tagLabel(tag),
    onclick: () => toggleTagFilter(tag.name),
  });
}

// -------------------------------------------------------------------
// Filter panel (All tab): stacked tag filters (AND) + nested time filter
// -------------------------------------------------------------------
function makeFilterChip(tag) {
  const pressed = state.filterTags.has(tag.name);
  return el("button", {
    class: "filter-chip", type: "button",
    "aria-pressed": String(pressed),
    text: tagLabel(tag),
    onclick: () => toggleTagFilter(tag.name),
  });
}

function renderFilterPanel() {
  const panel = $("#filter-panel");
  panel.innerHTML = "";

  panel.appendChild(el("div", { class: "filter-panel-header" }, [
    el("h2", { class: "filter-panel-title", id: "filter-panel-title", text: tx("Filters") }),
    el("button", { class: "filter-panel-close", type: "button", "aria-label": tx("Close filters"), text: "\u00d7", onclick: closeFilterPanel }),
  ]));

  const categories = tagGroups();
  for (const [catKey, catLabel] of categories) {
    const tagsInCat = state.allTags.filter((t) => t.category === catKey);
    if (!tagsInCat.length) continue;
    panel.appendChild(el("div", { class: "filter-section" }, [
      el("div", { class: "filter-section-title", text: catLabel }),
      el("div", { class: "filter-chip-row" }, tagsInCat.map(makeFilterChip)),
    ]));
  }

  // Pace: the quick / moderate / long rating set from the card or detail
  // view. Selecting more than one shows recipes with any of them.
  panel.appendChild(el("div", { class: "filter-section" }, [
    el("div", { class: "filter-section-title", text: tx("Pace") }),
    el("div", { class: "filter-chip-row" }, ["quick", "moderate", "long"].map((v) =>
      el("button", {
        class: "filter-chip", type: "button",
        "aria-pressed": String(state.pace.has(v)),
        text: valueLabel(v),
        onclick: () => {
          if (state.pace.has(v)) state.pace.delete(v); else state.pace.add(v);
          renderFilterPanel();
          syncClearFiltersButton();
          loadRecipes();
        },
      })
    )),
  ]));

  const timeSection = el("div", { class: "filter-section" });
  timeSection.appendChild(el("div", { class: "filter-section-title", text: tx("Cook Time") }));
  if (!state.timeBuckets.length) {
    timeSection.appendChild(el("div", { class: "field-hint", text: tx("No logged cook times yet \u2014 this filter appears once recipes have a real-world time logged.") }));
  } else {
    const hourBuckets = state.timeBuckets.filter((b) => b.minutes % 60 === 0);
    timeSection.appendChild(el("div", { class: "time-filter-row" }, hourBuckets.map((b) =>
      el("button", {
        class: "filter-chip", type: "button",
        "aria-pressed": String(uiTimeLevel1 === b.minutes),
        text: `\u2264 ${durationLabel(b.minutes)}`,
        onclick: () => {
          uiTimeLevel1 = uiTimeLevel1 === b.minutes ? null : b.minutes;
          state.maxMinutes = uiTimeLevel1;
          renderFilterPanel();
          syncClearFiltersButton();
          loadRecipes();
        },
      })
    )));

    if (uiTimeLevel1 != null) {
      const subOptions = state.timeBuckets.filter((b) => b.minutes <= uiTimeLevel1);
      timeSection.appendChild(el("div", { class: "time-filter-row sub" }, subOptions.map((b) =>
        el("button", {
          class: "filter-chip", type: "button",
          "aria-pressed": String(state.maxMinutes === b.minutes),
          text: `\u2264 ${durationLabel(b.minutes)}`,
          onclick: () => {
            state.maxMinutes = state.maxMinutes === b.minutes ? uiTimeLevel1 : b.minutes;
            renderFilterPanel();
            syncClearFiltersButton();
            loadRecipes();
          },
        })
      )));
    }
  }
  panel.appendChild(timeSection);

  // Footer: filters apply as they're tapped, so "Done" only closes the
  // panel -- but it's the obvious way out, where a thumb reaches after
  // scrolling through the chips.
  const bits = [];
  if (state.filterTags.size) bits.push(txn(state.filterTags.size, "{n} tag filter", "{n} tag filters"));
  if (state.pace.size) bits.push(tx("pace filter"));
  if (state.maxMinutes != null) bits.push(tx("time filter"));
  if (state.query) bits.push(tx("search"));
  // Done on the left: the + button covers the bottom-right corner.
  panel.appendChild(el("div", { class: "filter-panel-footer" }, [
    el("button", { type: "button", class: "btn-primary", text: tx("Done"), onclick: closeFilterPanel }),
    activeFilterCount()
      ? el("button", { type: "button", class: "btn-secondary", text: tx("Clear filters"), onclick: clearAllFilters })
      : null,
    el("span", { class: "filter-panel-summary", text: bits.length ? tx("{1} active", { 1: bits.join(" + ") }) : tx("No filters active") }),
  ]));
}

function closeFilterPanel() {
  const panel = $("#filter-panel");
  if (panel.hidden) return;
  panel.hidden = true;
  const toggle = $("#filters-toggle");
  toggle.setAttribute("aria-expanded", "false");
  toggle.focus();
}

// Escape closes the panel too, unless a dialog is open (it has its own).
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || $("#filter-panel").hidden) return;
  if ($$(".modal-overlay").some((o) => !o.hidden)) return;
  closeFilterPanel();
});

$("#filters-toggle").addEventListener("click", () => {
  const panel = $("#filter-panel");
  const willOpen = panel.hidden;
  panel.hidden = !willOpen;
  $("#filters-toggle").setAttribute("aria-expanded", String(willOpen));
  if (willOpen) renderFilterPanel();
});

// -------------------------------------------------------------------
// Search
// -------------------------------------------------------------------
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.query = $("#search-input").value.trim();
  syncClearFiltersButton();
  loadRecipes();
});

// The X shows while the box has text. It empties the box, puts the cursor
// back in it, and, if a search was active, shows the full list again.
// Escape in the box does the same: on its own the browser would empty the
// box and leave the list filtered.
function syncSearchClear() {
  $("#search-clear").hidden = $("#search-input").value === "";
}
function clearSearch() {
  const input = $("#search-input");
  input.value = "";
  syncSearchClear();
  input.focus();
  if (state.query) {
    state.query = "";
    syncClearFiltersButton();
    loadRecipes();
  }
}
$("#search-input").addEventListener("input", syncSearchClear);
$("#search-input").addEventListener("keydown", (e) => {
  if (e.key === "Escape" && e.target.value !== "") {
    e.preventDefault();
    clearSearch();
  }
});
$("#search-clear").addEventListener("click", clearSearch);
// Going back to the page (and a reload in Firefox) can put the old text
// back in the box without an input event.
syncSearchClear();
window.addEventListener("pageshow", syncSearchClear);

// -------------------------------------------------------------------
// Grouping toggle
// -------------------------------------------------------------------
// One listener on the container: the buttons are rebuilt when groups change.
$(".grouping-toggle").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-group]");
  if (!btn) return;
  state.grouping = btn.dataset.group;
  $$(".grouping-toggle button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
  const filtersToggle = $("#filters-toggle");
  filtersToggle.hidden = state.grouping !== "all";
  if (state.grouping !== "all") $("#filter-panel").hidden = true;
  renderRecipeList(currentRecipes);
});

// -------------------------------------------------------------------
// Select mode / batch delete
// -------------------------------------------------------------------
function updateBatchBar() {
  $("#batch-count").textContent = tx("{1} selected", { 1: state.selectedIds.size });
  const barVisible = state.selectMode && state.selectedIds.size > 0;
  $("#batch-bar").hidden = !barVisible;
  $("#batch-merge-btn").hidden = state.selectedIds.size !== 2;
  // The bar and the + button share the bottom edge; on a phone the bar is
  // wide enough to run underneath it. Adding a recipe mid-selection isn't
  // a thing anyone needs, so the + steps aside while the bar is up.
  $("#add-recipe-fab").hidden = barVisible;
}

function toggleSelect(id) {
  if (state.selectedIds.has(id)) state.selectedIds.delete(id);
  else state.selectedIds.add(id);
  updateBatchBar();
  renderRecipeList(currentRecipes);
}

$("#clear-filters-btn").addEventListener("click", clearAllFilters);

// Sort. Field and direction travel together in one select rather than a
// field picker plus an asc/desc toggle: with only five fields, spelling
// out both directions ("Rating: high to low") is less to think about than
// a separate arrow whose meaning changes per field.
const sortSelect = $("#sort-select");
sortSelect.value = `${state.sort}|${state.direction}`;
if (!sortSelect.value) {            // stored value no longer offered
  sortSelect.value = "created|desc";
  state.sort = "created"; state.direction = "desc";
}
sortSelect.addEventListener("change", () => {
  state.bestMatch = sortSelect.value === "relevance|desc";
  if (state.bestMatch) { loadRecipes(); return; }
  const [sort, direction] = sortSelect.value.split("|");
  state.sort = sort;
  state.direction = direction;
  try {
    localStorage.setItem("recipe-app-sort", sort);
    localStorage.setItem("recipe-app-sort-dir", direction);
  } catch { /* private mode -- the choice still applies for this session */ }
  loadRecipes();
});

$("#select-mode-toggle").addEventListener("click", () => {
  state.selectMode = !state.selectMode;
  state.selectedIds.clear();
  $("#select-mode-toggle").setAttribute("aria-pressed", String(state.selectMode));
  updateBatchBar();
  renderRecipeList(currentRecipes);
});

// Settings -> Library check (GET /api/library-check): what's missing,
// and recipes that look the same. A title opens the recipe; the results
// stay for when Settings is opened again.
const LIBRARY_CHECKS = [
  ["no_ingredients", "Without ingredients"], ["no_steps", "Without steps"], ["no_tags", "Without tags"],
  ["photo_missing", "Photo file missing"], ["no_photo", "Without a photo"], ["to_review", "Still to review"],
];
$("#library-check-btn").addEventListener("click", async () => {
  const btn = $("#library-check-btn");
  const out = $("#library-check-result");
  btn.disabled = true;
  out.replaceChildren(el("p", { class: "field-hint", text: tx("Working...") }));
  let data = null;
  try {
    const res = await fetch(`${API}/library-check`);
    if (res.ok) data = await res.json();
  } catch { /* below */ }
  btn.disabled = false;
  if (!data) {
    out.replaceChildren(el("p", { class: "field-hint field-warning", text: tx("Couldn't reach Open the Pantry. Check your connection and try again.") }));
    return;
  }
  const open = (id) => () => { closeModal(settingsOverlay); openRecipeDetail(id); };
  const link = (r) => el("button", { type: "button", class: "link-button", text: r.title, onclick: open(r.id) });
  const parts = [el("p", { class: "field-hint", text: txn(data.total, "{n} recipe checked.", "{n} recipes checked.") })];
  for (const [key, label] of LIBRARY_CHECKS) {
    const list = data.checks[key] || [];
    if (!list.length) continue;
    parts.push(el("details", { class: "library-check-group" }, [
      el("summary", { text: `${tx(label)} (${list.length})` }),
      el("ul", {}, list.map((r) => el("li", {}, [link(r)]))),
    ]));
  }
  if (data.duplicates.length) {
    parts.push(el("details", { class: "library-check-group" }, [
      el("summary", { text: `${tx("Look like the same recipe")} (${data.duplicates.length})` }),
      el("p", { class: "field-hint", text: tx("Same link or title. To combine two, select them and use Merge….") }),
      el("ul", {}, data.duplicates.map((set) => el("li", { class: "library-check-set" },
        set.flatMap((r, i) => (i ? [document.createTextNode(" · "), link(r)] : [link(r)]))))),
    ]));
  }
  if (parts.length === 1) parts.push(el("p", { text: tx("Nothing to report.") }));
  out.replaceChildren(...parts);
});

// Settings -> Tags: preview, then add, the tagger's suggestions for every
// recipe (POST /api/tags/auto-apply). Two steps inside the dialog rather
// than a browser confirm(), which would block the page.
const autotagBtn = $("#autotag-btn");
const autotagResult = $("#autotag-result");
function autotagChangeList(changes) {
  return el("details", { class: "autotag-details" }, [
    el("summary", { text: tx("Show each recipe") }),
    el("ul", {}, changes.map((c) => el("li", { text: `${c.title}: ${c.added.map((n) => tagLabel(n)).join(", ")}` }))),
  ]);
}
autotagBtn.addEventListener("click", async () => {
  autotagBtn.disabled = true;
  autotagResult.innerHTML = "";
  autotagResult.appendChild(el("p", { class: "field-hint", text: tx("Checking every recipe...") }));
  let preview;
  try {
    const res = await fetch(`${API}/tags/auto-apply?dry_run=true`, { method: "POST" });
    if (!res.ok) throw new Error(`error ${res.status}`);
    preview = await res.json();
  } catch (e) {
    autotagResult.innerHTML = "";
    announceError(tx("Couldn't check the recipes ({1}).", { 1: e.message }));
    autotagBtn.disabled = false;
    return;
  }
  autotagResult.innerHTML = "";
  if (!preview.tags_added) {
    autotagResult.appendChild(el("p", { text: tx("All {1} recipes checked already have their suggested tags", { 1: preview.recipes_scanned }) +
      (preview.recipes_ignored ? ` (${preview.recipes_ignored} skipped).` : ".") }));
    announce(tx("Nothing to add."));
    autotagBtn.disabled = false;
    return;
  }
  // One checkbox per suggested tag, grouped under a checkbox per recipe
  // (which ticks or unticks all of that recipe's tags). All start ticked.
  const summaryEl = el("p", {});
  const tagBoxes = [];
  const list = el("ul", { class: "autotag-pick" }, preview.changes.map((c) => {
    const recipeBox = el("input", { type: "checkbox", checked: "", "aria-label": tx("All suggested tags for {1}", { 1: c.title }) });
    const boxes = c.added.map((name) => {
      const b = el("input", { type: "checkbox", checked: "" });
      b.dataset.recipe = String(c.id); b.dataset.tag = name;
      tagBoxes.push(b);
      return b;
    });
    const sync = () => {
      const n = boxes.filter((b) => b.checked).length;
      recipeBox.checked = n === boxes.length;
      recipeBox.indeterminate = n > 0 && n < boxes.length;
      updateSummary();
    };
    recipeBox.addEventListener("change", () => { boxes.forEach((b) => { b.checked = recipeBox.checked; }); sync(); });
    boxes.forEach((b) => b.addEventListener("change", sync));
    const skipBtn = el("button", {
      type: "button", class: "autotag-skip", text: tx("Skip in future scans"),
      "aria-label": tx("Leave {1} out of future tag scans", { 1: c.title }),
      onclick: async () => {
        skipBtn.disabled = true;
        const res = await fetch(`${API}/recipes/${c.id}/autotag-ignore`, jsonBody("PATCH", { ignored: true })).catch(() => null);
        if (!res || !res.ok) { skipBtn.disabled = false; announceError(tx("Couldn't skip that recipe.")); return; }
        boxes.forEach((b) => { const i = tagBoxes.indexOf(b); if (i >= 0) tagBoxes.splice(i, 1); });
        skipBtn.closest("li").remove();
        announce(tx("{1} will be left out of tag scans.", { 1: c.title }));
        updateSummary();
      },
    });
    return el("li", {}, [
      el("div", { class: "autotag-row" }, [
        el("label", { class: "autotag-recipe" }, [recipeBox, el("span", { text: c.title })]),
        skipBtn,
      ]),
      el("div", { class: "autotag-tags" }, boxes.map((b) =>
        el("label", { class: "autotag-tag" }, [b, el("span", { text: tagLabel(b.dataset.tag) })]))),
    ]);
  }));
  function updateSummary() {
    const ticked = tagBoxes.filter((b) => b.checked);
    const recipes = new Set(ticked.map((b) => b.dataset.recipe)).size;
    summaryEl.textContent = txn(recipes, "Adds {1} of {2} suggested tags to {n} recipe. Untick any that don't apply.",
      "Adds {1} of {2} suggested tags to {n} recipes. Untick any that don't apply.", { 1: ticked.length, 2: tagBoxes.length }) +
      (preview.recipes_ignored ? " " + txn(preview.recipes_ignored,
        "{n} recipe is skipped (each recipe's edit screen can include it again, or suggest tags for it).",
        "{n} recipes are skipped (each recipe's edit screen can include it again, or suggest tags for it).") : "");
    apply.disabled = ticked.length === 0;   // only called once apply exists
  }
  const cancel = el("button", { type: "button", class: "btn-secondary", text: tx("Cancel"), onclick: () => {
    autotagResult.innerHTML = ""; autotagBtn.disabled = false; autotagBtn.focus();
  } });
  const apply = el("button", { type: "button", class: "btn-primary", text: tx("Add tags"), onclick: async () => {
    apply.disabled = cancel.disabled = true;
    try {
      const selections = {};
      for (const b of tagBoxes) if (b.checked) (selections[b.dataset.recipe] ||= []).push(b.dataset.tag);
      const res = await fetch(`${API}/tags/auto-apply`, jsonBody("POST", {
        selections: Object.entries(selections).map(([id, tags]) => ({ id: Number(id), tags })),
      }));
      if (!res.ok) throw new Error(`error ${res.status}`);
      const done = await res.json();
      autotagResult.innerHTML = "";
      autotagResult.appendChild(el("p", { text:
        tx("Added {1} tag(s) to {2} recipe(s). Remove any you don't want from each recipe's page.", { 1: done.tags_added, 2: done.recipes_changed }) }));
      if (done.changes.length) autotagResult.appendChild(autotagChangeList(done.changes));
      announce(tx("Added {1} tags.", { 1: done.tags_added }));
      await loadTags();
      loadRecipes();
    } catch (e) {
      announceError(tx("Couldn't add the tags ({1}). Nothing was changed.", { 1: e.message }));
      apply.disabled = cancel.disabled = false;
      return;
    }
    autotagBtn.disabled = false;
  } });
  autotagResult.appendChild(summaryEl);
  autotagResult.appendChild(list);
  updateSummary();
  autotagResult.appendChild(el("div", { class: "autotag-actions" }, [apply, cancel]));
  apply.focus();
});

// Settings -> Tags -> Manage tag groups and keywords.
const tgToggle = $("#taggroups-toggle");
const tgPanel = $("#taggroups-panel");
const tgOpen = new Set();   // which groups are expanded, kept across re-renders
tgToggle.addEventListener("click", () => {
  tgPanel.hidden = !tgPanel.hidden;
  tgToggle.setAttribute("aria-expanded", String(!tgPanel.hidden));
  if (!tgPanel.hidden) renderTagGroupsPanel();
});

async function tgRequest(url, method, body) {
  const res = await fetch(url, {
    method, headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  }).catch(() => null);
  if (!res || !res.ok) {
    const fallback = tx("Couldn't save that change.");
    announceError(res ? await errorDetail(res, fallback) : fallback);
    return null;
  }
  return res.json();
}

async function tgReload() {
  await loadTags();
  if (!$("#filter-panel").hidden) renderFilterPanel();
  renderTagGroupsPanel();
}

function renderTagGroupsPanel() {
  const list = $("#taggroups-list");
  list.innerHTML = "";
  const groups = state.tagGroups && state.tagGroups.length ? state.tagGroups : BUILTIN_GROUPS;
  for (const g of groups) {
    const tags = state.allTags.filter((t) => t.category === g.key)
      .sort((a, b) => a.name.localeCompare(b.name));
    const isMain = g.key === "main_ingredient";
    const rows = tags.map((t) => {
      const kw = el("input", { type: "text", value: t.keywords || "", placeholder: tx("extra words, comma separated"),
        "aria-label": tx("Keywords for {1}", { 1: tagLabel(t) }) });
      const rov = isMain && t.name !== "Vegetarian"   // stored name, not shown
        ? el("input", { type: "checkbox", "aria-label": tx("{1} rules out Vegetarian", { 1: tagLabel(t) }) }) : null;
      if (rov) rov.checked = !!t.rules_out_vegetarian;
      const save = el("button", { type: "button", class: "btn-secondary btn-small", text: tx("Save"), hidden: "",
        onclick: async () => {
          if (await tgRequest(`${API}/tags/${t.id}`, "PUT", { keywords: kw.value, rules_out_vegetarian: rov ? rov.checked : false })) {
            announce(tx("Saved keywords for {1}.", { 1: tagLabel(t) })); await tgReload();
          }
        } });
      const dirty = () => { save.hidden = false; };
      kw.addEventListener("input", dirty); if (rov) rov.addEventListener("change", dirty);
      const del = t.user_defined ? el("button", { type: "button", class: "tg-delete", text: tx("Delete"),
        "aria-label": tx("Delete tag {1}", { 1: tagLabel(t) }),
        onclick: async () => {
          if (del.dataset.armed !== "1") { del.dataset.armed = "1"; del.textContent = tx("Tap again to delete"); return; }
          const r = await tgRequest(`${API}/tags/${t.id}`, "DELETE");
          if (r) { announce(r.recipes_changed ? tx("Deleted {1} (removed from {2} recipes).", { 1: tagLabel(t), 2: r.recipes_changed }) : tx("Deleted {1}.", { 1: tagLabel(t) })); await tgReload(); }
        } }) : null;
      return el("li", { class: "tg-tag" }, [
        el("div", { class: "tg-tag-head" }, [el("strong", { text: tagLabel(t) }), del]),
        el("div", { class: "tg-tag-edit" }, [kw, save]),
        rov ? el("label", { class: "inline-check" }, [rov, el("span", { text: tx("Means it's not vegetarian") })]) : null,
      ]);
    });

    const newName = el("input", { type: "text", placeholder: tx("New tag name"), "aria-label": tx("New tag in {1}", { 1: groupLabel(g.key, g.label) }), maxlength: "40" });
    const newKw = el("input", { type: "text", placeholder: tx("keywords, comma separated"), "aria-label": tx("Keywords for the new tag") });
    const newRov = isMain ? el("input", { type: "checkbox" }) : null;
    const add = el("button", { type: "button", class: "btn-primary btn-small", text: tx("Add tag"), onclick: async () => {
      if (!newName.value.trim()) { newName.focus(); return; }
      const r = await tgRequest(`${API}/tags`, "POST", { name: newName.value, category: g.key, keywords: newKw.value,
        rules_out_vegetarian: newRov ? newRov.checked : false });
      if (r) { tgOpen.add(g.key); announce(tx("Added {1} to {2}.", { 1: tagLabel(r), 2: groupLabel(g.key, g.label) })); await tgReload(); }
    } });
    const addRow = el("div", { class: "tg-add" }, [
      newName, newKw,
      newRov ? el("label", { class: "inline-check" }, [newRov, el("span", { text: tx("Means it's not vegetarian") })]) : null,
      add,
    ]);

    const delGroup = g.builtin ? null : el("button", { type: "button", class: "tg-delete", text: tx("Delete group"),
      onclick: async (e) => {
        e.preventDefault();
        if (delGroup.dataset.armed !== "1") {
          delGroup.dataset.armed = "1";
          delGroup.textContent = tags.length ? txn(tags.length, "Delete group and its {n} tag?", "Delete group and its {n} tags?") : tx("Tap again to delete");
          return;
        }
        const r = await tgRequest(`${API}/tag-groups/${encodeURIComponent(g.key)}`, "DELETE");
        if (r) { announce(tx("Deleted {1}.", { 1: g.label })); await tgReload(); }
      } });

    const det = el("details", { class: "tg-group" }, [
      el("summary", {}, [el("span", { text: `${groupLabel(g.key, g.label)} (${tags.length})` })]),
      delGroup,
      el("ul", { class: "tg-tags" }, rows),
      addRow,
    ]);
    if (tgOpen.has(g.key)) det.open = true;
    det.addEventListener("toggle", () => { if (det.open) tgOpen.add(g.key); else tgOpen.delete(g.key); });
    list.appendChild(det);
  }

  const groupName = el("input", { type: "text", placeholder: tx("New group, e.g. Cuisine"), maxlength: "40", "aria-label": tx("New group name") });
  list.appendChild(el("div", { class: "tg-add tg-add-group" }, [
    groupName,
    el("button", { type: "button", class: "btn-primary btn-small", text: tx("Add group"), onclick: async () => {
      if (!groupName.value.trim()) { groupName.focus(); return; }
      const r = await tgRequest(`${API}/tag-groups`, "POST", { label: groupName.value });
      if (r) { tgOpen.add(r.key); announce(tx("Added the {1} group. Add tags to it below.", { 1: r.label })); await tgReload(); }
    } }),
  ]));
}

// Settings -> Learning, What to eat?, photos and checkboxes (settings.js).
initPreferencePanels();

$("#batch-cancel-btn").addEventListener("click", () => {
  state.selectMode = false;
  state.selectedIds.clear();
  $("#select-mode-toggle").setAttribute("aria-pressed", "false");
  updateBatchBar();
  renderRecipeList(currentRecipes);
});

// Select mode -> Tags: one existing tag on or off every selected recipe
// (POST /api/recipes/batch-tags). The selection stays, so another tag
// can follow; the list reloads to show the change.
const batchTagsOverlay = $("#batch-tags-overlay");
$("#batch-tags-close").addEventListener("click", () => closeModal(batchTagsOverlay));
batchTagsOverlay.addEventListener("click", (e) => { if (e.target === batchTagsOverlay) closeModal(batchTagsOverlay); });

$("#batch-tags-btn").addEventListener("click", () => {
  if (!state.selectedIds.size) return;
  const select = $("#batch-tags-select");
  const previous = select.value;
  select.replaceChildren(...tagGroups().map(([key, label]) => {
    const tags = state.allTags.filter((t) => t.category === key)
      .sort((a, b) => tagLabel(a).localeCompare(tagLabel(b), LANG));
    return tags.length ? el("optgroup", { label }, tags.map((t) => el("option", { value: String(t.id), text: tagLabel(t) }))) : null;
  }).filter(Boolean));
  if (previous && select.querySelector(`option[value="${CSS.escape(previous)}"]`)) select.value = previous;
  const none = !select.options.length;
  $("#batch-tags-add").disabled = none;
  $("#batch-tags-remove").disabled = none;
  $("#batch-tags-count").textContent = txn(state.selectedIds.size, "{n} recipe selected", "{n} recipes selected");
  $("#batch-tags-status").textContent = none ? tx("There are no tags yet.") : "";
  openModal(batchTagsOverlay);
});

async function batchTags(action) {
  const select = $("#batch-tags-select");
  const tag = state.allTags.find((t) => String(t.id) === select.value);
  if (!tag || !state.selectedIds.size) return;
  const buttons = [$("#batch-tags-add"), $("#batch-tags-remove")];
  buttons.forEach((b) => { b.disabled = true; });
  const status = $("#batch-tags-status");
  status.textContent = tx("Working...");
  const result = await fetchWithTimeout(`${API}/recipes/batch-tags`, jsonBody("POST", { ids: [...state.selectedIds], tag_id: tag.id, action }));
  buttons.forEach((b) => { b.disabled = false; });
  if (!result.ok) {
    status.textContent = result.status === 404 ? tx("That tag no longer exists.") : writeFailureMessage(result, tx("The selected recipes"));
    if (result.status === 404) loadTags();
    return;
  }
  const body = await result.res.json();
  const name = tagLabel(tag);
  const parts = [action === "add"
    ? txn(body.changed.length, "“{1}” added to {n} recipe.", "“{1}” added to {n} recipes.", { 1: name })
    : txn(body.changed.length, "“{1}” removed from {n} recipe.", "“{1}” removed from {n} recipes.", { 1: name })];
  if (body.unchanged.length) {
    parts.push(action === "add"
      ? txn(body.unchanged.length, "{n} recipe already had it.", "{n} recipes already had it.")
      : txn(body.unchanged.length, "{n} recipe didn't have it.", "{n} recipes didn't have it."));
  }
  if (body.full.length) parts.push(txn(body.full.length, "{n} recipe already has 50 tags.", "{n} recipes already have 50 tags."));
  if (body.missing.length) parts.push(txn(body.missing.length, "{n} recipe no longer exists.", "{n} recipes no longer exist."));
  status.textContent = parts.join(" ");
  announce(parts.join(" "));
  loadRecipes();
}
$("#batch-tags-add").addEventListener("click", () => batchTags("add"));
$("#batch-tags-remove").addEventListener("click", () => batchTags("remove"));

// Select mode -> Merge… (exactly two selected): each part of the merged
// recipe from one or the other (POST /api/recipes/merge). The recipe
// whose ingredients and steps are kept stays; the other goes to
// Recently deleted. A part both have the same is not asked about.
const mergeOverlay = $("#merge-overlay");
$("#merge-close").addEventListener("click", () => closeModal(mergeOverlay));
mergeOverlay.addEventListener("click", (e) => { if (e.target === mergeOverlay) closeModal(mergeOverlay); });
let mergePair = null;

function mergeSummaries(r) {
  const stars = r.tastiness_rating ? "★".repeat(r.tastiness_rating) + "☆".repeat(5 - r.tastiness_rating) : null;
  const ratings = [r.favorite ? tx("Favorite") : null, stars,
    r.cook_time_rating ? valueLabel(r.cook_time_rating) : null, r.difficulty_rating ? valueLabel(r.difficulty_rating) : null,
    r.actual_cook_time ? tx("Real-world cook time: {1}", { 1: cookTimeLabel(r.actual_cook_time) }) : null].filter(Boolean);
  const details = [r.servings ? tx("Servings: {1}", { 1: r.servings }) : null,
    r.prep_time ? `${tx("Prep time")}: ${r.prep_time}` : null, r.cook_time ? `${tx("Cook time")}: ${r.cook_time}` : null,
    r.total_time ? `${tx("Total time")}: ${r.total_time}` : null].filter(Boolean);
  let host = null;
  try { host = r.source_url ? new URL(r.source_url).host.replace(/^www\./, "") : null; } catch { host = r.source_url; }
  const first = r.ingredients.length ? ingredientText(r.ingredients[0]) : "";
  return {
    title: r.title,
    photo: r.image_path,
    content: `${txn(r.ingredients.length, "{n} ingredient", "{n} ingredients")}, ${txn(r.steps.length, "{n} step", "{n} steps")}`
      + (first ? ` (${first.length > 40 ? first.slice(0, 40) + "…" : first}…)` : ""),
    contentSize: r.ingredients.length + r.steps.length,
    details: details.join(" · "), detailsSize: details.length,
    source: host, ratings: ratings.join(" · "), ratingsSize: ratings.length,
    notes: (r.notes || "").trim(),
  };
}

function mergeRow(name, legend, options, chosen) {
  return el("fieldset", { class: "merge-row" }, [
    el("legend", { text: legend }),
    ...options.map(([value, content]) => el("label", { class: "merge-option" }, [
      el("input", { type: "radio", name: `merge-${name}`, value, ...(value === chosen ? { checked: "" } : {}) }),
      el("span", { class: "merge-option-text" },
        [].concat(content).map((c) => (typeof c === "string" ? document.createTextNode(c) : c))),
    ])),
  ]);
}

$("#batch-merge-btn").addEventListener("click", async () => {
  if (state.selectedIds.size !== 2) return;
  const ids = [...state.selectedIds];
  let recipes;
  try {
    recipes = await Promise.all(ids.map(async (id) => {
      const res = await fetch(`${API}/recipes/${id}`);
      if (!res.ok) throw new Error(String(res.status));
      return res.json();
    }));
  } catch {
    announceError(tx("Couldn't load the two recipes. The list has been refreshed."));
    loadRecipes();
    return;
  }
  mergePair = recipes;
  const [s1, s2] = recipes.map(mergeSummaries);
  const pickBigger = (a, b, tie) => (a > b ? "1" : b > a ? "2" : tie);
  const content = pickBigger(s1.contentSize, s2.contentSize, "1");
  const other = content === "1" ? "2" : "1";
  const has = (s, k) => !!s[k];
  const prefer = (k) => (has(content === "1" ? s1 : s2, k) ? content : has(content === "1" ? s2 : s1, k) ? other : content);
  const opt = (n, text) => [n, [el("strong", { text: `${n}. ` }), document.createTextNode(text || tx("None"))]];
  const rows = [
    el("ol", { class: "merge-pair" }, recipes.map((r) => el("li", { text: r.title }))),
  ];
  if (s1.title !== s2.title) rows.push(mergeRow("title", tx("Title"), [opt("1", s1.title), opt("2", s2.title)], content));
  if (s1.photo || s2.photo) {
    // Only a recipe that has one is offered, then "No photo".
    const photo = (n, s) => [n, [el("strong", { text: `${n}. ` }),
      el("img", { class: "merge-thumb", src: `/uploads/${s.photo}`, alt: tx("Photo of recipe {1}", { 1: n }) })]];
    const options = [s1.photo ? photo("1", s1) : null, s2.photo ? photo("2", s2) : null, ["none", tx("No photo")]].filter(Boolean);
    rows.push(mergeRow("photo", tx("Photo"), options, prefer("photo")));
  }
  rows.push(mergeRow("content", tx("Ingredients and steps"), [opt("1", s1.content), opt("2", s2.content)], content));
  if (s1.details !== s2.details) {
    rows.push(mergeRow("details", tx("Servings and times"), [opt("1", s1.details), opt("2", s2.details)],
      pickBigger(s1.detailsSize, s2.detailsSize, content)));
  }
  if (s1.source !== s2.source) rows.push(mergeRow("source", tx("Link"), [opt("1", s1.source), opt("2", s2.source)], prefer("source")));
  if (s1.ratings !== s2.ratings) {
    rows.push(mergeRow("ratings", tx("Favorite, ratings and real-world time"), [opt("1", s1.ratings), opt("2", s2.ratings)],
      pickBigger(s1.ratingsSize, s2.ratingsSize, content)));
  }
  if (s1.notes && s2.notes && s1.notes !== s2.notes) {
    const cut = (t) => (t.length > 80 ? t.slice(0, 80) + "…" : t);
    rows.push(mergeRow("notes", tx("Notes"), [["both", tx("Both, one after the other")], opt("1", cut(s1.notes)), opt("2", cut(s2.notes))], "both"));
  }
  $("#merge-body").replaceChildren(...rows);
  $("#merge-status").textContent = "";
  $("#merge-submit").disabled = false;
  openModal(mergeOverlay);
});

$("#merge-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!mergePair) return;
  const form = $("#merge-form");
  const val = (name, fallback) => (form.querySelector(`input[name="merge-${name}"]:checked`) || {}).value || fallback;
  const keepN = val("content", "1");
  const [keep, other] = keepN === "1" ? mergePair : [mergePair[1], mergePair[0]];
  const rel = (n) => (n === keepN ? "keep" : "other");
  const pickOr = (name, fallback) => { const v = val(name, null); return v === null ? fallback : rel(v); };
  const photoChoice = val("photo", null);
  const notesChoice = val("notes", null);
  const take = {
    title: pickOr("title", "keep"),
    photo: photoChoice === "none" ? "none" : photoChoice ? rel(photoChoice) : "keep",
    content: "keep",
    details: pickOr("details", "keep"),
    source: pickOr("source", "keep"),
    ratings: pickOr("ratings", "keep"),
    // Not asked (only one has notes, or both the same): those notes.
    notes: notesChoice === "both" ? "both" : notesChoice ? rel(notesChoice) : ((keep.notes || "").trim() ? "keep" : "other"),
  };
  const btn = $("#merge-submit");
  btn.disabled = true;
  $("#merge-status").textContent = tx("Working...");
  const result = await fetchWithTimeout(`${API}/recipes/merge`, jsonBody("POST", { keep_id: keep.id, other_id: other.id, take }));
  btn.disabled = false;
  if (!result.ok) {
    // A 400 says why (notes or tags over a recipe's limits); show it.
    const err = result.status === 400 ? await result.res.json().catch(() => ({})) : {};
    $("#merge-status").textContent = typeof err.detail === "string" ? err.detail
      : writeFailureMessage(result, tx("The selected recipes"));
    if (result.status === 404) loadRecipes();
    return;
  }
  const merged = await result.res.json();
  closeModal(mergeOverlay);
  mergePair = null;
  state.selectedIds.clear();
  state.selectMode = false;
  $("#select-mode-toggle").setAttribute("aria-pressed", "false");
  updateBatchBar();
  announce(tx("Merged into “{1}”. The other recipe is in Recently deleted.", { 1: merged.title }));
  await loadTags();
  await loadTimeBuckets();
  loadRecipes();
});

$("#batch-delete-btn").addEventListener("click", async () => {
  if (!state.selectedIds.size) return;
  if (!confirm(txn(state.selectedIds.size, "Delete {n} recipe? You can put it back from Recently deleted for 30 days.",
    "Delete {n} recipes? You can put them back from Recently deleted for 30 days."))) return;
  const ids = [...state.selectedIds];
  const btn = $("#batch-delete-btn");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = tx("Deleting…");

  const result = await fetchWithTimeout(`${API}/recipes/batch-delete`, jsonBody("POST", { ids }));

  btn.disabled = false;
  btn.textContent = label;

  if (!result.ok) {
    // Keep the selection on failure. It used to be cleared unconditionally
    // alongside an unconditional "deleted" message, so a failed batch told
    // you it had worked AND threw away the selection you would need to
    // retry with.
    announceError(writeFailureMessage(result, tx("The selected recipes")));
    if (result.timedOut) loadRecipes();
    return;
  }

  // The endpoint reports per-item outcomes (deleted / missing / failed).
  // Those were being discarded, so a batch where half the items failed
  // still announced complete success.
  let summary;
  try {
    const body = await result.res.json();
    // Only the deleted and missing ones lose their ticks, not one the
    // server failed to delete (review of 0179).
    [...(body.deleted || []), ...(body.missing || [])].forEach(forgetTicks);
    const parts = [txn(body.deleted.length, "{n} recipe moved to Recently deleted (in the side menu) for 30 days",
      "{n} recipes moved to Recently deleted (in the side menu) for 30 days")];
    if (body.missing.length) parts.push(tx("{1} already gone", { 1: body.missing.length }));
    if (body.failed.length) parts.push(tx("{1} failed", { 1: body.failed.length }));
    summary = parts.join(", ") + ".";
    if (!body.failed.length) {
      state.selectedIds.clear();
      state.selectMode = false;
      $("#select-mode-toggle").setAttribute("aria-pressed", "false");
    } else {
      // Leave the failures selected so a retry is one tap.
      state.selectedIds = new Set(body.failed.map((f) => f.id));
    }
  } catch {
    summary = `${ids.length} recipe(s) deleted.`;
    state.selectedIds.clear();
    state.selectMode = false;
    $("#select-mode-toggle").setAttribute("aria-pressed", "false");
  }

  announce(summary);
  updateBatchBar();
  await loadTimeBuckets();
  loadRecipes();
});

// -------------------------------------------------------------------
// Recipe list / grid
// -------------------------------------------------------------------
let currentRecipes = [];

// While searching, "Best match" is offered first in the sort list and
// used when the sort was the default (newest first); it isn't saved,
// and clearing the search goes back to the saved sort.
function syncBestMatch() {
  const select = $("#sort-select");
  const searching = !!state.query;
  let opt = select.querySelector('option[value="relevance|desc"]');
  if (searching && !opt) {
    opt = el("option", { value: "relevance|desc", text: tx("Best match") });
    select.prepend(opt);
  }
  if (searching && !state.wasSearching) state.bestMatch = state.sort === "created" && state.direction === "desc";
  if (!searching) {
    state.bestMatch = false;
    if (opt) opt.remove();
  }
  state.wasSearching = searching;
  select.value = state.bestMatch ? "relevance|desc" : `${state.sort}|${state.direction}`;
}

// "Showing results for chicken (you typed chiken)", from the server's
// X-Search-Corrected header.
function showSearchNote(res) {
  const note = $("#search-note");
  let corrected = {};
  try { corrected = JSON.parse(res.headers.get("X-Search-Corrected") || "{}"); } catch { corrected = {}; }
  const words = Object.entries(corrected);
  note.hidden = !words.length;
  note.textContent = words.length ? words.map(([typed, used]) =>
    tx("Showing results for {1} (you typed {2}).", { 1: used.join(", "), 2: typed })).join(" ") : "";
}

// Only the newest request's answer is shown: a slow one for the previous
// filters used to replace the list after a faster one for the new
// filters (review of 0158).
let recipesGeneration = 0;
async function loadRecipes() {
  const generation = ++recipesGeneration;
  const stale = () => generation !== recipesGeneration;
  syncBestMatch();
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  for (const t of state.filterTags) params.append("tags", t);
  if (state.maxMinutes != null) params.set("max_minutes", String(state.maxMinutes));
  for (const p of state.pace) params.append("pace", p);
  if (state.toReview) params.set("to_review", "true");
  params.set("sort", state.query && state.bestMatch ? "relevance" : state.sort);
  params.set("direction", state.direction);
  // An error used to be parsed as the recipe list, which came out empty
  // and read "No recipes match" -- e.g. a 429 from the rate limiter.
  let res;
  try {
    res = await fetch(`${API}/recipes?${params.toString()}`);
  } catch {
    if (stale()) return;
    showListError(tx("Couldn't reach Open the Pantry. Check your connection and try again."));
    return;
  }
  if (stale()) return;
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    if (stale()) return;
    showListError(res.status === 429
      ? tx("The server is busy with too many requests. Wait a moment and try again.")
      : tx("Couldn't load recipes ({1}).", { 1: typeof err.detail === "string" ? err.detail : tx("error {1}", { 1: res.status }) }));
    return;
  }
  const recipes = await res.json();
  if (stale()) return;
  showSearchNote(res);
  currentRecipes = recipes;
  renderRecipeList(currentRecipes);
  announce(txn(currentRecipes.length, "{n} recipe found", "{n} recipes found"));
  refreshReviewCount();
  refreshBin();
}

// -------------------------------------------------------------------
// Recently deleted (0109): deleting moves a recipe here for 30 days.
// -------------------------------------------------------------------
const binOverlay = $("#bin-overlay");
$("#bin-close").addEventListener("click", () => closeModal(binOverlay));
binOverlay.addEventListener("click", (e) => { if (e.target === binOverlay) closeModal(binOverlay); });

async function fetchBin() {
  try {
    const res = await fetch(`${API}/recently-deleted`);
    return res.ok ? await res.json() : null;
  } catch { return null; }
}

// The sidebar count; the open sheet is redrawn too.
async function refreshBin(data) {
  data = data || await fetchBin();
  if (!data) return;
  const count = data.recipes.length;
  if (!binOverlay.hidden) renderBin(data);
  if (count === state.binCount) return;
  state.binCount = count;
  renderSidebar();
}

function renderBin(data) {
  const list = $("#bin-list");
  $("#bin-empty").hidden = !data.recipes.length;
  if (!data.recipes.length) {
    list.replaceChildren(el("li", { class: "bin-empty-note", text: tx("Nothing here.") }));
    return;
  }
  list.replaceChildren(...data.recipes.map((r) => {
    const restore = el("button", { type: "button", class: "btn-secondary btn-small", text: tx("Put back"),
      "aria-label": tx("Put back {1}", { 1: r.title }), onclick: () => binAction(r, "restore", restore) });
    const forget = el("button", { type: "button", class: "btn-secondary btn-small btn-danger", text: tx("Delete now"),
      "aria-label": tx("Delete {1} for good", { 1: r.title }), onclick: () => binAction(r, "forget", forget) });
    return el("li", { class: "bin-item" }, [
      el("div", { class: "bin-item-text" }, [
        el("span", { class: "bin-item-title", text: r.title }),
        el("span", { class: "field-hint", text: `${tx("Deleted {1}", { 1: fmtDateTime(r.deleted_at) })} · ${
          txn(r.days_left, "gone in {n} day", "gone in {n} days")}` }),
      ]),
      el("div", { class: "bin-item-actions" }, [restore, forget]),
    ]);
  }));
}

async function openBin() {
  $("#bin-status").textContent = "";
  const data = await fetchBin();
  if (!data) { announceError(tx("Couldn't reach Open the Pantry. Check your connection and try again.")); return; }
  renderBin(data);
  openModal(binOverlay);
}

async function binAction(recipe, action, btn) {
  if (action === "forget" && !confirm(tx("Delete “{1}” for good? This cannot be undone.", { 1: recipe.title }))) return;
  btn.disabled = true;
  const result = action === "restore"
    ? await fetchWithTimeout(`${API}/recently-deleted/${recipe.id}/restore`, { method: "POST" })
    : await fetchWithTimeout(`${API}/recently-deleted/${recipe.id}`, { method: "DELETE" });
  if (!result.ok && result.status !== 404) {
    btn.disabled = false;
    $("#bin-status").textContent = writeFailureMessage(result, tx("That recipe"));
    return;
  }
  const msg = result.status === 404 ? tx("That recipe was no longer in Recently deleted.")
    : action === "restore" ? tx("Put back: {1}", { 1: recipe.title }) : tx("Deleted for good: {1}", { 1: recipe.title });
  $("#bin-status").textContent = msg;
  announce(msg);
  if (action === "restore") { await loadTimeBuckets(); loadRecipes(); }
  else refreshBin();
}

$("#bin-empty").addEventListener("click", async () => {
  if (!confirm(tx("Delete everything in Recently deleted for good? This cannot be undone."))) return;
  const btn = $("#bin-empty");
  btn.disabled = true;
  const result = await fetchWithTimeout(`${API}/recently-deleted`, { method: "DELETE" });
  btn.disabled = false;
  if (!result.ok) { $("#bin-status").textContent = writeFailureMessage(result, tx("Recently deleted")); return; }
  const n = (await result.res.json()).deleted;
  const msg = txn(n, "{n} recipe deleted for good.", "{n} recipes deleted for good.");
  $("#bin-status").textContent = msg;
  announce(msg);
  refreshBin();
});

// The sidebar's "To review (n)". The last recipe reviewed turns the
// filter off, so the list doesn't stay empty behind a hidden button.
async function refreshReviewCount() {
  let count;
  try {
    const res = await fetch(`${API}/review-queue`);
    if (!res.ok) return;
    count = (await res.json()).count;
  } catch { return; }
  if (count === state.reviewCount && (count || !state.toReview)) return;
  state.reviewCount = count;
  if (!count && state.toReview) {
    state.toReview = false;
    syncClearFiltersButton();
    renderSidebar();
    loadRecipes();
    return;
  }
  renderSidebar();
}

// "Looks right": off the review list without changing anything.
async function markReviewed(recipe) {
  let res;
  try {
    res = await fetch(`${API}/recipes/${recipe.id}/reviewed`, { method: "POST" });
  } catch { res = null; }
  if (!res || !res.ok) {
    announceError(await responseErrorText(res));
    return;
  }
  announce(tx("Marked as checked."));
  renderRecipeDetail(await res.json());
  loadRecipes();
}

// Top of a recipe saved without review: where it came from, and the
// three ways out (correct it, accept it, or delete it).
function reviewBanner(recipe) {
  if (!recipe.needs_review) return null;
  const where = { url: tx("Saved from a batch of links without being checked."),
    pdf: tx("Saved from a batch of PDFs without being checked."),
    document: tx("Saved from a batch of documents without being checked."),
    email: tx("Saved from an email without being checked.") }[recipe.source_type]
    || tx("Saved without being checked.");
  return el("div", { class: "review-banner", role: "note" }, [
    el("p", { class: "review-banner-text" }, [
      el("strong", { text: tx("To review") }), document.createTextNode(` — ${where} `),
      document.createTextNode(tx("Compare it with the original, then mark it as right or correct it.")),
    ]),
    el("div", { class: "review-banner-actions" }, [
      el("button", { class: "btn-primary btn-small", type: "button", text: tx("Review"),
        onclick: () => renderRecipeEditForm(recipe) }),
      el("button", { class: "btn-secondary btn-small", type: "button", text: tx("Looks right"),
        onclick: () => markReviewed(recipe) }),
      el("button", { class: "btn-secondary btn-small btn-danger", type: "button", text: tx("Delete this import"),
        onclick: (e) => deleteRecipe(recipe.id, e.currentTarget, tx("Delete this import")) }),
    ]),
  ]);
}

// Where a suggested tag came from (tagger.explain_tags): the keyword that
// matched, the learned model, or Vegetarian's "no meat word found", so a
// tag that keeps coming back wrongly can be traced.
function tagSourceParts(t) {
  if (t.source === "keyword" && t.keyword) return [document.createTextNode(tx("keyword:") + " "), el("em", { text: t.keyword })];
  if (t.source === "learned") return [document.createTextNode(tx("learned"))];
  if (t.source === "no_meat") return [document.createTextNode(tx("no meat or fish word found"))];
  return [];
}

function tagSourcesNote(tags) {
  const shown = (tags || []).filter((t) => t.source);
  if (!shown.length) return null;
  const parts = [document.createTextNode(tx("Suggested because:") + " ")];
  shown.forEach((t, i) => {
    if (i) parts.push(document.createTextNode("; "));
    parts.push(el("span", { class: "tag-source" }, [
      el("strong", { text: tagLabel(t) }), document.createTextNode(" ("), ...tagSourceParts(t), document.createTextNode(")"),
    ]));
  });
  return el("div", { class: "field-hint tag-sources" }, parts);
}

// The review view's "Check these": what the server thinks most likely
// went wrong (review.py). Line numbers are the line in the box below.
function reviewChecklist(hints, fields) {
  const items = [];
  const quote = (t) => `“${t.length > 80 ? t.slice(0, 80) + "…" : t}”`;
  if (hints.title_guessed) {
    items.push(tx("Title: read from the text (usually its first line)."));
    fields.title.classList.add("needs-check");
  }
  if (hints.servings_missing) {
    items.push(tx("Servings: none found."));
    fields.servings.classList.add("needs-check");
  }
  if (hints.ocr_confidence != null) items.push(tx("Text recognition was unsure ({1}%): look for misread words and numbers.", { 1: hints.ocr_confidence }));
  if (hints.text_cut) items.push(tx("Only the start of the text was read, because it is very long. What comes after is not in this recipe."));
  if (hints.no_ingredients) items.push(tx("No ingredients were found."));
  if (hints.left_out && hints.left_out.length) {
    items.push(tx("Above the ingredients, left out: {1}. Ingredients read without their amount?",
      { 1: hints.left_out.map(quote).join(", ") }));
    fields.ingredients.classList.add("needs-check");
  }
  if (hints.no_steps) items.push(tx("No steps were found."));
  for (const i of hints.ingredients) {
    const n = i.index + 1;
    items.push(i.reason === "no_amount"
      ? tx("Ingredients, line {1}: {2} has no amount.", { 1: n, 2: quote(i.text) })
      : tx("Ingredients, line {1}: {2} — the learned model wasn't sure this is an ingredient.", { 1: n, 2: quote(i.text) }));
  }
  for (const st of hints.steps) {
    const n = st.index + 1;
    items.push(st.reason === "short"
      ? tx("Steps, line {1}: {2} is very short. Part of the next step, or not a step?", { 1: n, 2: quote(st.text) })
      : st.reason === "long"
        ? tx("Steps, line {1} is very long. Several steps run together?", { 1: n })
        : tx("Steps, line {1}: {2} — the learned model wasn't sure this is a step.", { 1: n, 2: quote(st.text) }));
  }
  if (hints.ingredients.length) fields.ingredients.classList.add("needs-check");
  if (hints.steps.length || hints.no_steps) fields.steps.classList.add("needs-check");
  return el("div", { class: "review-checklist" }, [
    el("h2", { class: "review-checklist-title", text: tx("Check these") }),
    items.length
      ? el("ul", {}, items.map((t) => el("li", { text: t })))
      : el("p", { text: tx("Nothing in particular stood out. Compare it with the original text, then save or mark it as right.") }),
  ]);
}

// An ingredient as it was written ("3 Tablespoons plus 1 teaspoon maple
// syrup"), not rebuilt from the parsed fields ("3 tbsp plus 1 teaspoon
// maple syrup"). The parsed quantity/unit/name are for sorting and
// filtering; the screens used to show -- and, via the edit form, save --
// the rebuilt string, which normalised units and moved parentheticals.
function ingredientText(i) {
  return (i.raw_line || [i.quantity, i.unit, i.name].filter(Boolean).join(" ")).trim();
}

function showListError(message) {
  const region = $("#recipe-list-region");
  region.innerHTML = "";
  region.appendChild(el("div", { class: "empty-state", role: "alert" }, [
    el("p", { text: message }),
    el("button", { class: "btn-secondary", type: "button", text: tx("Try again"), onclick: () => loadRecipes() }),
  ]));
  announce(message);
}

function sourceBadge(recipe) {
  const label = { url: tx("Web"), pdf: "PDF", document: tx("Document"), screenshot: tx("Screenshot"), manual: tx("Handwritten/Manual"), email: tx("Email"),
    import: tx("Imported") }[recipe.source_type] || recipe.source_type;
  return el("span", { class: `badge badge-source-${recipe.source_type}`, text: label });
}

// "View original": the page a web or emailed-link recipe was read from.
// Only http(s) is linked; source_url comes from stored data (including a
// restored backup), so anything else, such as a javascript: URL, is ignored.
function sourceLink(recipe) {
  let url;
  try { url = new URL(recipe.source_url); } catch (e) { return null; }
  if (url.protocol !== "http:" && url.protocol !== "https:") return null;
  return el("a", {
    class: "badge badge-link", href: url.href, target: "_blank", rel: "noopener noreferrer",
    title: url.hostname.replace(/^www\./, ""),
  }, [el("span", { text: tx("View original") }), el("span", { "aria-hidden": "true", text: " \u2197" })]);
}

function confidenceBadge(recipe) {
  if (recipe.ocr_confidence == null) return null;
  const c = recipe.ocr_confidence;
  const tier = c >= 80 ? "high" : c >= 55 ? "med" : "low";
  const label = tier === "low" ? tx("OCR quality: low") : tier === "med" ? tx("OCR quality: fair") : tx("OCR quality: good");
  return el("span", { class: `badge badge-confidence-${tier}`, text: label });
}

// Searching: where the words matched when it isn't the title ("…1 cup
// buttermilk…"), matches marked. The server puts \u0002 / \u0003 around
// them; the text goes in as text nodes, never as markup.
function matchSnippet(recipe) {
  const s = recipe.match_snippet;
  if (!s) return null;
  const parts = [];
  for (const [i, piece] of s.split(/[\u0002\u0003]/).entries()) {
    if (piece) parts.push(i % 2 ? el("mark", { text: piece }) : document.createTextNode(piece));
  }
  return el("p", { class: "card-snippet" }, parts);
}

// Only for a recipe found by a tag alone: a match in the text shows in
// the title or the excerpt (matchSnippet).
function matchedViaBadge(recipe) {
  const via = recipe.matched_via || [];
  if (!via.includes("tag") || via.includes("text")) return null;
  return el("span", { class: "badge badge-matched", text: tx("Matched a tag") });
}

// -------------------------------------------------------------------
// Card-level quick controls: favorite toggle + 3 rating popovers
// -------------------------------------------------------------------
// The message in an error response: a plain "detail" string, or the
// first validation error's text from a 422.
function apiErrorText(body, status) {
  const d = body && body.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length && d[0] && typeof d[0].msg === "string") return d[0].msg.replace(/^Value error, /, "");
  if (!status) return tx("Couldn't reach Open the Pantry. Check your connection and try again.");
  return tx("That didn't work (status {1}).", { 1: status });
}

// apiErrorText for a failed response, or for none (a request that never
// reached the server: res is null).
async function responseErrorText(res) {
  return apiErrorText(res ? await res.json().catch(() => ({})) : {}, res ? res.status : 0);
}

// False when offline too (a rejected fetch used to escape as an
// unhandled error, with nothing on screen). A change made on the recipe
// page is copied to the list's entry, shown when the page closes: the
// page works on its own copy of the recipe, so the list kept the old
// rating until a refresh (DeepSeek review of 0073).
let listStale = false;
function syncListEntry(id, fields) {
  const entry = currentRecipes.find((r) => r.id === id);
  if (entry) { Object.assign(entry, fields); listStale = true; }
}

async function patchRating(id, payload) {
  try {
    const res = await fetch(`${API}/recipes/${id}/rating`, jsonBody("PATCH", payload));
    if (res.ok) syncListEntry(id, payload);
    return res.ok;
  } catch {
    return false;
  }
}

async function patchNotes(id, notes) {
  try {
    const res = await fetch(`${API}/recipes/${id}/notes`, jsonBody("PATCH", { notes }));
    // The list draws the notes icon from has_notes (DeepSeek review of 0180).
    if (res.ok) syncListEntry(id, { notes: notes || null, has_notes: !!notes });
    return res.ok;
  } catch {
    return false;
  }
}

function closeAllRatingPopovers() {
  $$(".rating-popover").forEach((p) => { p.hidden = true; });
  $$(".rating-control > button").forEach((b) => b.setAttribute("aria-expanded", "false"));
}
document.addEventListener("click", (e) => {
  if (!e.target.closest(".rating-control")) closeAllRatingPopovers();
  if (!e.target.closest(".share-menu-wrap")) {
    $$(".share-menu").forEach((m) => { m.hidden = true; });
    $$(".share-menu-wrap > button[aria-haspopup]").forEach((b) => b.setAttribute("aria-expanded", "false"));
  }
});

const RATING_TITLES = {
  tastiness_rating: tx("How good was it?"),
  cook_time_rating: tx("How long does it take?"),
  difficulty_rating: tx("How hard is it?"),
};

const RATING_FIELD_NAMES = {
  tastiness_rating: () => tx("Rating"),
  cook_time_rating: () => tx("Cook time"),
  difficulty_rating: () => tx("Difficulty"),
};

function ratingOptionLabel(field, opt) {
  return field === "tastiness_rating"
    ? "\u2b50".repeat(opt)
    : valueLabel(opt);
}

// Rating controls come in two shapes for the same reason the share button
// does: on a list card the inline popover is unusable. .recipe-card sets
// overflow:hidden, so the popover is clipped at the card edge, and because
// a card is short the popover also lands on top of the thumbnail and the
// title. On the detail page there is room and nothing clips, so the popover
// stays there. Both paths write through the same patch + apply helper, so
// the behaviour can't drift.
// A card is only ~15rem wide and the row also carries the favourite and
// share buttons. Full words ("Moderate", "Medium") overflow it, the row
// wraps, and share drops onto a line of its own. Cards get a one-character
// form -- the icon already says which rating it is, and the sheet spells
// the value out in full. The detail page has the room, so it keeps words.
function compactRatingLabel(v) {
  return typeof v === "number" ? String(v) : valueLabel(v).charAt(0).toUpperCase();
}

function applyRating(recipe, field, opt, btn, icon, shortLabel) {
  recipe[field] = opt;
  btn.setAttribute("data-has-value", String(opt != null));
  btn.textContent = opt != null ? `${icon} ${shortLabel(opt)}` : icon;
  if (opt != null) btn.setAttribute("title", `${field.replace(/_/g, " ")}: ${opt}`);
  else btn.removeAttribute("title");
}

function openRatingSheet(recipe, field, icon, options, shortLabel, btn) {
  const overlay = $("#rating-sheet-overlay");
  $("#rating-sheet-heading").textContent = RATING_TITLES[field] || tx("Rating");
  $("#rating-sheet-title").textContent = recipe.title;
  const actions = $("#rating-sheet-actions");
  actions.innerHTML = "";

  for (const opt of options) {
    actions.appendChild(el("button", {
      type: "button",
      "aria-pressed": String(recipe[field] === opt),
      text: ratingOptionLabel(field, opt),
      onclick: async () => {
        closeModal(overlay);
        const ok = await patchRating(recipe.id, { [field]: opt });
        if (ok) applyRating(recipe, field, opt, btn, icon, shortLabel);
        else announceError(tx("Could not save rating."));
      },
    }));
  }

  // Setting a rating by mistake needs a way out (the recipe view's popover
  // has the same button).
  if (recipe[field] != null) {
    actions.appendChild(el("button", {
      type: "button", class: "rating-clear",
      text: tx("Clear rating"),
      onclick: async () => {
        closeModal(overlay);
        const ok = await patchRating(recipe.id, { [field]: null });
        if (ok) applyRating(recipe, field, null, btn, icon, compactRatingLabel);
        else announceError(tx("Could not clear rating."));
      },
    }));
  }

  openModal(overlay);
}

function ratingPopoverControl(recipe, field, icon, options, shortLabel, inline = false) {
  const current = recipe[field];
  const label = inline ? shortLabel : compactRatingLabel;
  const btn = el("button", {
    type: "button", "data-has-value": String(current != null),
    "aria-haspopup": "true", "aria-expanded": "false",
    text: current != null ? `${icon} ${label(current)}` : icon,
    "aria-label": tx("Set {1}", { 1: RATING_FIELD_NAMES[field]().toLowerCase() }),
    title: current != null ? `${RATING_FIELD_NAMES[field]()}: ${typeof current === "number" ? current : valueLabel(current)}` : null,
  });

  if (!inline) {
    btn.setAttribute("aria-haspopup", "dialog");
    btn.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      // The card's own (compact) label, so the button reads the same after a pick.
      openRatingSheet(recipe, field, icon, options, label, btn);
    });
    return el("div", { class: "rating-control" }, [btn]);
  }

  const popover = el("div", { class: "rating-popover", hidden: "" });
  btn.addEventListener("click", () => {
    const willOpen = popover.hidden;
    closeAllRatingPopovers();
    popover.hidden = !willOpen;
    btn.setAttribute("aria-expanded", String(willOpen));
  });

  for (const opt of options) {
    popover.appendChild(el("button", {
      type: "button",
      "aria-pressed": String(recipe[field] === opt),
      "data-option": String(opt),
      text: ratingOptionLabel(field, opt),
      onclick: async () => {
        popover.hidden = true;
        btn.setAttribute("aria-expanded", "false");
        const ok = await patchRating(recipe.id, { [field]: opt });
        if (ok) {
          applyRating(recipe, field, opt, btn, icon, shortLabel);
          clear.hidden = false;
          // The pressed state is what a screen reader announces: it was
          // set once, so after a pick the list still named the old rating
          // (DeepSeek review of 0181).
          popover.querySelectorAll("button[data-option]").forEach((b) =>
            b.setAttribute("aria-pressed", String(b.dataset.option === String(opt))));
        }
        else announceError(tx("Could not save rating."));
      },
    }));
  }
  // The recipe view had no way to take a rating off, only to change it
  // (DeepSeek review of 0180). Shown while there is one.
  const clear = el("button", {
    type: "button", class: "rating-clear", text: tx("Clear rating"),
    hidden: recipe[field] == null ? "" : null,
    onclick: async () => {
      popover.hidden = true;
      btn.setAttribute("aria-expanded", "false");
      const ok = await patchRating(recipe.id, { [field]: null });
      if (ok) {
        applyRating(recipe, field, null, btn, icon, shortLabel);
        popover.querySelectorAll("button[data-option]").forEach((b) => b.setAttribute("aria-pressed", "false"));
        clear.hidden = true;
      } else announceError(tx("Could not clear rating."));
    },
  });
  popover.appendChild(clear);

  return el("div", { class: "rating-control" }, [btn, popover]);
}

function cardQuickControls(recipe, inline = false) {
  // On a list card this row sits inside the card's link (<a href="#recipe-N">).
  // Stopping the click here keeps the card's handler from opening the
  // recipe, but that handler is also what cancelled the link, so a tap on
  // the heart or a rating followed it: the address changed and iOS
  // redrew the list at the top.
  // Only a tap on a control is kept from the card. The row is as wide as
  // the card (Share sits at the far end), and a tap on the empty space
  // between the ratings and Share used to do nothing at all; it now opens
  // the recipe like the rest of the card.
  const wrap = el("div", {
    class: inline ? "card-quick-controls inline" : "card-quick-controls",
    onclick: (e) => {
      if (!e.target.closest("button, .rating-popover")) return;
      e.stopPropagation(); e.preventDefault();
    },
  });

  const favBtn = el("button", {
    type: "button", class: "favorite-btn",
    "aria-pressed": String(!!recipe.favorite),
    "aria-label": recipe.favorite ? tx("Remove from favorites") : tx("Add to favorites"),
    text: recipe.favorite ? "\u2665" : "\u2661",
    onclick: async () => {
      const newVal = !recipe.favorite;
      const ok = await patchRating(recipe.id, { favorite: newVal });
      if (ok) {
        recipe.favorite = newVal;
        favBtn.setAttribute("aria-pressed", String(newVal));
        favBtn.textContent = newVal ? "\u2665" : "\u2661";
      } else {
        announceError(tx("Could not save the favorite."));
      }
    },
  });
  wrap.appendChild(favBtn);

  wrap.appendChild(ratingPopoverControl(recipe, "tastiness_rating", "\u2b50", [1, 2, 3, 4, 5], (v) => String(v), inline));
  wrap.appendChild(ratingPopoverControl(recipe, "cook_time_rating", "\u23f1", ["quick", "moderate", "long"], valueLabel, inline));
  wrap.appendChild(ratingPopoverControl(recipe, "difficulty_rating", "\ud83c\udf9a", ["easy", "medium", "hard"], valueLabel, inline));

  // Icon-only share, pushed to the far end of the row so it sits opposite
  // the ratings. Only on list cards -- the detail page has its own
  // labelled Share button in the header, so a second one here would be
  // redundant.
  if (!inline) {
    wrap.appendChild(el("button", {
      type: "button", class: "card-share-btn",
      "aria-label": tx("Share {1}", { 1: recipe.title }),
      title: tx("Share"),
      trustedStaticHtml: SHARE_ICON_SVG,
      onclick: (e) => { e.preventDefault(); e.stopPropagation(); openShareSheet(recipe); },
    }));
  }

  return wrap;
}

function gridAttrs() {
  return { class: "recipe-grid", "data-select-mode": String(state.selectMode),
           "data-show-thumbnails": String(state.listStyle !== "titles"), "data-list-style": state.listStyle };
}

function recipeCard(recipe) {
  const isSelected = state.selectedIds.has(recipe.id);
  const card = el("a", {
    href: `#recipe-${recipe.id}`,
    class: recipe.image_path ? "recipe-card" : "recipe-card recipe-card--no-image",
    onclick: (e) => {
      e.preventDefault();
      if (state.selectMode) { toggleSelect(recipe.id); return; }
      openRecipeDetail(recipe.id);
    },
  });
  // Only render the image element when there is actually an image. It used
  // to be emitted unconditionally with src="", and the CSS aspect-ratio
  // plus border-coloured background turned that into a grey placeholder
  // box on every photo-less recipe. Omitting the element entirely (rather
  // than hiding it) means the card is genuinely shorter instead of leaving
  // the gap behind.
  if (recipe.image_path) {
    card.appendChild(el("img", {
      class: "recipe-card-image",
      src: `/uploads/${recipe.image_path}`,
      // async: a large photo is decoded off the main thread, so scrolling
      // and taps don't wait on it.
      alt: "", loading: "lazy", decoding: "async",
    }));
  }

  // Source and OCR-confidence badges are detail-page only now: they sat in
  // an absolutely-positioned overlay in the photo's corner, which is both
  // clutter in a list and homeless once a card has no photo. The
  // matched-via label stays, because on a search result it explains WHY a
  // recipe matched -- but it moves in-flow into the card body below, for
  // the same reason the overlay had to go.

  if (state.selectMode) {
    const checkboxAttrs = {
      type: "checkbox", class: "card-select-box",
      "aria-label": tx("Select {1}", { 1: recipe.title }),
      onclick: (e) => { e.stopPropagation(); e.preventDefault(); toggleSelect(recipe.id); },
    };
    if (isSelected) checkboxAttrs.checked = "";
    card.appendChild(el("input", checkboxAttrs));
  }

  // The favourite/rating controls live IN FLOW inside the card body, on
  // their own row under the title -- they used to be absolutely positioned
  // at the card's bottom-right, which is exactly where the title sits, so
  // they painted straight over it (worse the longer the title). Keeping
  // them in flow means the card grows to fit them and the controls can be
  // sized for touch without ever colliding with text again.
  const matched = matchedViaBadge(recipe);
  card.appendChild(el("div", { class: "recipe-card-body" }, [
    el("h3", { class: "recipe-card-title recipe-title" }, [
      document.createTextNode(recipe.title),
      recipe.has_notes ? el("span", { class: "card-notes-icon", role: "img", "aria-label": tx("Has notes"),
        title: tx("Has notes"), trustedStaticHtml: NOTE_ICON_SVG }) : null,
    ]),
    matched || recipe.needs_review ? el("div", { class: "card-matched-row" }, [
      recipe.needs_review ? el("span", { class: "badge badge-review", text: tx("To review") }) : null,
      matched,
    ]) : null,
    matchSnippet(recipe),
    // Tiles show the photo and title; the rest is a tap away.
    state.selectMode || state.listStyle === "tiles" ? null : cardQuickControls(recipe),
  ]));
  return card;
}

function renderRecipeList(recipes) {
  const region = $("#recipe-list-region");
  region.innerHTML = "";

  if (!recipes.length) {
    region.appendChild(el("div", { class: "empty-state", text: tx("No recipes match. Tap + to add one, or adjust your filters.") }));
    return;
  }

  if (state.grouping === "all") {
    region.appendChild(el("div", gridAttrs(), recipes.map(recipeCard)));
    return;
  }

  const category = state.grouping;
  const tagsInCategory = state.allTags.filter((t) => t.category === category);
  const buckets = new Map();
  for (const t of tagsInCategory) buckets.set(t.name, { tag: t, recipes: [] });
  const uncategorized = [];

  for (const r of recipes) {
    const matches = r.tags.filter((t) => t.category === category);
    if (!matches.length) { uncategorized.push(r); continue; }
    for (const m of matches) {
      if (!buckets.has(m.name)) buckets.set(m.name, { tag: m, recipes: [] });
      buckets.get(m.name).recipes.push(r);
    }
  }

  const mainBuckets = [...buckets.values()].filter((b) => !b.tag.subgroup && b.recipes.length);
  const subBuckets = [...buckets.values()].filter((b) => b.tag.subgroup && b.recipes.length);

  for (const b of mainBuckets) region.appendChild(groupSection(tagLabel(b.tag), b.recipes));

  if (subBuckets.length) {
    const subWrap = el("div", { class: "group-section" });
    subWrap.appendChild(el("h2", { text: tx("Cocktail Prep") }));
    for (const b of subBuckets) subWrap.appendChild(groupSection(tagLabel(b.tag), b.recipes));
    region.appendChild(subWrap);
  }

  if (uncategorized.length) region.appendChild(groupSection(tx("Uncategorized"), uncategorized));
}

function groupSection(title, recipes) {
  return el("section", { class: "group-section" }, [
    el("h3", { text: title }),
    el("div", gridAttrs(), recipes.map(recipeCard)),
  ]);
}

// -------------------------------------------------------------------
// Duration input helper (dd:hh:mm), used for the real-world cook time
// -------------------------------------------------------------------
// "dd:hh:mm" -> "1 d 4 h 15 min" (French "1 j 4 h 15 min"), zero parts left out.
function cookTimeLabel(ddhhmm) {
  const [d, h, m] = String(ddhhmm || "").split(":").map((p) => parseInt(p, 10) || 0);
  const parts = [];
  if (d) parts.push(`${d}\u00a0${tx("d")}`);
  if (h) parts.push(`${h}\u00a0h`);
  if (m) parts.push(`${m}\u00a0min`);
  return parts.join(" ") || `0\u00a0min`;
}

function makeDurationInput(prefix, initialDdHhMm) {
  let initD = "", initH = "", initM = "";
  if (initialDdHhMm) {
    const parts = initialDdHhMm.split(":").map((p) => parseInt(p, 10));
    if (parts.length === 3 && parts.every((n) => !Number.isNaN(n))) {
      [initD, initH, initM] = parts.map(String);
    }
  }
  const days = el("input", { type: "number", min: "0", id: `${prefix}-days`, "aria-label": tx("Days"), value: initD, placeholder: "0" });
  const hours = el("input", { type: "number", min: "0", max: "23", id: `${prefix}-hours`, "aria-label": tx("Hours"), value: initH, placeholder: "0" });
  const minutes = el("input", { type: "number", min: "0", max: "59", id: `${prefix}-minutes`, "aria-label": tx("Minutes"), value: initM, placeholder: "0" });
  const wrap = el("div", { class: "duration-input" }, [
    days, el("span", { text: tx("d") }), hours, el("span", { text: tx("h") }), minutes, el("span", { text: tx("m") }),
  ]);
  return {
    element: wrap,
    getValue: () => {
      const d = parseInt(days.value || "0", 10) || 0;
      const h = parseInt(hours.value || "0", 10) || 0;
      const m = parseInt(minutes.value || "0", 10) || 0;
      if (!d && !h && !m) return null;
      return `${String(d).padStart(2, "0")}:${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
    },
  };
}

// -------------------------------------------------------------------
// Recipe detail view
// -------------------------------------------------------------------
const detailOverlay = $("#recipe-detail-overlay");

function closeRecipeDetail() {
  // Always release on close -- otherwise the screen stays on
  // indefinitely after you've walked away from the recipe.
  releaseWakeLock();
  detailRecipeId = null;
  detailRecipe = null;
  closeModal(detailOverlay);
  if (listStale) { listStale = false; renderRecipeList(currentRecipes); }
}

$("#recipe-detail-close").addEventListener("click", closeRecipeDetail);
detailOverlay.addEventListener("click", (e) => { if (e.target === detailOverlay) closeRecipeDetail(); });

// The dialog opens on the tap itself and fills in when the recipe
// arrives. It used to open only after the download, so on a slow
// connection (the NAS busy, or the list's photos still downloading) a
// tap seemed to do nothing and people tapped again. Only the latest tap
// counts, and closing the dialog while it loads cancels the open.
let detailTicket = 0;
// The recipe the dialog shows, for Print (null while loading or closed).
let detailRecipeId = null;
// Its data, to draw it again when the checkboxes setting changes.
let detailRecipe = null;

// What's ticked in a recipe, on this device: the place and text of each
// line (an edited recipe drops the ticks of lines that changed). Kept
// until cleared, so a tick outlasts the app being closed mid-recipe.
// A deleted recipe's ticks go with it (review of 0177); put back from
// Recently deleted, it starts unticked.
function forgetTicks(id) {
  try { localStorage.removeItem(`recipe-app-ticks-${id}`); } catch { /* private mode */ }
}

function recipeTicks(id) {
  const key = `recipe-app-ticks-${id}`;
  let saved = {};
  try { saved = JSON.parse(storageGet(key) || "{}") || {}; } catch { saved = {}; }
  const sets = { i: new Set(saved.i || []), s: new Set(saved.s || []) };
  const save = () => {
    if (!sets.i.size && !sets.s.size) { try { localStorage.removeItem(key); } catch { /* private mode */ } return; }
    storageSet(key, JSON.stringify({ i: [...sets.i], s: [...sets.s] }));
  };
  const of = (kind) => ({
    isTicked: (index, text) => sets[kind].has(`${index}\u0000${text}`),
    toggle: (index, text, on) => { sets[kind][on ? "add" : "delete"](`${index}\u0000${text}`); save(); },
  });
  return { ingredients: of("i"), steps: of("s"), clear: () => { sets.i.clear(); sets.s.clear(); save(); } };
}
async function openRecipeDetail(id) {
  const ticket = ++detailTicket;
  detailRecipeId = null;
  const body = $("#recipe-detail-body");
  wakeLockListeners.clear();
  body.replaceChildren(el("p", { class: "detail-loading", role: "status", text: tx("Loading...") }));
  $("#recipe-detail-modal").scrollTop = 0;
  if (detailOverlay.hidden) openModal(detailOverlay);
  let recipe = null;
  try {
    const res = await fetch(`${API}/recipes/${id}`, { priority: "high" });
    if (res.ok) recipe = await res.json();
  } catch { /* offline with nothing cached: handled below */ }
  if (ticket !== detailTicket || detailOverlay.hidden) return;   // superseded or closed
  if (!recipe) {
    closeRecipeDetail();   // also releases the wake lock, if one is held
    announceError(tx("Could not load recipe."));
    return;
  }
  renderRecipeDetail(recipe);
  if (wakeLockSupported && getWakeLockDefault()) await setWakeLockWanted(true);
}

// An address ending in #recipe-<id> opens that recipe, on load and when
// the hash changes: the What to eat? page links to recipes this way. The
// cards' own #recipe-<id> links don't set it (they open the dialog
// directly), so only an address from outside does.
const RECIPE_HASH = /^#recipe-(\d+)$/;
function openRecipeFromHash() {
  const m = RECIPE_HASH.exec(location.hash);
  if (m) openRecipeDetail(Number(m[1]));
}
function clearRecipeHash() {
  // replaceState: no history entry and no hashchange, so Back still goes
  // to the page that linked here.
  if (RECIPE_HASH.test(location.hash)) history.replaceState(history.state, "", location.pathname + location.search);
}
window.addEventListener("hashchange", openRecipeFromHash);

// -------------------------------------------------------------------
// Notes: a toggle button (visually distinct once notes exist) that
// reveals an inline editor. Saved via the dedicated notes endpoint so
// this doesn't need to resend the whole recipe payload.
// -------------------------------------------------------------------
// -------------------------------------------------------------------
// Per-recipe wake lock toggle. Reflects the ACTUAL lock state rather
// than what was requested -- if the browser denies or silently revokes
// it, the button shows off, because a wake lock that looks on but
// isn't is worse than no button at all.
// -------------------------------------------------------------------
function wakeLockToggle() {
  if (!wakeLockSupported) return null;

  const input = el("input", { type: "checkbox", role: "switch", class: "switch", id: "wakelock-recipe" });
  const row = el("label", { class: "switch-row wakelock-row", for: "wakelock-recipe" }, [
    el("span", { text: tx("Keep screen on") }),
    input,
  ]);

  function sync() {
    input.checked = wakeLockActive();
  }

  input.addEventListener("change", async () => {
    await setWakeLockWanted(input.checked);
    if (input.checked && !wakeLockActive()) {
      announce(tx("The browser wouldn't keep the screen awake \u2014 it may be blocked on low battery."));
    }
    sync();
  });

  wakeLockListeners.add(sync);
  sync();
  return row;
}

function notesSection(recipe) {
  const hasNotes = () => !!recipe.notes;

  const textarea = el("textarea", {
    id: `notes-textarea-${recipe.id}`,
    placeholder: tx("Substitutions, timing tweaks, how it turned out..."),
    style: "min-height:6rem;",
  });
  textarea.value = recipe.notes || "";

  const editorWrap = el("div", { class: "notes-editor", hidden: "" });

  const toggleBtn = el("button", {
    class: "btn-secondary notes-toggle-btn", type: "button",
    "data-has-notes": String(hasNotes()),
    "aria-expanded": "false",
    "aria-label": hasNotes() ? tx("View or edit notes") : tx("Add notes"),
    text: "\ud83d\udcdd " + (hasNotes() ? tx("Notes") : tx("Add notes")),
    onclick: () => {
      const willOpen = editorWrap.hidden;
      editorWrap.hidden = !willOpen;
      toggleBtn.setAttribute("aria-expanded", String(willOpen));
      if (willOpen) textarea.focus();
    },
  });

  const syncButtonState = () => {
    toggleBtn.setAttribute("data-has-notes", String(hasNotes()));
    toggleBtn.textContent = "\ud83d\udcdd " + (hasNotes() ? tx("Notes") : tx("Add notes"));
    toggleBtn.setAttribute("aria-label", hasNotes() ? tx("View or edit notes") : tx("Add notes"));
  };

  const saveBtn = el("button", {
    class: "btn-primary", type: "button", text: tx("Save notes"),
    onclick: async () => {
      const newNotes = textarea.value.trim();
      const ok = await patchNotes(recipe.id, newNotes);
      if (ok) {
        recipe.notes = newNotes || null;
        syncButtonState();
        editorWrap.hidden = true;
        toggleBtn.setAttribute("aria-expanded", "false");
        announce(tx("Notes saved."));
      } else {
        announceError(tx("Could not save notes."));
      }
    },
  });

  const cancelBtn = el("button", {
    class: "btn-secondary", type: "button", text: tx("Cancel"),
    onclick: () => {
      textarea.value = recipe.notes || "";
      editorWrap.hidden = true;
      toggleBtn.setAttribute("aria-expanded", "false");
    },
  });

  editorWrap.appendChild(el("div", { class: "field" }, [
    el("label", { for: `notes-textarea-${recipe.id}`, text: tx("Notes") }),
    textarea,
    el("div", { style: "display:flex;gap:0.5rem;margin-top:0.5rem;" }, [saveBtn, cancelBtn]),
  ]));

  return el("div", { class: "notes-section" }, [toggleBtn, editorWrap]);
}

// -------------------------------------------------------------------
// Showcase image: upload/replace/remove, shown directly below the title.
// -------------------------------------------------------------------
function showcaseImageSection(recipe) {
  const container = el("div", { class: "showcase-image-section" });

  function render() {
    container.innerHTML = "";
    if (recipe.image_path) {
      container.appendChild(el("img", { class: "hero", src: `/uploads/${recipe.image_path}`, alt: "" }));
    }

    const fileInput = el("input", { type: "file", accept: "image/*", style: "display:none;" });
    fileInput.addEventListener("change", async () => {
      if (!fileInput.files.length) return;
      announce(tx("Uploading photo..."));
      const fd = new FormData(); fd.append("file", fileInput.files[0]);
      const upRes = await fetch(`${API}/upload-image`, { method: "POST", body: fd });
      if (!upRes.ok) { announceError(tx("Could not upload photo.")); return; }
      const { stored_file } = await upRes.json();
      const patchRes = await fetch(`${API}/recipes/${recipe.id}/image`, jsonBody("PATCH", { image_path: stored_file }));
      if (patchRes.ok) {
        recipe.image_path = (await patchRes.json()).image_path;
        announce(tx("Photo updated."));
        render();
        loadRecipes();  // keep the list thumbnail in sync
      } else {
        discardDraftFile(stored_file);
        announceError(tx("Could not save photo."));
      }
    });

    const changeBtn = el("button", {
      class: "btn-secondary", type: "button",
      text: recipe.image_path ? tx("Change photo") : tx("Add photo"),
      onclick: () => fileInput.click(),
    });

    const controls = [changeBtn, fileInput];
    if (recipe.image_path) {
      controls.push(el("button", {
        class: "btn-secondary", type: "button", text: tx("Remove photo"),
        onclick: async () => {
          if (!confirm(tx("Remove this recipe's photo?"))) return;
          const res = await fetch(`${API}/recipes/${recipe.id}/image`, jsonBody("PATCH", { image_path: null }));
          if (res.ok) {
            recipe.image_path = null;
            announce(tx("Photo removed."));
            render();
            loadRecipes();
          } else {
            announceError(tx("Could not remove photo."));
          }
        },
      }));
    }
    container.appendChild(el("div", { class: "photo-controls", style: "display:flex;gap:0.5rem;margin-top:0.5rem;flex-wrap:wrap;" }, controls));
  }

  render();
  return container;
}

// -------------------------------------------------------------------
// Post-save editing. Necessary because three ingestion paths (batch
// URL, batch PDF, email) auto-save with no review screen at all, and
// even reviewed ones can turn out to have OCR garbage you only notice
// later. Shows the stored raw extracted text alongside the editable
// fields -- the same affordance as the pre-save review screen, since
// fixing bad OCR means comparing against what was actually read.
//
// PUT /api/recipes/{id} is a FULL REPLACE: every field it touches must
// be sent back or it's silently cleared. That includes ones this form
// doesn't expose (ratings, favorite, logged cook time) and, critically,
// image_path -- omitting it would delete the showcase image file.
// -------------------------------------------------------------------
function renderRecipeEditForm(recipe) {
  const body = $("#recipe-detail-body");
  body.innerHTML = "";
  wakeLockListeners.clear();

  const titleInput = el("input", { type: "text", id: "edit-title", value: recipe.title || "" });
  const servingsInput = el("input", { type: "text", id: "edit-servings", maxlength: "100", value: recipe.servings || "" });
  const prepInput = el("input", { type: "text", id: "edit-prep", value: recipe.prep_time || "" });
  const cookInput = el("input", { type: "text", id: "edit-cook", value: recipe.cook_time || "" });
  const totalInput = el("input", { type: "text", id: "edit-total", value: recipe.total_time || "" });

  const ingredientsArea = el("textarea", { id: "edit-ingredients", class: "recipe-lines" });
  ingredientsArea.value = recipe.ingredients
    .map((i) => ingredientText(i))
    .join("\n");

  const stepsArea = el("textarea", { id: "edit-steps", class: "recipe-lines" });
  stepsArea.value = recipe.steps.map((s) => s.text).join("\n");

  const tagsInput = el("input", {
    type: "text", id: "edit-tags",
    value: recipe.tags.map((t) => tagLabel(t)).join(", "),
  });
  // Suggested tags keep the tagger's category (Oven is a cooking style),
  // not "custom", when saved -- including ones that don't exist yet.
  const suggestedCats = new Map();
  const currentTagNames = () => tagsInput.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
  // The field shows labels ("Bœuf" in French); the server knows stored
  // names (Beef). Sent as stored names, so it can skip tags already there
  // and hold back Vegetarian when a meat tag is present.
  const currentStoredTagNames = () => currentTagNames().map((label) =>
    (findTagByLabel(label) || suggestedCats.get(label.toLowerCase()) || { name: label }).name);
  const suggestHint = el("div", { class: "field-hint", role: "status" });
  const suggestBtn = el("button", {
    type: "button", class: "btn-secondary btn-small", text: tx("Suggest tags"),
    onclick: async () => {
      suggestBtn.disabled = true;
      try {
        const res = await fetch(`${API}/tags/suggest`, jsonBody("POST", {
          title: titleInput.value, current_tags: currentStoredTagNames(),
          ingredients: linesOf(ingredientsArea), steps: linesOf(stepsArea),
        }));
        if (!res.ok) throw new Error(`error ${res.status}`);
        const found = await res.json();
        found.forEach((t) => suggestedCats.set(tagLabel(t).toLowerCase(), t));
        if (found.length) tagsInput.value = [...currentTagNames(), ...found.map((t) => tagLabel(t))].join(", ");
        if (found.length) {
          suggestHint.replaceChildren(document.createTextNode(tx("Added. Remove any that don't fit, then save.") + " "),
            tagSourcesNote(found));
        } else {
          suggestHint.textContent = tx("No new suggestions.");
        }
      } catch (e) {
        suggestHint.textContent = tx("Couldn't get suggestions ({1}).", { 1: e.message });
      }
      suggestBtn.disabled = false;
    },
  });
  const includeScan = el("input", { type: "checkbox", id: "edit-autotag-include" });
  includeScan.checked = !recipe.autotag_ignored;

  // Language (0125), for What to eat?'s Language choice. Automatic is the
  // guess from the text as last saved; each language is named in itself.
  const LANGUAGE_NAMES = { en: "English", fr: "Français" };
  const languageSelect = el("select", { id: "edit-language", "aria-describedby": "edit-language-hint" }, [
    el("option", { value: "", text: tx("Automatic ({1})", { 1: LANGUAGE_NAMES[recipe.detected_language] || LANGUAGE_NAMES.en }) }),
    ...Object.entries(LANGUAGE_NAMES).map(([code, name]) => el("option", { value: code, lang: code, text: name })),
  ]);
  languageSelect.value = LANGUAGE_NAMES[recipe.language] ? recipe.language : "";

  // pre-line: the test result puts sending and reading on separate lines.
  const statusLine = el("div", { class: "field-hint", role: "status", style: "margin-top:0.5rem;white-space:pre-line;" });

  const saveBtn = el("button", {
    class: "btn-primary", type: "button", text: tx("Save changes"),
    onclick: async () => {
      statusLine.textContent = tx("Saving...");
      const tagObjs = tagsFromInput(tagsInput.value, suggestedCats);

      const payload = {
        title: titleInput.value.trim() || tx("Untitled Recipe"),
        source_type: recipe.source_type,
        source_url: recipe.source_url || null,
        servings: servingsInput.value.trim() || null,
        prep_time: prepInput.value.trim() || null,
        cook_time: cookInput.value.trim() || null,
        total_time: totalInput.value.trim() || null,
        // Round-tripped, not edited here -- PUT would clear them otherwise.
        image_path: recipe.image_path || null,
        raw_text: recipe.raw_text || null,
        ocr_confidence: recipe.ocr_confidence != null ? recipe.ocr_confidence : null,
        favorite: !!recipe.favorite,
        tastiness_rating: recipe.tastiness_rating != null ? recipe.tastiness_rating : null,
        cook_time_rating: recipe.cook_time_rating || null,
        difficulty_rating: recipe.difficulty_rating || null,
        language: languageSelect.value || null,
        actual_cook_time: recipe.actual_cook_time || null,
        ingredients: ingredientsOf(ingredientsArea),
        steps: linesOf(stepsArea),
        tags: tagObjs,
      };

      try {
        const res = await fetch(`${API}/recipes/${recipe.id}`, jsonBody("PUT", payload));
        if (!res.ok) throw new Error(tx("Could not save changes."));
        let updated = await res.json();
        if (includeScan.checked === !!recipe.autotag_ignored) {
          const r2 = await fetch(`${API}/recipes/${recipe.id}/autotag-ignore`, jsonBody("PATCH", { ignored: !includeScan.checked }));
          if (r2.ok) updated = await r2.json();
          else announceError(tx("Saved, but the tag-scan setting couldn't be changed."));
        }
        announce(tx("Recipe updated."));
        renderRecipeDetail(updated);
        await loadTags();
        loadRecipes();
      } catch (err) {
        statusLine.textContent = err.message;
      }
    },
  });

  const cancelBtn = el("button", {
    class: "btn-secondary", type: "button", text: tx("Cancel"),
    onclick: () => renderRecipeDetail(recipe),
  });

  // A recipe saved without review opens as a review: what to check, and
  // "Looks right" / "Delete this import" beside Save.
  const reviewing = !!recipe.needs_review;
  const checklist = el("div", { class: "review-checklist-slot" });
  if (reviewing) {
    fetch(`${API}/recipes/${recipe.id}/review-hints`).then((r) => (r.ok ? r.json() : null)).then((hints) => {
      if (hints && checklist.isConnected) checklist.replaceChildren(reviewChecklist(hints,
        { title: titleInput, servings: servingsInput, ingredients: ingredientsArea, steps: stepsArea }));
    }).catch(() => {});
  }
  const reviewButtons = reviewing ? [
    el("button", { class: "btn-secondary", type: "button", text: tx("Looks right"), onclick: () => markReviewed(recipe) }),
    el("button", { class: "btn-secondary btn-danger", type: "button", text: tx("Delete this import"),
      onclick: (e) => deleteRecipe(recipe.id, e.currentTarget, tx("Delete this import")) }),
  ] : [];

  const fields = el("div", {}, [
    el("div", { class: "field" }, [el("label", { for: "edit-title", text: tx("Title") }), titleInput]),
    el("div", { class: "field" }, [
      el("label", { for: "edit-ingredients", text: tx("Ingredients (one per line)") }),
      ingredientsArea,
      partHint(ingredientsArea),
    ]),
    el("div", { class: "field" }, [
      el("label", { for: "edit-steps", text: tx("Steps (one per line)") }),
      stepsArea,
      partHint(stepsArea),
    ]),
    el("div", { class: "field" }, [
      el("label", { for: "edit-tags", text: tx("Tags (comma or semicolon separated)") }),
      el("div", { class: "tags-with-button" }, [tagsInput, suggestBtn]),
      suggestHint,
      el("label", { class: "inline-check", for: "edit-autotag-include" }, [
        includeScan, el("span", { text: tx("Include in Settings \u2192 Tags scans") }),
      ]),
    ]),
    el("div", { class: "field" }, [el("label", { for: "edit-servings", text: tx("Servings") }), servingsInput]),
    el("div", { class: "field" }, [el("label", { for: "edit-prep", text: tx("Prep time") }), prepInput]),
    el("div", { class: "field" }, [el("label", { for: "edit-cook", text: tx("Cook time") }), cookInput]),
    el("div", { class: "field" }, [el("label", { for: "edit-total", text: tx("Total time") }), totalInput]),
    el("div", { class: "field" }, [
      el("label", { for: "edit-language", text: tx("Language") }), languageSelect,
      el("div", { class: "field-hint", id: "edit-language-hint",
        text: tx("Automatic guesses from the ingredients and steps. Used by What to eat?") }),
    ]),
  ]);

  // Raw source text as a read-only reference pane. For a low-confidence
  // OCR scrape this is the whole point of the screen: compare the
  // parsed fields against what was actually extracted.
  const referencePane = recipe.raw_text
    ? el("div", {}, [
        el("h4", { text: tx("Original extracted text (reference)") }),
        el("div", { class: "review-raw", text: recipe.raw_text }),
        el("div", { class: "field-hint", text: tx("Read-only. What the scraper or OCR actually produced.") }),
      ])
    : el("div", { class: "field-hint", text: tx("No original extracted text stored for this recipe.") });

  body.appendChild(el("div", { class: "recipe-detail" }, [
    el("h1", { id: "recipe-detail-heading", class: "recipe-title", text: reviewing ? tx("Review import") : tx("Edit recipe") }),
    confidenceBadge(recipe),
    checklist,
    el("div", { class: "review-columns", style: "margin-top:0.75rem;" }, [referencePane, fields]),
    el("div", { style: "display:flex;gap:0.5rem;margin-top:1rem;flex-wrap:wrap;" }, [saveBtn, ...reviewButtons, cancelBtn]),
    statusLine,
  ]));
}

// Under the ingredient and step boxes: how to start a part of the recipe
// (shown as a heading; see isPartName). Linked to its box for screen readers.
function partHint(area) {
  const id = `${area.id}-hint`;
  area.setAttribute("aria-describedby", id);
  return el("div", { class: "field-hint", id, text: tx("A short line ending in “:” starts a section, e.g. “Sauce:”.") });
}

function renderRecipeDetail(recipe) {
  const body = $("#recipe-detail-body");
  body.innerHTML = "";
  detailRecipeId = recipe.id;
  detailRecipe = recipe;
  const ticks = state.showTicks ? recipeTicks(recipe.id) : null;
  // The previous detail view's toggle is about to be destroyed; drop
  // its listener so callbacks don't accumulate one per recipe opened.
  wakeLockListeners.clear();

  // Servings and the real-world cook time are typed in by hand. Once
  // either is set they read like the rest of the recipe's facts (the
  // line under the photo) with a small Edit button; the boxes only show
  // while nothing is set, or after Edit. One Save sends both.
  const meta = el("div", { class: "recipe-meta" });
  const factsEditor = el("div", { class: "recipe-facts-editor" });
  const hasFacts = () => !!(recipe.servings || recipe.actual_cook_time);

  function renderMeta() {
    const editing = !factsEditor.hidden;
    meta.replaceChildren(...[
      recipe.servings ? el("span", { text: tx("Servings: {1}", { 1: recipe.servings }) }) : null,
      recipe.prep_time ? el("span", { text: tx("Prep: {1}", { 1: recipe.prep_time }) }) : null,
      recipe.cook_time ? el("span", { text: tx("Cook: {1}", { 1: recipe.cook_time }) }) : null,
      recipe.total_time ? el("span", { text: tx("Total: {1}", { 1: recipe.total_time }) }) : null,
      recipe.actual_cook_time
        ? el("span", { text: tx("Real-world cook time: {1}", { 1: cookTimeLabel(recipe.actual_cook_time) }) }) : null,
      editing ? null : el("button", {
        type: "button", class: "meta-edit",
        "aria-label": tx("Edit servings and cook time"),
        text: hasFacts() ? tx("Edit") : tx("Add servings or cook time"),
        onclick: () => openFactsEditor(true),
      }),
    ].filter(Boolean));
  }

  function openFactsEditor(focus) {
    const servingsInput = el("input", {
      type: "text", id: `detail-servings-${recipe.id}`, class: "servings-input",
      maxlength: "100", placeholder: tx("e.g. 4"), value: recipe.servings || "",
    });
    const duration = makeDurationInput(`detail-time-${recipe.id}`, recipe.actual_cook_time);
    const save = async () => {
      const res = await fetch(`${API}/recipes/${recipe.id}/rating`, jsonBody("PATCH", { servings: servingsInput.value.trim(), actual_cook_time: duration.getValue() || "" })).catch(() => null);
      if (res && res.ok) {
        const saved = await res.json();
        recipe.servings = saved.servings;
        recipe.actual_cook_time = saved.actual_cook_time;
        announce(tx("Saved."));
        closeFactsEditor();
        await loadTimeBuckets();
        return;
      }
      // The server's message (a 422's validation list, translated
      // server-side) says what's wrong.
      announceError(await responseErrorText(res));
    };
    servingsInput.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
    factsEditor.replaceChildren(
      el("div", { class: "field" }, [
        el("label", { for: servingsInput.id, text: tx("Servings") }), servingsInput,
      ]),
      el("div", { class: "field" }, [
        el("label", { text: tx("Real-world cook time") }), duration.element,
      ]),
      el("div", { class: "recipe-facts-actions" }, [
        el("button", { class: "btn-secondary", type: "button", text: tx("Save"), onclick: save }),
        hasFacts() ? el("button", { class: "btn-secondary", type: "button", text: tx("Cancel"), onclick: closeFactsEditor }) : null,
      ]),
    );
    factsEditor.hidden = false;
    renderMeta();
    if (focus) servingsInput.focus();
  }

  // Nothing set: the boxes stay open (there is nothing to read yet).
  function closeFactsEditor() {
    if (!hasFacts()) { openFactsEditor(false); return; }
    factsEditor.hidden = true;
    factsEditor.replaceChildren();
    renderMeta();
  }
  closeFactsEditor();

  // ½ / 1× / 2× and As written / Weights / Volumes (amounts.js, shared
  // with What to eat?).
  const amounts = window.OTP_AMOUNTS.ingredientControls({
    lines: recipe.ingredients.map(ingredientText), servings: recipe.servings,
    language: recipe.language || recipe.detected_language,
    ticks: ticks && ticks.ingredients,
  });

  body.appendChild(el("div", { class: "recipe-detail" }, [
    // Share lives at the top right of the recipe card; the source/OCR
    // badges move under the title so the corner stays a single action.
    el("div", { class: "recipe-detail-header" }, [
      el("div", { class: "recipe-detail-header-main" }, [
        el("h1", { id: "recipe-detail-heading", class: "recipe-title", text: recipe.title }),
        el("div", { class: "recipe-detail-badges" }, [sourceBadge(recipe), sourceLink(recipe), confidenceBadge(recipe)]),
      ]),
      shareMenu(recipe),
    ]),
    reviewBanner(recipe),
    showcaseImageSection(recipe),
    meta,
    cardQuickControls(recipe, true),
    wakeLockToggle(),
    factsEditor,
    notesSection(recipe),
    // Side by side on a wide screen (ingredients stay in view while the
    // steps scroll), one after the other on a narrow one.
    el("div", { class: "recipe-body" }, [
      el("section", { class: "recipe-ingredients" }, [
        el("div", { class: "ingredients-head" }, [el("h2", { text: tx("Ingredients") }), amounts.scale, amounts.units]),
        amounts.note,
        amounts.list,
      ]),
      el("section", { class: "recipe-steps" }, [
        el("h2", { text: tx("Instructions") }),
        // With checkboxes, the numbers are drawn here, after the box: Safari
        // drew the list's own numbers over the box until the page was laid
        // out again (0179).
        // role="list": Safari's VoiceOver stops calling a list a list
        // once its numbering is turned off (review of 0179).
        el("ol", ticks ? { class: "steps-list has-ticks", role: "list" } : { class: "steps-list" }, (() => {
          // Part names ("Sauce tartare:") aren't numbered; the steps
          // after them carry on from the last number.
          let n = 0;
          return recipe.steps.map((s, index) => {
            if (isPartName(s.text)) return el("li", { class: "list-part", text: s.text });
            if (!ticks) return el("li", { value: String(++n), text: s.text });
            const on = ticks.steps.isTicked(index, s.text);
            const number = ++n;
            return el("li", { value: String(number), class: on ? "is-ticked" : null }, [
              window.OTP_AMOUNTS.tickRow([document.createTextNode(s.text)], on,
                (checked) => ticks.steps.toggle(index, s.text, checked),
                el("span", { class: "step-number", text: `${number}.` })),
            ]);
          });
        })()),
      ]),
    ]),
    ticks ? el("div", { class: "clear-ticks-row" }, [
      el("button", {
        class: "btn-secondary", type: "button", text: tx("Clear checkboxes"),
        onclick: () => {
          ticks.clear();
          body.querySelectorAll(".tick-box").forEach((b) => { b.checked = false; });
          body.querySelectorAll("li.is-ticked").forEach((li) => li.classList.remove("is-ticked"));
          announce(tx("Checkboxes cleared."));
        },
      }),
    ]) : null,
    // Edit and Delete are grouped at the foot of the recipe: both are
    // operations on the record rather than part of reading it, and Edit
    // was previously orphaned between the ratings row and the cook-time
    // form, where it read as part of the time-logging block. Delete gets
    // btn-danger so the pair don't look like two equivalent safe actions.
    el("div", { class: "recipe-record-actions" }, [
      el("button", {
        class: "btn-secondary", type: "button", text: tx("✎ Edit recipe"),
        onclick: () => renderRecipeEditForm(recipe),
      }),
      el("button", {
        class: "btn-secondary", type: "button", text: tx("⧉ Duplicate"),
        onclick: (e) => duplicateRecipe(recipe, e.currentTarget),
      }),
      el("button", {
        class: "btn-secondary btn-danger", type: "button", text: tx("Delete recipe"),
        onclick: (e) => deleteRecipe(recipe.id, e.currentTarget),
      }),
    ]),
  ]));
}

// Downloads an export without navigating the window.
//
// This used to be window.open(url, "_blank"). In an installed PWA there is
// no browser chrome, and iOS in particular ignores "_blank" and navigates
// the standalone window itself -- so the PDF filled the app window with no
// address bar and no back button, and the only way out was to kill and
// reopen the app. Fetching to a blob and clicking a synthetic <a download>
// keeps the current page put: nothing navigates, so there is nothing to
// navigate back from. window.open is kept only as a last-resort fallback
// for a browser that refuses the blob path.
async function downloadExport(url, filename) {
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    saveBlob(await res.blob(), filename);
    announce(tx("Download started."));
  } catch (err) {
    announceError(tx("Could not prepare the download; opening it instead."));
    // A plain navigation can't carry the X-App-Lang header.
    window.open(`${url}${url.includes("?") ? "&" : "?"}lang=${LANG}`, "_blank", "noopener");
  }
}

// Backups go through a blob too, for the same reason as downloadExport().
//
// They used to be a plain <a download> pointing at the endpoint, to avoid
// holding the whole archive in memory. The assumption was that the download
// attribute alone would keep an installed PWA from navigating. On iOS it
// doesn't: the standalone window navigated to the zip's preview page, which
// has no back button, and the only way out was to force-quit the app. A
// blob URL keeps the page where it is. The cost is memory: the archive is
// held in full before it is saved, which is fine for megabytes and gets
// risky on a phone somewhere in the hundreds of megabytes.
function saveBlob(blob, filename) {
  const objectUrl = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = objectUrl;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke late: Safari can still be reading the blob as the click is
  // handled, and revoking immediately produces an empty file.
  setTimeout(() => URL.revokeObjectURL(objectUrl), 60000);
}

// The server names the file (with a timestamp) in Content-Disposition; a
// blob download loses that unless it is read back out explicitly.
function filenameFromResponse(res) {
  const cd = res.headers.get("Content-Disposition") || "";
  const star = cd.match(/filename\*=UTF-8''([^;]+)/i);
  if (star) { try { return decodeURIComponent(star[1]); } catch { /* fall through */ } }
  const plain = cd.match(/filename="?([^";]+)"?/i);
  return plain ? plain[1] : null;
}

function formatBytes(n) {
  const units = LANG === "fr" ? ["o", "Ko", "Mo", "Go"] : ["B", "KB", "MB", "GB"];
  if (!n) return `0 ${units[0]}`;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
  const num = n < 10 && i > 0 ? n.toFixed(1) : String(Math.round(n));
  return `${LANG === "fr" ? num.replace(".", ",") : num}\u00a0${units[i]}`;
}

async function refreshBackupStats() {
  const elStats = $("#backup-stats");
  if (!elStats) return;
  try {
    const res = await fetch(`${API}/backup/info`);
    if (!res.ok) throw new Error(String(res.status));
    const d = await res.json();
    elStats.textContent =
      tx("{1} recipe(s), {2} uploaded file(s) ({3} total).", { 1: d.recipe_count, 2: d.upload_count, 3: formatBytes(d.database_bytes + d.upload_bytes) });
  } catch {
    elStats.textContent = "";
  }
}

function safeFilename(title, ext) {
  const base = String(title || "recipe").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
  return `${base || "recipe"}.${ext}`;
}

// The familiar "box with an arrow leaving the top" share glyph. There is no
// Unicode character for it, so it is inline SVG. It is a fixed literal --
// never built from recipe data -- so the innerHTML path in el() stays free
// of anything user-controlled. stroke="currentColor" means it inherits the
// button's themed colour and works in both light and dark without a second
// asset.
const SHARE_ICON_SVG =
  '<svg class="icon-share" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
  '<path d="M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-7"/>' +
  '<path d="M12 15V3"/>' +
  '<path d="M8 7l4-4 4 4"/>' +
  '</svg>';

// The four share actions, built once and reused by both presentations:
// the dropdown on the recipe detail page and the centred sheet opened from
// a card in the list. onDone() closes whichever container invoked them.
// Print prints the open recipe (the print styles hide the list while one
// is open). From a list card, that recipe is opened first: printing used
// to give the list, or the recipe last viewed (Claude review of 0138).
async function printRecipe(id) {
  if (detailOverlay.hidden || detailRecipeId !== id) await openRecipeDetail(id);
  if (detailRecipeId === id) window.print();
}

function shareActionButtons(recipe, onDone) {
  return [
    el("button", { type: "button", text: tx("Print"), onclick: () => { onDone(); printRecipe(recipe.id); } }),
    el("button", {
      type: "button", text: tx("Download PDF"),
      onclick: () => {
        onDone();
        const includeNotes = recipe.notes ? confirm(tx("Include your notes in the PDF?")) : false;
        const url = `${API}/recipes/${recipe.id}/export.pdf${includeNotes ? "?include_notes=true" : ""}`;
        downloadExport(url, safeFilename(recipe.title, "pdf"));
      },
    }),
    state.mail.canSend ? el("button", {
      type: "button", text: tx("Email PDF"),
      onclick: async () => {
        onDone();
        // Sends from your email account, so it needs the settings password.
        if (!(await ensureUnlocked()) || !(await loadMailDetails())) return;
        openEmailSheet(recipe);
      },
    }) : null,
    el("button", {
      type: "button", text: tx("Download HTML"),
      onclick: () => {
        onDone();
        downloadExport(`${API}/recipes/${recipe.id}/export.html`, safeFilename(recipe.title, "html"));
      },
    }),
    el("button", {
      type: "button", text: tx("Download JSON (schema.org)"),
      onclick: () => {
        onDone();
        downloadExport(`${API}/recipes/${recipe.id}/export.json`, safeFilename(recipe.title, "json"));
      },
    }),
    el("button", {
      type: "button", text: tx("Image (JPG)"),
      onclick: () => { onDone(); openImageSheet(recipe); },
    }),
    el("button", {
      type: "button", text: tx("Copy as text"),
      onclick: async () => {
        onDone();
        const text = `${recipe.title}\n\n${tx("Ingredients:")}\n` +
          recipe.ingredients.map((i) => `- ${ingredientText(i)}`).join("\n") +
          `\n\n${tx("Instructions:")}\n` + recipe.steps.map((s, idx) => `${idx + 1}. ${s.text}`).join("\n");
        try { await navigator.clipboard.writeText(text); announce(tx("Recipe copied as text.")); }
        catch { announceError(tx("Could not copy to clipboard.")); }
      },
    }),
  ];
}

// -------------------------------------------------------------------
// Share -> Image (JPG) (0113). Drawn on a canvas in the browser from the
// recipe as saved (1x): photo, title, servings and times, ingredients,
// instructions, your notes if you tick them in (0114; off by default),
// and where it came from. No server work and nothing to install. JPEG: a fraction of a
// PNG's size once there's a photo, and fine for text at quality 0.9.
// -------------------------------------------------------------------
const IMAGE_WIDTH = 1080;
const IMAGE_MAX_PIXELS = 16_000_000;     // under iOS Safari's canvas limit

function loadImage(src) {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => resolve(null);
    img.src = src;
  });
}

// Lines of text no wider than maxWidth; a word too long for a line is cut.
function wrapText(ctx, text, maxWidth) {
  const lines = [];
  for (const para of String(text || "").split(/\n/)) {
    let line = "";
    for (let word of para.split(/\s+/).filter(Boolean)) {
      while (ctx.measureText(word).width > maxWidth) {
        let cut = word.length - 1;
        while (cut > 1 && ctx.measureText(word.slice(0, cut)).width > maxWidth) cut -= 1;
        if (line) { lines.push(line); line = ""; }
        lines.push(word.slice(0, cut));
        word = word.slice(cut);
      }
      const next = line ? `${line} ${word}` : word;
      if (line && ctx.measureText(next).width > maxWidth) { lines.push(line); line = word; } else line = next;
    }
    lines.push(line);
  }
  return lines;
}

async function recipeImageBlob(recipe, { includeNotes = false } = {}) {
  const css = getComputedStyle(document.documentElement);
  const display = css.getPropertyValue("--font-display").trim() || "serif";
  const body = css.getPropertyValue("--font-body").trim() || "sans-serif";
  const C = { bg: "#FFFFFF", ink: "#22201B", muted: "#6B675C", accent: "#8F6218", rule: "#E4DFD3" };
  const W = IMAGE_WIDTH, PAD = 72, TEXT_W = W - 2 * PAD;
  const photo = recipe.image_path ? await loadImage(`/uploads/${recipe.image_path}`) : null;
  if (document.fonts && document.fonts.ready) await document.fonts.ready;

  // Pass 1 lays out every line on a scratch canvas (for measuring); pass
  // 2 draws them once the height is known.
  const ops = [];
  const measure = document.createElement("canvas").getContext("2d");
  let y = 0;
  const photoH = photo ? Math.round(W * 0.62) : 0;
  if (photo) ops.push({ photo: true, y: 0, h: photoH });
  y = photoH + PAD;
  const block = (text, font, color, size, opts = {}) => {
    measure.font = font;
    const indent = opts.indent || 0;
    wrapText(measure, text, TEXT_W - indent).forEach((line, i) => {
      y += size * (opts.lead || 1.32);
      ops.push({ text: line, font, color, x: PAD + indent, y });
      if (i === 0 && opts.marker) ops.push({ text: opts.marker, font, color: opts.markerColor || color, x: PAD, y });
    });
    y += opts.after || 0;
  };
  block(recipe.title, `bold 64px ${display}`, C.ink, 64, { lead: 1.18, after: 18 });
  const facts = [recipe.servings ? tx("Servings: {1}", { 1: recipe.servings }) : null,
    recipe.prep_time ? `${tx("Prep time")}: ${recipe.prep_time}` : null,
    recipe.cook_time ? `${tx("Cook time")}: ${recipe.cook_time}` : null,
    recipe.total_time ? `${tx("Total time")}: ${recipe.total_time}` : null].filter(Boolean);
  if (facts.length) block(facts.join("  ·  "), `30px ${body}`, C.muted, 30, { after: 10 });

  const heading = (text) => { y += 34; block(text, `bold 42px ${display}`, C.accent, 42, { after: 8 }); };
  if (recipe.ingredients.length) {
    heading(tx("Ingredients"));
    for (const ing of recipe.ingredients) {
      const t = ingredientText(ing);
      if (isPartName(t)) block(t, `bold 32px ${body}`, C.ink, 32, { after: 4 });
      else block(t, `32px ${body}`, C.ink, 32, { indent: 40, marker: "•", markerColor: C.accent, after: 6 });
    }
  }
  if (recipe.steps.length) {
    heading(tx("Instructions"));
    let n = 0;
    for (const step of recipe.steps) {
      if (isPartName(step.text)) { block(step.text, `bold 32px ${body}`, C.ink, 32, { after: 4 }); continue; }
      n += 1;
      block(step.text, `32px ${body}`, C.ink, 32, { indent: 58, marker: `${n}.`, markerColor: C.accent, after: 14 });
    }
  }
  const notes = includeNotes ? (recipe.notes || "").trim() : "";
  if (notes) {
    heading(tx("Notes"));
    block(notes, `italic 30px ${body}`, C.ink, 30, { after: 6 });
  }
  let source = null;
  try { source = recipe.source_url ? new URL(recipe.source_url).host.replace(/^www\./, "") : null; } catch { source = null; }
  y += 40;
  ops.push({ rule: true, y });
  y += 8;
  block(source ? `Open the Pantry · ${source}` : "Open the Pantry", `26px ${body}`, C.muted, 26);
  const H = Math.ceil(y + PAD * 0.75);

  // A very long recipe is drawn smaller rather than not at all.
  const scale = Math.min(1, Math.sqrt(IMAGE_MAX_PIXELS / (W * H)));
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(W * scale);
  canvas.height = Math.floor(H * scale);
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  ctx.fillStyle = C.bg;
  ctx.fillRect(0, 0, W, H);
  ctx.textBaseline = "alphabetic";
  for (const op of ops) {
    if (op.photo) {
      // Cover: fill the band, cropping the photo's longer side.
      const r = Math.max(W / photo.naturalWidth, op.h / photo.naturalHeight);
      const sw = W / r, sh = op.h / r;
      ctx.drawImage(photo, (photo.naturalWidth - sw) / 2, (photo.naturalHeight - sh) / 2, sw, sh, 0, 0, W, op.h);
    } else if (op.rule) {
      ctx.fillStyle = C.rule;
      ctx.fillRect(PAD, op.y, TEXT_W, 2);
    } else {
      ctx.font = op.font;
      ctx.fillStyle = op.color;
      ctx.fillText(op.text, op.x, op.y);
    }
  }
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.9));
}

const imageSheetOverlay = $("#image-sheet-overlay");
$("#image-sheet-close").addEventListener("click", () => closeModal(imageSheetOverlay));
imageSheetOverlay.addEventListener("click", (e) => { if (e.target === imageSheetOverlay) closeModal(imageSheetOverlay); });

async function openImageSheet(recipe) {
  const preview = $("#image-sheet-preview");
  const status = $("#image-sheet-status");
  const shareBtn = $("#image-sheet-share");
  const saveBtn = $("#image-sheet-download");
  const notesRow = $(".image-sheet-notes");
  const notesBox = $("#image-sheet-notes");
  notesRow.hidden = true;
  notesBox.checked = false;
  notesBox.onchange = null;
  status.textContent = "";
  openModal(imageSheetOverlay);
  let url = null;
  let ticket = 0;
  imageSheetOverlay._onClose = () => { ticket += 1; if (url) URL.revokeObjectURL(url); url = null; };

  // Drawn again when "Include my notes" changes; only the latest drawing
  // is shown.
  async function draw() {
    const mine = ++ticket;
    preview.replaceChildren(el("p", { class: "field-hint", text: tx("Preparing…") }));
    shareBtn.hidden = true;
    saveBtn.disabled = true;
    notesBox.disabled = true;
    const blob = await recipeImageBlob(recipe, { includeNotes: notesBox.checked });
    notesBox.disabled = false;
    if (mine !== ticket || imageSheetOverlay.hidden) return;
    if (!blob) throw new Error("no image");
    const name = safeFilename(recipe.title, "jpg");
    const file = new File([blob], name, { type: "image/jpeg" });
    if (url) URL.revokeObjectURL(url);
    url = URL.createObjectURL(blob);
    preview.replaceChildren(el("img", { src: url, alt: tx("Image of {1}", { 1: recipe.title }) }));
    saveBtn.disabled = false;
    saveBtn.onclick = () => {
      const a = el("a", { href: url, download: name });
      document.body.appendChild(a);
      a.click();
      a.remove();
      status.textContent = tx("Download started.");
    };
    // The phone's share sheet (Messages, mail...), where the browser
    // allows sharing files: on a phone, over HTTPS.
    if (navigator.canShare && navigator.canShare({ files: [file] })) {
      shareBtn.hidden = false;
      shareBtn.onclick = async () => {
        try { await navigator.share({ files: [file], title: recipe.title }); }
        catch (err) { if (!err || err.name !== "AbortError") status.textContent = tx("Couldn't open the share sheet; use Download."); }
      };
    }
  }

  try {
    // A card's recipe can be the list's short form, without ingredients.
    if (!Array.isArray(recipe.ingredients) || !Array.isArray(recipe.steps)) {
      preview.replaceChildren(el("p", { class: "field-hint", text: tx("Preparing…") }));
      const res = await fetch(`${API}/recipes/${recipe.id}`);
      if (!res.ok) throw new Error(String(res.status));
      recipe = await res.json();
    }
    if ((recipe.notes || "").trim()) {
      notesRow.hidden = false;
      notesBox.onchange = () => { status.textContent = ""; draw().catch(failed); };
    }
    await draw();
  } catch {
    failed();
  }

  function failed() {
    preview.replaceChildren();
    notesBox.disabled = false;
    status.textContent = tx("Couldn't make the image.");
  }
}

// Share from a recipe card in the list. Cards clip their own overflow, so
// this is a centred modal rather than the detail page's dropdown.
function openShareSheet(recipe) {
  const overlay = $("#share-sheet-overlay");
  $("#share-sheet-title").textContent = recipe.title;
  const actions = $("#share-sheet-actions");
  actions.innerHTML = "";
  // filter(Boolean): Email PDF is null when sending isn't set up. Appending
  // that null threw, and every button after it went missing.
  shareActionButtons(recipe, () => closeModal(overlay)).filter(Boolean).forEach((b) => actions.appendChild(b));
  openModal(overlay);
}

function shareMenu(recipe) {
  const menu = el("div", { class: "share-menu", id: "share-menu", hidden: "" },
    shareActionButtons(recipe, () => { menu.hidden = true; toggleBtn.setAttribute("aria-expanded", "false"); }));
  const toggleBtn = el("button", {
    class: "btn-secondary share-toggle", type: "button", "aria-haspopup": "true",
    "aria-expanded": "false", "aria-label": tx("Share recipe"),
    trustedStaticHtml: SHARE_ICON_SVG,
    onclick: () => {
      const willOpen = menu.hidden;
      menu.hidden = !willOpen;
      toggleBtn.setAttribute("aria-expanded", String(willOpen));
    },
  }, [el("span", { text: tx("Share") })]);  // label as text; only the fixed icon goes in as markup
  return el("div", { class: "share-menu-wrap" }, [toggleBtn, menu]);
}

// How long to wait before giving up on a write. Long enough that a slow
// NAS or a sluggish VPN hop isn't cut off mid-request, short enough that
// you aren't staring at a dead button wondering.
const WRITE_TIMEOUT_MS = 10000;

/** fetch with a timeout. Returns {ok, status, timedOut, networkError}. */
async function fetchWithTimeout(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), WRITE_TIMEOUT_MS);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    return { ok: res.ok, status: res.status, res };
  } catch (err) {
    // AbortError is our own timeout; anything else is the network.
    if (err && err.name === "AbortError") return { ok: false, timedOut: true };
    return { ok: false, networkError: true };
  } finally {
    clearTimeout(timer);
  }
}

/** Turns a failed request into something that says WHY, not just "failed". */
function writeFailureMessage(result, subject) {
  if (result.timedOut) {
    return tx("The server didn't respond within {1} seconds. {2} may or may not have gone through — the list has been refreshed so you can check.",
      { 1: WRITE_TIMEOUT_MS / 1000, 2: subject });
  }
  if (result.networkError) return tx("Couldn't reach Open the Pantry. Check your connection and try again.");
  switch (result.status) {
    case 401:
    case 403: return tx("Not authorised — the API key is missing or wrong.");
    case 404: return tx("{1} was already gone. The list has been refreshed.", { 1: subject });
    case 429: return tx("Too many requests just now. Wait a moment and try again.");
    default:
      return result.status >= 500
        ? tx("The server returned an error ({1}). Nothing was changed.", { 1: result.status })
        : tx("That didn't work (status {1}).", { 1: result.status });
  }
}

// A copy to make a variant from (backend: duplicate_recipe). Opens in
// Edit, titled "… (copy)", so it can be renamed and changed right away.
async function duplicateRecipe(recipe, btn) {
  btn.disabled = true;
  const result = await fetchWithTimeout(`${API}/recipes/${recipe.id}/duplicate`, { method: "POST" });
  btn.disabled = false;
  if (!result.ok) {
    if (result.timedOut) loadRecipes();
    announceError(writeFailureMessage(result, tx("The copy")));
    return;
  }
  const copy = await result.res.json();
  announce(tx("Copy made: {1}. Change it here, then save.", { 1: copy.title }));
  renderRecipeEditForm(copy);
  $("#recipe-detail-modal").scrollTop = 0;
  await loadTags();
  loadRecipes();
}

async function deleteRecipe(id, btn, label) {
  if (!confirm(tx("Delete this recipe? You can put it back from Recently deleted for 30 days."))) return;

  // Guard against a double-fire, but deliberately do NOT touch the modal's
  // close button or the Escape handler: the modal now stays open until the
  // request resolves, so those are the only way out if this hangs.
  if (btn) { btn.disabled = true; btn.textContent = tx("Deleting…"); }
  const restore = () => { if (btn) { btn.disabled = false; btn.textContent = label || tx("Delete recipe"); } };

  const result = await fetchWithTimeout(`${API}/recipes/${id}`, { method: "DELETE" });
  if (result.ok || result.status === 404) forgetTicks(id);

  if (result.ok) {
    closeModal(detailOverlay);
    announce(tx("Moved to Recently deleted (in the side menu) for 30 days."));
    await loadTimeBuckets();
    loadRecipes();
    return;
  }

  // A 404 means it is not there any more, which is the outcome that was
  // wanted -- close and refresh rather than leaving the user staring at a
  // recipe that no longer exists.
  if (result.status === 404) {
    closeModal(detailOverlay);
    // announce AFTER the refresh: loadRecipes() announces its own
    // "N recipes found", which would otherwise immediately overwrite the
    // explanation and leave the user with no idea what happened.
    await loadRecipes();
    announceError(writeFailureMessage(result, tx("That recipe")));
    return;
  }

  // Everything else: stay put, say what happened, let them retry. On a
  // timeout the outcome is genuinely unknown, so refresh the list behind
  // the modal rather than claiming either result -- again announcing last.
  restore();
  if (result.timedOut) await loadRecipes();
  announceError(writeFailureMessage(result, tx("The recipe")));
}

// Add a recipe (add_recipe.js).
initAddRecipe();

// -------------------------------------------------------------------
// Pull to refresh (a home-screen app on iOS has none of its own)
// -------------------------------------------------------------------
// Pulling down from the top of the recipe list reloads the recipes and
// tags. If the server has a newer version of the app (GET /api/version
// fingerprints its files), the page reloads into it instead.
let loadedVersion = null;
fetch(`${API}/version`, { cache: "no-store" }).then((r) => r.ok ? r.json() : null)
  .then((v) => { loadedVersion = v && v.frontend; }).catch(() => {});

const PULL_TRIGGER = 70;   // px of pull needed to refresh
const pullEl = el("div", { class: "pull-indicator", "aria-hidden": "true" }, [el("span", {})]);
document.body.appendChild(pullEl);
let pullStartY = null, pullDist = 0, refreshing = false;

function pullAllowed() {
  if (refreshing || window.scrollY > 0) return false;
  if ($$(".modal-overlay").some((o) => !o.hidden)) return false;     // Settings, a recipe, add...
  if (isDrawer() && sidebarOpen()) return false;                     // tag drawer
  return true;
}
function setPullUI(dist, label) {
  pullEl.style.transform = `translate(-50%, ${Math.min(dist, PULL_TRIGGER * 1.4) - 50}px)`;
  pullEl.classList.toggle("visible", dist > 8);
  pullEl.firstChild.textContent = label;
}
document.addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1 || !pullAllowed()) { pullStartY = null; return; }
  // Not from inside something that scrolls on its own.
  if (e.target.closest(".modal, #sidebar, textarea, select")) { pullStartY = null; return; }
  pullStartY = e.touches[0].clientY; pullDist = 0;
}, { passive: true });
document.addEventListener("touchmove", (e) => {
  if (pullStartY == null) return;
  pullDist = (e.touches[0].clientY - pullStartY) * 0.5;   // resistance
  if (pullDist <= 0 || window.scrollY > 0) { pullDist = 0; setPullUI(0, ""); return; }
  setPullUI(pullDist, pullDist >= PULL_TRIGGER ? tx("Release to refresh") : tx("Pull to refresh"));
}, { passive: true });
document.addEventListener("touchend", async () => {
  if (pullStartY == null) return;
  pullStartY = null;
  if (pullDist < PULL_TRIGGER) { setPullUI(0, ""); return; }
  await pullRefresh();
});

async function pullRefresh() {
  refreshing = true;
  setPullUI(PULL_TRIGGER, tx("Checking for updates…"));
  pullEl.classList.add("busy");
  try {
    const reg = "serviceWorker" in navigator ? await navigator.serviceWorker.getRegistration() : null;
    if (reg) reg.update().catch(() => {});
    const v = await fetch(`${API}/version`, { cache: "no-store" }).then((r) => r.ok ? r.json() : null).catch(() => null);
    if (v && loadedVersion && v.frontend !== loadedVersion) {
      setPullUI(PULL_TRIGGER, tx("Updating…"));
      window.location.reload();
      return;
    }
    await loadTags();
    await loadTimeBuckets();
    await loadRecipes();
    announce(tx("Refreshed."));
  } finally {
    refreshing = false;
    pullEl.classList.remove("busy");
    setPullUI(0, "");
  }
}

// -------------------------------------------------------------------
// Init
// -------------------------------------------------------------------
(async function init() {
  $("#filters-toggle").hidden = state.grouping !== "all";
  await loadTags();
  // Opened from a link to one recipe (What to eat?): show it now, the
  // list loads behind it.
  openRecipeFromHash();
  openSharedLink();
  await loadTimeBuckets();
  await loadRecipes();
  await loadMailState();
  refreshHttpsSummary();
  refreshPickerLink();
})();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  });
}

// What add_recipe.js uses from here (besides what settings.js does).
export {
  topOverlay, tagsFromInput, linesOf, ingredientsOf, discardDraftFile, loadTimeBuckets, loadRecipes, tagSourcesNote,
  ingredientText, makeDurationInput, openRecipeDetail, partHint,
};

// What settings.js uses from here.
export {
  API, tx, txn, fmtDateTime, tagLabel, $, $$, announce, announceError, el, openModal, closeModal, storageSet,
  jsonBody, refreshLibrary, settingsOverlay, setMailState, loadMailState, recentRecipientsEditor, state,
  currentRecipes, apiErrorText, responseErrorText, renderRecipeList, detailOverlay, detailRecipeId, detailRecipe,
  renderRecipeDetail, saveBlob, filenameFromResponse, formatBytes, refreshBackupStats, fetchWithTimeout,
  writeFailureMessage,
};
