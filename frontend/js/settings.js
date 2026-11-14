// Settings panels (0188, moved from app.js unchanged): Backup and
// Restore; the settings password and what it guards (email, HTTPS, Logs);
// Learning, What to eat? and the display switches.
//
// Each part's functions, element lookups and constants are at the top
// level here. Its setup steps (listeners, first values) are in an init
// function that app.js calls where the code used to be, so they run at the
// same point as before. Elements are looked up as this module loads, once
// the page is parsed; app.js's helpers used here are imported from it.
import {
  API, tx, txn, fmtDateTime, tagLabel, $, $$, announce, announceError, el, openModal, closeModal, storageSet,
  jsonBody, refreshLibrary, settingsOverlay, setMailState, loadMailState, recentRecipientsEditor, state,
  currentRecipes, apiErrorText, responseErrorText, renderRecipeList, detailOverlay, detailRecipeId, detailRecipe,
  renderRecipeDetail, saveBlob, filenameFromResponse, formatBytes, refreshBackupStats, fetchWithTimeout,
  writeFailureMessage,
} from "./app.js";
import { savedResultRow, renderBatchResults, modeSwitchControl } from "./add_recipe.js";

// One chooser, one Download button. There used to be two separate buttons
// with the explanation in small print underneath, which read as "a backup
// button" plus something else rather than as a choice of format.
const backupDownloadBtn = $("#backup-download-btn");
const backupNotesOption = document.querySelector(".backup-notes-option");
const selectedBackupFormat = () =>
  (document.querySelector('input[name="backup-format"]:checked') || {}).value || "database";

// Settings -> Backup & export -> Restore or import. A full backup
// (backend: restore.py) is checked first and replaces the library only
// after "Replace my library" (behind the settings password); anything else
// goes to the import (interchange.py, POST /api/import/recipes), which
// adds and reports per recipe like the batches.
const importBtn = $("#import-btn");
const restoreConfirm = $("#restore-confirm");
let pendingRestore = null;

// Settings -> Backup & export -> Automatic backups (scheduled_backup.py).
// Changing them or "Back up now" needs the settings password; the status
// is open.
const autoBackup = {
  enabled: $("#auto-backup-enabled"), hour: $("#auto-backup-hour"), keep: $("#auto-backup-keep"),
  run: $("#auto-backup-run"), status: $("#auto-backup-status"), folder: $("#auto-backup-folder"),
};
function renderAutoBackup(st) {
  autoBackup.enabled.checked = !!st.enabled;
  autoBackup.hour.value = String(st.hour);
  if (![...autoBackup.keep.options].some((o) => o.value === String(st.keep))) {
    autoBackup.keep.appendChild(el("option", { value: String(st.keep), text: txn(st.keep, "{n} backup", "{n} backups") }));
  }
  autoBackup.keep.value = String(st.keep);
  autoBackup.hour.disabled = autoBackup.keep.disabled = !st.enabled;
  autoBackup.folder.textContent = st.folder_in_data
    ? tx("In {1}, on the same disk as your data (README: Automatic backups).", { 1: st.folder })
    : tx("In {1}.", { 1: st.folder });
  const parts = [];
  if (st.running) parts.push(tx("A backup is being made now."));
  else if (!st.last_run_at) parts.push(tx("No automatic backup yet."));
  else if (st.last_ok) parts.push(tx("Last: {1}.", { 1: fmtDateTime(st.last_run_at) }));
  else parts.push(tx("Last one failed ({1}): {2}", { 1: fmtDateTime(st.last_run_at), 2: st.last_message || "" }));
  if (st.backups.length) {
    const total = st.backups.reduce((a, b) => a + b.size, 0);
    parts.push(txn(st.backups.length, "{n} backup kept ({1}).", "{n} backups kept ({1}).", { 1: formatBytes(total) }));
  }
  autoBackup.status.textContent = parts.join(" ");
}
async function refreshAutoBackup() {
  const res = await fetch(`${API}/backups/status`).catch(() => null);
  if (res && res.ok) renderAutoBackup(await res.json());
}
async function saveAutoBackup() {
  const res = await adminFetch(`${API}/backups/schedule`, jsonBody("PUT", { enabled: autoBackup.enabled.checked, hour: Number(autoBackup.hour.value), keep: Number(autoBackup.keep.value) })).catch(() => null);
  if (res && res.ok) {
    const st = await res.json();
    renderAutoBackup(st);
    announce(st.enabled ? tx("Automatic backups on.") : tx("Automatic backups off."));
    return;
  }
  const body = res ? await res.json().catch(() => ({})) : {};
  if (res && !body.locked) announceError(apiErrorText(body, res.status));
  refreshAutoBackup();          // back to what's saved
}

// Settings -> Backup and Restore: the download, restore and automatic
// backups, and opening and closing the dialog.
export function initBackupPanel() {
  document.querySelectorAll('input[name="backup-format"]').forEach((r) =>
    r.addEventListener("change", () => { backupNotesOption.hidden = selectedBackupFormat() !== "pdfs"; }));

  if (backupDownloadBtn) {
    backupDownloadBtn.addEventListener("click", async () => {
      const status = $("#backup-status");
      const format = selectedBackupFormat();
      let url, fallbackName;
      if (format === "pdfs") {
        const includeNotes = $("#backup-pdf-notes").checked;
        url = `${API}/backup/pdfs.zip?include_notes=${includeNotes}`;
        fallbackName = "open-the-pantry-pdfs.zip";
        status.textContent = tx("Building a PDF of every recipe. This can take a while.");
      } else if (format === "json") {
        url = `${API}/backup/recipes-json.zip`;
        fallbackName = "open-the-pantry-recipes-json.zip";
        status.textContent = tx("Preparing your recipes as JSON…");
      } else {
        url = `${API}/backup/database.zip`;
        fallbackName = "open-the-pantry-backup.zip";
        status.textContent = tx("Preparing your backup…");
      }
      backupDownloadBtn.disabled = true;
      const original = backupDownloadBtn.textContent;
      backupDownloadBtn.textContent = tx("Preparing…");
      try {
        const res = await fetch(url);
        if (!res.ok) {
          status.textContent = tx("The server couldn't build the file (error {1}). Nothing was downloaded.", { 1: res.status });
          return;
        }
        const blob = await res.blob();
        saveBlob(blob, filenameFromResponse(res) || fallbackName);
        status.textContent = tx("Ready: {1}. Check your downloads.", { 1: formatBytes(blob.size) });
      } catch {
        status.textContent = tx("Couldn't reach Open the Pantry, so nothing was downloaded. Check your connection and try again.");
      } finally {
        backupDownloadBtn.disabled = false;
        backupDownloadBtn.textContent = original;
      }
    });
  }
  if (importBtn) {
    $("#restore-cancel").addEventListener("click", () => {
      pendingRestore = null;
      restoreConfirm.hidden = true;
      $("#import-status").textContent = tx("Nothing was changed.");
      importBtn.focus();
    });
    $("#restore-go").addEventListener("click", async () => {
      if (!pendingRestore) return;
      const status = $("#import-status");
      const go = $("#restore-go");
      go.disabled = true;
      status.textContent = tx("Restoring… The app pauses until it's done.");
      try {
        const res = await adminFetch(`${API}/backup/restore`, jsonBody("POST", { token: pendingRestore.token }));
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { status.textContent = apiErrorText(data, res.status); return; }
        restoreConfirm.hidden = true;
        pendingRestore = null;
        let msg = tx("Restored. Recipes: {1}; photos and PDFs: {2}. What was here before is saved as {3} in the data folder.",
          { 1: data.recipes, 2: data.files, 3: data.safety_backup });
        if (data.email_password_cleared) msg += " " + tx("Enter the email password again in Settings → Email ingest: the one in the backup can't be read on this server.");
        status.textContent = msg;
        announce(msg);
        await refreshLibrary(); refreshBackupStats(); refreshPickerLink();
      } catch {
        status.textContent = tx("Couldn't reach Open the Pantry. Check your connection and try again.");
      } finally {
        go.disabled = false;
      }
    });

    importBtn.addEventListener("click", async () => {
      const input = $("#import-file");
      const status = $("#import-status");
      const results = $("#import-results");
      const file = input.files && input.files[0];
      results.replaceChildren();
      restoreConfirm.hidden = true;
      pendingRestore = null;
      if (!file) { status.textContent = tx("Choose a file first."); input.focus(); return; }
      importBtn.disabled = true;
      // A .zip may be a full backup: ask the server what it is first.
      if (/\.zip$/i.test(file.name)) {
        status.textContent = tx("Checking {1}…", { 1: file.name });
        try {
          const body = new FormData();
          body.append("file", file);
          // A full backup needs the settings password even to be checked
          // (the server answers 401 locked); adminFetch asks for it and
          // sends the file again. Anything else is checked without it.
          // When the zip's own list of files shows a recipes.db, the
          // password is asked for first, so a large backup is sent once,
          // not twice (DeepSeek review of 0143).
          if (await zipHasEntry(file, "recipes.db")) {
            const st = await adminStatus().catch(() => null);
            if (st && !st.unlocked && !st.https_url) {
              const ok = !damagedPasswordFile(st) && await settingsPasswordDialog(st.password_set ? "unlock" : "create", Boolean(st.reset));
              if (!ok) { status.textContent = ""; importBtn.disabled = false; return; }
            }
          }
          const res = await adminFetch(`${API}/backup/restore/check`, { method: "POST", body });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) { status.textContent = apiErrorText(data, res.status); importBtn.disabled = false; return; }
          if (data.kind === "backup") {
            pendingRestore = data;
            const when = data.created_at ? fmtDateTime(data.created_at) : tx("an unknown date");
            $("#restore-summary").textContent = tx(
              "This is a full backup from {1}. Recipes: {2}; photos and PDFs: {3}. Restoring replaces your whole library (recipes here now: {4}) with it. A safety copy of your current library is saved first.",
              { 1: when, 2: data.recipes, 3: data.files, 4: data.current_recipes });
            restoreConfirm.hidden = false;
            status.textContent = "";
            $("#restore-cancel").focus();
            importBtn.disabled = false;
            return;
          }
        } catch {
          status.textContent = tx("Couldn't reach Open the Pantry. Check your connection and try again.");
          importBtn.disabled = false;
          return;
        }
      }
      if (file.size > 1024 * 1024 * 1024) { status.textContent = tx("That file is larger than 1 GB."); importBtn.disabled = false; return; }
      status.textContent = tx("Importing {1}…", { 1: file.name });
      try {
        const body = new FormData();
        body.append("file", file);
        const res = await fetch(`${API}/import/recipes`, { method: "POST", body });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          status.textContent = apiErrorText(data, res.status);
          return;
        }
        status.textContent = tx("{1} added, {2} skipped, {3} failed.",
          { 1: data.succeeded.length, 2: data.skipped.length, 3: data.failed.length });
        renderBatchResults(results, data);
        announce(status.textContent);
        if (data.succeeded.length) { await refreshLibrary(); refreshBackupStats(); }
      } catch {
        status.textContent = tx("Couldn't reach Open the Pantry. Check your connection and try again.");
      } finally {
        importBtn.disabled = false;
      }
    });
  }

  $("#settings-toggle").addEventListener("click", () => { refreshBackupStats(); refreshAutoBackup(); refreshAdminSection(); refreshLearning(); refreshPicker(); openModal(settingsOverlay); });
  for (let h = 0; h < 24; h++) {
    autoBackup.hour.appendChild(el("option", { value: String(h), text: tx("{1}:00", { 1: String(h).padStart(2, "0") }) }));
  }
  for (const n of [1, 3, 7, 14, 30]) {
    autoBackup.keep.appendChild(el("option", { value: String(n), text: txn(n, "{n} backup", "{n} backups") }));
  }
  autoBackup.enabled.addEventListener("change", saveAutoBackup);
  autoBackup.hour.addEventListener("change", saveAutoBackup);
  autoBackup.keep.addEventListener("change", saveAutoBackup);
  autoBackup.run.addEventListener("click", async () => {
    autoBackup.run.disabled = true;
    autoBackup.status.textContent = tx("A backup is being made now.");
    const res = await adminFetch(`${API}/backups/run`, { method: "POST" }).catch(() => null);
    autoBackup.run.disabled = false;
    if (res && res.ok) {
      renderAutoBackup(await res.json());
      announce(tx("Backup made."));
      return;
    }
    const body = res ? await res.json().catch(() => ({})) : {};
    if (res && !body.locked) announceError(res ? apiErrorText(body, res.status) : tx("Couldn't reach Open the Pantry. Check your connection and try again."));
    refreshAutoBackup();
  });
  $("#settings-close").addEventListener("click", () => closeModal(settingsOverlay));
  settingsOverlay.addEventListener("click", (e) => { if (e.target === settingsOverlay) closeModal(settingsOverlay); });
}

// -------------------------------------------------------------------
// Settings password (backend: admin_lock.py)
// -------------------------------------------------------------------
// Email ingest, HTTPS, Logs and Share -> Email PDF use the email account
// or the DNS login, so they need the settings password; the rest of the
// app has none. The password is created here the first time it's needed.
// Entering it unlocks this browser for 15 minutes (an HttpOnly cookie the
// page never sees; the server keeps the clock).
const adminOverlay = $("#admin-overlay");

// Whether a .zip's central directory (the list at its end) names `entry`
// at its top level. Reads only the end of the file: the last 64 KB for
// the end record, then the directory itself. false when unsure (not a
// zip, a zip64 archive, a directory larger than 8 MB); the caller then
// goes on as before.
async function zipHasEntry(file, entry) {
  try {
    const tailLen = Math.min(file.size, 65557);
    const tail = new DataView(await file.slice(file.size - tailLen).arrayBuffer());
    let eocd = -1;
    for (let i = tailLen - 22; i >= 0; i--) {
      if (tail.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) return false;
    const cdSize = tail.getUint32(eocd + 12, true);
    const cdOffset = tail.getUint32(eocd + 16, true);
    if (cdOffset === 0xffffffff || cdSize > 8 * 1024 * 1024 || cdOffset + cdSize > file.size) return false;
    const cd = new DataView(await file.slice(cdOffset, cdOffset + cdSize).arrayBuffer());
    const decoder = new TextDecoder();
    for (let p = 0; p + 46 <= cd.byteLength && cd.getUint32(p, true) === 0x02014b50;) {
      const nameLen = cd.getUint16(p + 28, true);
      const extra = cd.getUint16(p + 30, true);
      const comment = cd.getUint16(p + 32, true);
      const name = decoder.decode(new Uint8Array(cd.buffer, p + 46, nameLen));
      if (name === entry) return true;
      p += 46 + nameLen + extra + comment;
    }
  } catch { /* unreadable: treated as not a backup */ }
  return false;
}

async function adminStatus() {
  const res = await fetch(`${API}/admin/status`);
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

// mode: "create" (first use), "unlock", or "change". Resolves true once
// the server has unlocked this browser, false if the dialog is closed.
// reset: the password file was deleted; creating a new password clears
// the saved email password and HTTPS (the server does it).
function settingsPasswordDialog(mode, reset = false) {
  return new Promise((resolve) => {
    const body = $("#admin-body");
    body.innerHTML = "";
    $("#admin-heading").textContent = mode === "change" ? tx("Change settings password") : tx("Settings password");
    const status = el("p", { class: "field-hint admin-status", role: "status" });
    const field = (id, label, autocomplete) => {
      const input = el("input", { type: "password", id, autocomplete, required: "", maxlength: "256" });
      return [input, el("div", { class: "field" }, [el("label", { for: id, text: label }), input])];
    };
    const [cur, curField] = field("admin-current", mode === "unlock" ? tx("Password") : tx("Current password"), "current-password");
    const [pw, pwField] = field("admin-new", mode === "change" ? tx("New password") : tx("Password"), "new-password");
    const [pw2, pw2Field] = field("admin-confirm", tx("Type it again"), "new-password");

    const intro = mode === "create" && reset
      ? tx("The settings password was reset. Creating a new one clears the saved email password and cPanel token and turns HTTPS off (its certificate is deleted); you then set both up again. Recipes and everything else are kept.")
      : mode === "create"
      ? tx("Email ingest, HTTPS, Logs and Email PDF use your email account or DNS login, so they're behind a password. Create it now. Everything else in the app stays open.")
      : mode === "unlock"
        ? tx("Email ingest, HTTPS, Logs and Email PDF are locked. Enter the settings password to unlock them on this device for 15 minutes.")
        : tx("Every other device that's unlocked gets locked.");
    body.appendChild(el("p", { class: "field-hint", text: intro }));
    const fields = mode === "create" ? [pwField, pw2Field] : mode === "unlock" ? [curField] : [curField, pwField, pw2Field];
    const submit = el("button", { class: "btn-primary", type: "submit",
      text: mode === "create" ? tx("Create password") : mode === "unlock" ? tx("Unlock") : tx("Change password") });
    const form = el("form", { class: "admin-form" }, [...fields, submit, status]);
    form.appendChild(el("p", { class: "field-hint", text: mode === "unlock"
      ? tx("Forgot it? Delete admin-password.json in the app's data folder. Creating a new password then clears the saved email password and HTTPS, which you set up again.")
      : tx("At least 8 characters. Forgot it? Delete admin-password.json in the app's data folder. Creating a new password then clears the saved email password and HTTPS, which you set up again.") }));
    body.appendChild(form);

    let settled = false;
    const finish = (ok) => { if (!settled) { settled = true; resolve(ok); } };
    adminOverlay._onClose = () => finish(false);

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (mode !== "unlock" && pw.value !== pw2.value) { status.textContent = tx("The two passwords don't match."); pw2.focus(); return; }
      if (mode !== "unlock" && pw.value.length < 8) { status.textContent = tx("The password must be at least 8 characters."); pw.focus(); return; }
      const [url, payload] = mode === "create" ? [`${API}/admin/password`, { password: pw.value }]
        : mode === "unlock" ? [`${API}/admin/unlock`, { password: cur.value }]
          : [`${API}/admin/password/change`, { current_password: cur.value, new_password: pw.value }];
      submit.disabled = true;
      status.textContent = "";
      try {
        const res = await fetch(url, jsonBody("POST", payload));
        if (res.ok) {
          adminOverlay._onClose = null;
          closeModal(adminOverlay);
          refreshAdminSection();
          if (mode === "change") announce(tx("Settings password changed."));
          if (mode === "create" && reset) {
            await loadMailState();   // Email PDF is gone until the email password is entered again
            httpsSummary.textContent = HTTPS_HINT();
            announce(tx("New password set. The email password was cleared and HTTPS turned off; set them up again."));
          }
          finish(true);
          return;
        }
        const err = await res.json().catch(() => ({}));
        if (res.status === 403 && err.https_url) {
          // HTTPS was turned on since this dialog opened: offer the link.
          adminOverlay._onClose = null;
          closeModal(adminOverlay);
          finish(await httpsOnlyDialog(err.https_url));
          return;
        }
        status.textContent = apiErrorText(err, res.status);
        (mode === "create" ? pw : cur).select();
      } catch {
        status.textContent = tx("Couldn't reach Open the Pantry. Check your connection and try again.");
      } finally {
        submit.disabled = false;
      }
    });
    openModal(adminOverlay);
    (mode === "create" ? pw : cur).focus();
  });
}

// Once HTTPS is on, the server refuses the password and the locked
// sections over plain http:// (they could be read on the network there).
// Instead of asking for the password, link to the secure address.
function httpsOnlyDialog(url) {
  return new Promise((resolve) => {
    const body = $("#admin-body");
    body.innerHTML = "";
    $("#admin-heading").textContent = tx("Settings password");
    body.appendChild(el("p", { class: "field-hint", text: tx("HTTPS is on, so Email ingest, HTTPS, Logs and Email PDF open only at the secure address. Over this plain http:// address the password could be read on the network.") }));
    body.appendChild(el("a", { class: "btn-primary", href: url, text: tx("Open {1}", { 1: url }) }));
    adminOverlay._onClose = () => resolve(false);
    openModal(adminOverlay);
  });
}

// True once this browser is unlocked, asking for (or creating) the
// password if needed. False if the person closes the dialog.
async function ensureUnlocked() {
  let st;
  try { st = await adminStatus(); } catch {
    announceError(tx("Couldn't reach Open the Pantry. Check your connection and try again."));
    return false;
  }
  if (st.https_url) return httpsOnlyDialog(st.https_url);
  if (st.unlocked) return true;
  if (damagedPasswordFile(st)) return false;
  return settingsPasswordDialog(st.password_set ? "unlock" : "create", Boolean(st.reset));
}

// A damaged admin-password.json: neither unlock nor create works until
// it's deleted (0159), so the message instead of a password box that
// would only refuse (DeepSeek review of 0159).
const DAMAGED_PASSWORD_FILE = "The settings password file (admin-password.json) is damaged. Delete it from the data folder to set a new password; the email password and HTTPS are then cleared, as after any reset.";
function damagedPasswordFile(st) {
  if (!st || !st.damaged) return false;
  announceError(tx(DAMAGED_PASSWORD_FILE));
  return true;
}

// fetch() for a locked route. If the 15 minutes ran out since the panel
// was opened, asks for the password and sends the request again.
async function adminFetch(url, options = {}, { prompt = true } = {}) {
  const res = await fetch(url, options);
  if (res.status === 403) {
    const b = await res.clone().json().catch(() => ({}));
    if (b.https_url && prompt) await httpsOnlyDialog(b.https_url);
    return res;
  }
  if (res.status !== 401) return res;
  const body = await res.clone().json().catch(() => ({}));
  if (!body.locked || !prompt) return res;
  if (damagedPasswordFile(body)) return res;
  const ok = await settingsPasswordDialog(body.password_set ? "unlock" : "create", Boolean(body.reset));
  return ok ? fetch(url, options) : res;
}

// Settings -> Settings password: the state, Lock now, Change password.
let adminTicker = null;
async function refreshAdminSection() {
  clearTimeout(adminTicker);
  const statusEl = $("#admin-lock-status");
  const actions = $("#admin-lock-actions");
  let st;
  try { st = await adminStatus(); } catch { return; }
  actions.hidden = !st.password_set || Boolean(st.https_url);
  $("#admin-lock-now").hidden = !st.unlocked;
  if (st.https_url) {
    statusEl.textContent = tx("HTTPS is on: these open only at {1}", { 1: st.https_url });
  } else if (st.damaged) {
    statusEl.textContent = tx(DAMAGED_PASSWORD_FILE);
    actions.hidden = true;
  } else if (!st.password_set && st.reset) {
    statusEl.textContent = tx("Reset. Creating a new password clears the saved email password and cPanel token and turns HTTPS off.");
  } else if (!st.password_set) {
    statusEl.textContent = tx("Not set yet. You'll create it the first time you open one of them.");
  } else if (st.unlocked) {
    const min = Math.max(1, Math.ceil(st.seconds_left / 60));
    statusEl.textContent = txn(min, "Unlocked on this device for {n} more minute.", "Unlocked on this device for {n} more minutes.");
    adminTicker = setTimeout(() => { if (!settingsOverlay.hidden) refreshAdminSection(); }, 30000);
  } else {
    statusEl.textContent = tx("Locked.");
  }
}

// -------------------------------------------------------------------
// Email ingest settings (optional feature, collapsed by default)
// -------------------------------------------------------------------
const emailPanel = $("#email-settings-panel");
const emailToggle = $("#email-settings-toggle");

// -------------------------------------------------------------------
// Settings -> HTTPS (https_setup.py does the work). The cPanel login comes
// from .env; everything else is set here.
// -------------------------------------------------------------------
const httpsPanel = $("#https-settings-panel");
const httpsToggle = $("#https-settings-toggle");
const httpsSummary = $("#https-summary");
const HTTPS_HINT = () => tx("An https:// address on your network (Let's Encrypt). Needed to keep the screen awake.");
let httpsPollTimer = null;
let httpsLastState = null;

async function fetchHttpsStatus(prompt = true) {
  const res = await adminFetch(`${API}/https`, {}, { prompt });
  if (res.status === 403) {
    const b = await res.json().catch(() => ({}));
    if (b.https_url) { const e = new Error("https"); e.url = b.https_url; throw e; }
  }
  if (!res.ok) throw new Error(res.status === 401 ? "locked" : String(res.status));
  return res.json();
}

function httpsSummaryText(st) {
  if (st.state === "working") return tx("Setting up HTTPS…");
  if (st.enabled && st.url) return tx("On: {1}", { 1: st.url });
  return null;
}

async function refreshHttpsSummary() {
  try {
    // The open summary: on/off and the address, no password needed.
    const res = await fetch(`${API}/https/summary`);
    if (!res.ok) return;
    const text = httpsSummaryText(await res.json());
    if (text) httpsSummary.textContent = text;
  } catch { /* keep the static hint */ }
}

function isIPv4(host) {
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
}

// Step 1 of Settings -> HTTPS: the cPanel login and the one name to set up.
// Saved in the app (token encrypted, never shown again). A value set in
// .env wins and is shown read-only.
function httpsLoginSection(st, keep) {
  const c = st.credentials;
  const fromEnv = new Set(c.from_env || []);
  const wrap = el("div", { class: "setup-callout https-login" });
  wrap.appendChild(el("strong", { text: c.found ? tx("cPanel login") : tx("Step 1: your cPanel login and the name to use") }));
  if (!c.found) {
    wrap.appendChild(el("p", { text: tx("The app creates its DNS record through your domain's cPanel. In cPanel: Security → Manage API Tokens → Create. Give the token an expiry date and, if cPanel offers it, access to DNS only.") }));
  }
  const input = (id, type, value, placeholder, envField) => {
    const i = el("input", { type, id, placeholder, autocomplete: "off", value: keep[id] ?? value ?? "" });
    i.dataset.env = envField;
    if (fromEnv.has(envField)) { i.readOnly = true; i.value = type === "password" ? "" : value || ""; i.placeholder = tx("Set in .env"); }
    return i;
  };
  const userIn = input("https-cp-user", "text", c.username, "your-cpanel-username", "username");
  const baseIn = input("https-cp-base", "url", c.base_url, "https://cpanel.example.com:2083", "base_url");
  const tokenIn = input("https-cp-token", "password", "", c.token_set ? tx("Saved; leave empty to keep it") : tx("Paste the API token"), "token");
  const domainIn = input("https-domain", "text", c.domain || st.domain, "pantry.example.com", "domain");
  const row = (id, label, i, hint) => el("div", { class: "field" }, [
    el("label", { for: id, text: label }), i,
    fromEnv.has(i.dataset.env) ? null : hint ? el("div", { class: "field-hint", text: hint }) : null,
  ]);
  wrap.appendChild(row("https-cp-user", tx("cPanel username"), userIn));
  wrap.appendChild(row("https-cp-base", tx("cPanel address"), baseIn, tx("The address you log in to cPanel at, with its port.")));
  wrap.appendChild(row("https-cp-token", tx("API token"), tokenIn,
    tx("Stored encrypted, and never shown again. Changing the username or address clears it unless you enter it again.")));
  wrap.appendChild(row("https-domain", tx("Name to use"), domainIn,
    tx("The only name this page will create or change, e.g. pantry.example.com. If it isn't in use yet, the app creates its DNS record.")));
  const status = el("p", { class: "field-hint", role: "status" });
  const save = el("button", { class: "btn-primary", type: "button", text: tx("Save login"), onclick: async () => {
    save.disabled = true;
    status.textContent = "";
    const res = await adminFetch(`${API}/https/credentials`, jsonBody("PUT", {
      username: userIn.value.trim(), base_url: baseIn.value.trim(),
      domain: domainIn.value.trim(), token: tokenIn.value.trim() || null,
    })).catch(() => null);
    save.disabled = false;
    if (!res || !res.ok) {
      status.textContent = await responseErrorText(res);
      return;
    }
    announce(tx("cPanel login saved."));
    ["https-cp-user", "https-cp-base", "https-cp-token", "https-domain"].forEach((k) => { delete keep[k]; });
    await renderHttpsSettings();
  } });
  const buttons = [save];
  if ((c.username || c.token_set) && fromEnv.size < 4) {
    buttons.push(el("button", { class: "btn-secondary", type: "button", text: tx("Clear login"), onclick: async () => {
      if (!confirm(tx("Remove the saved cPanel login? HTTPS keeps working until the certificate needs renewing."))) return;
      const res = await adminFetch(`${API}/https/credentials`, { method: "DELETE" }).catch(() => null);
      if (!res || !res.ok) { announceError(tx("Couldn't reach Open the Pantry. Check your connection and try again.")); return; }
      announce(tx("cPanel login removed."));
      await renderHttpsSettings();
    } }));
  }
  wrap.appendChild(el("div", { style: "display:flex;gap:0.5rem;flex-wrap:wrap;" }, buttons));
  wrap.appendChild(status);
  if (!c.found && c.missing && c.missing.length && (c.username || c.token_set || c.domain)) {
    status.textContent = tx("Still needed: {1}", { 1: c.missing.map((m) => ({ CPANEL_USERNAME: tx("cPanel username"),
      CPANEL_TOKEN: tx("API token"), CPANEL_BASE_URL: tx("cPanel address"), PANTRY_DOMAIN: tx("Name to use") })[m] || m).join(", ") });
  }
  return wrap;
}

async function renderHttpsSettings(fromPoll = false) {
  clearTimeout(httpsPollTimer);
  let st;
  try {
    st = await fetchHttpsStatus(!fromPoll);
  } catch (e) {
    httpsPanel.innerHTML = "";
    if (e.message === "https") {
      httpsPanel.appendChild(el("p", { class: "field-hint", text: tx("HTTPS is on. From now on this section opens only at the secure address:") }));
      httpsPanel.appendChild(el("a", { class: "btn-primary", href: e.url, text: tx("Open {1}", { 1: e.url }) }));
      return;
    }
    httpsPanel.appendChild(el("div", { class: "field-hint", text: e.message === "locked"
      ? tx("Locked again after 15 minutes. Close and reopen this section to enter the password.")
      : tx("Could not load the HTTPS settings.") }));
    return;
  }
  const keep = {};  // values typed so far survive a re-render while polling
  // The token is never carried over: a saved form must not send it again.
  $$("input", httpsPanel).forEach((i) => { if (i.type !== "password") keep[i.id] = i.value; });
  httpsPanel.innerHTML = "";
  const summary = httpsSummaryText(st);
  if (summary) httpsSummary.textContent = summary;

  httpsPanel.appendChild(httpsLoginSection(st, keep));

  const disabled = !st.credentials.found || st.state === "working";
  const field = (id, label, input, hint) => el("div", { class: "field" }, [
    el("label", { for: id, text: label }), input, hint ? el("div", { class: "field-hint", text: hint }) : null,
  ]);
  const guessLan = st.lan_address || (isIPv4(location.hostname) ? location.hostname : "");
  const lanIn = el("input", { type: "text", id: "https-lan", placeholder: "192.168.1.20", inputmode: "decimal",
    autocomplete: "off", value: keep["https-lan"] ?? guessLan });
  const emailIn = el("input", { type: "email", id: "https-email", placeholder: "you@example.com",
    autocomplete: "email", value: keep["https-email"] ?? (st.email || state.mail.from || "") });
  const portIn = el("input", { type: "number", id: "https-port", min: "1", max: "65535",
    value: keep["https-port"] ?? String(st.port || 8443) });
  [lanIn, emailIn, portIn].forEach((i) => { i.disabled = disabled; });

  httpsPanel.appendChild(field("https-lan", tx("This server's address on your network"), lanIn,
    tx("Where the name will point: an address on your home network or VPN. Filled in from the address you're using now, if it's a number.")));
  httpsPanel.appendChild(field("https-email", tx("Email for Let's Encrypt"), emailIn,
    tx("Let's Encrypt writes here only if something is wrong with the certificate.")));
  httpsPanel.appendChild(field("https-port", tx("Port"), portIn,
    tx("The port docker-compose.yml publishes for HTTPS (8443 unless you changed it). Only used for the link.")));

  const statusEl = el("div", { class: "https-status", role: "status" });
  if (st.state === "working") {
    statusEl.appendChild(el("p", { class: "https-working", text: st.step || tx("Working...") }));
    statusEl.appendChild(el("p", { class: "field-hint", text: tx("This takes a minute or two. You can close Settings; it carries on.") }));
  } else if (st.enabled && st.url) {
    const p = el("p", {}, [
      el("span", { text: tx("HTTPS is on:") + " " }),
      el("a", { href: st.url, text: st.url, rel: "noopener" }),
    ]);
    statusEl.appendChild(p);
    if (st.cert_expires_at) {
      statusEl.appendChild(el("p", { class: "field-hint", text: tx("Certificate valid until {1}; it renews by itself.", { 1: fmtDateTime(st.cert_expires_at) }) }));
    }
    if (location.protocol !== "https:") {
      statusEl.appendChild(el("p", { class: "field-hint", text: tx("Open the secure address and add it to your Home Screen from there. Language, theme and similar settings are kept per address, so set them once more.") }));
    }
  }
  if (st.last_error && st.state !== "working") {
    statusEl.appendChild(el("p", { class: "https-error", text: (st.enabled ? tx("Last attempt failed:") + " " : "") + st.last_error }));
  }

  const go = el("button", {
    class: "btn-primary", type: "button", disabled: disabled ? "" : null,
    text: st.enabled ? tx("Apply and renew") : tx("Set up HTTPS"),
    onclick: async () => {
      go.disabled = true;
      const res = await adminFetch(`${API}/https/setup`, jsonBody("POST", {
        lan_address: lanIn.value.trim(), email: emailIn.value.trim(), port: parseInt(portIn.value, 10) || 8443,
      })).catch(() => null);
      if (!res || !res.ok) {
        announceError(await responseErrorText(res));
        go.disabled = false;
        return;
      }
      await renderHttpsSettings();
    },
  });
  const buttons = [go];
  if (st.enabled) {
    buttons.push(el("button", {
      class: "btn-secondary", type: "button", text: tx("Turn off HTTPS"), disabled: st.state === "working" ? "" : null,
      onclick: async () => {
        if (!confirm(tx("Turn off HTTPS? The https:// address stops working; the usual address keeps working. The DNS record stays."))) return;
        const res = await adminFetch(`${API}/https`, { method: "DELETE" }).catch(() => null);
        if (!res || !res.ok) { announceError(tx("Couldn't reach Open the Pantry. Check your connection and try again.")); return; }
        announce(tx("HTTPS turned off."));
        httpsSummary.textContent = HTTPS_HINT();
        await renderHttpsSettings();
      },
    }));
  }
  httpsPanel.appendChild(el("div", { style: "display:flex;gap:0.5rem;flex-wrap:wrap;margin-top:0.75rem;" }, buttons));
  httpsPanel.appendChild(statusEl);

  if (httpsLastState === "working" && st.state !== "working") {
    if (st.last_error) announceError(st.last_error);
    else announce(tx("HTTPS is ready."));
  }
  httpsLastState = st.state;
  if (st.state === "working") {
    httpsPollTimer = setTimeout(() => { if (!httpsPanel.hidden) renderHttpsSettings(true); }, 2000);
  }
}

// -------------------------------------------------------------------
// Settings -> Logs: view (newest at the bottom), download, clear.
// -------------------------------------------------------------------
const logsPanel = $("#logs-panel");
const logsToggle = $("#logs-view-toggle");
let logsProblemsOnly = false;

async function renderLogs() {
  logsPanel.innerHTML = "";
  const filter = modeSwitchControl(
    () => { logsProblemsOnly = false; renderLogBody(); },
    () => { logsProblemsOnly = true; renderLogBody(); },
    [tx("Everything"), tx("Warnings and errors")]);
  if (logsProblemsOnly) filter.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", String(i === 1)));
  const refresh = el("button", { type: "button", class: "btn-secondary btn-small", text: tx("Refresh"), onclick: () => renderLogBody() });
  const info = el("div", { class: "field-hint", role: "status" });
  const pre = el("pre", { class: "log-view", tabindex: "0", "aria-label": tx("Log") });
  logsPanel.appendChild(el("div", { class: "logs-toolbar" }, [filter, refresh]));
  logsPanel.appendChild(pre);
  logsPanel.appendChild(info);

  async function renderLogBody() {
    pre.textContent = tx("Loading...");
    try {
      const res = await adminFetch(`${API}/logs?lines=500&problems=${logsProblemsOnly}`);
      if (!res.ok) throw new Error(String(res.status));
      const d = await res.json();
      pre.textContent = d.records.length ? d.records.join("\n")
        : logsProblemsOnly ? tx("No warnings or errors.") : tx("Nothing logged yet.");
      pre.scrollTop = pre.scrollHeight;
      info.textContent = txn(d.records.length, "{n} entry shown. The whole log is {1}; Download has all of it.",
        "Last {n} entries shown. The whole log is {1}; Download has all of it.", { 1: formatBytes(d.bytes) });
    } catch {
      pre.textContent = "";
      info.textContent = tx("Could not load the log.");
    }
  }
  await renderLogBody();
}

// One scan routine for both places it can be started: Settings (next to
// the connection settings, for testing) and the + menu (for everyday use).
// Shows the summary plus one line per email -- the per-email lines carry
// the reason for each failure, and their only other route is the result
// email, which can't arrive when sending is what's broken.
async function runInboxScan(statusEl, button) {
  statusEl.textContent = tx("Checking the inbox\u2026");
  const old = statusEl.nextElementSibling;
  if (old && old.classList.contains("scan-saved")) old.remove();
  if (button) button.disabled = true;
  try {
    const res = await fetch(`${API}/email-settings/scan`, { method: "POST" });
    const body = await res.json();
    if (!res.ok) {
      statusEl.textContent = body.detail || tx("The scan failed (error {1}).", { 1: res.status });
    } else if (body.scanned === 0 && body.messages.length) {
      statusEl.textContent = body.messages.join("\n");
    } else if (body.scanned === 0) {
      statusEl.textContent = tx("No new recipe emails.");
    } else {
      const summary = tx("Scanned {1}: {2} ingested, {3} failed.", { 1: body.scanned, 2: body.succeeded, 3: body.failed });
      statusEl.textContent = [summary, ...(body.messages || [])].join("\n");
      if (body.saved && body.saved.length) {
        // What it added, each with Delete; they're also in "To review".
        const list = el("ul", { class: "batch-results-list scan-saved" }, body.saved.map((r) => savedResultRow(r.recipe_id, r.title)));
        statusEl.after(list);
      }
      if (body.succeeded) await refreshLibrary();
    }
    announce(statusEl.textContent.split("\n")[0]);
  } catch {
    statusEl.textContent = tx("Couldn't reach Open the Pantry to run the scan. Check your connection and try again.");
  } finally {
    if (button) button.disabled = false;
  }
}

function emailField(labelText, input, hint) {
  return el("div", { class: "field" }, [
    el("label", { for: input.id, text: tx(labelText) }),
    input,
    hint ? el("div", { class: "field-hint", text: tx(hint) }) : null,
  ]);
}

async function renderEmailSettings() {
  emailPanel.innerHTML = "";
  emailPanel.appendChild(el("div", { class: "field-hint", text: tx("Loading...") }));

  let settings;
  try {
    const res = await adminFetch(`${API}/email-settings`);
    if (!res.ok) throw new Error(String(res.status));
    settings = await res.json();
  } catch {
    emailPanel.innerHTML = "";
    emailPanel.appendChild(el("div", { class: "field-hint", text: tx("Could not load email settings.") }));
    return;
  }

  emailPanel.innerHTML = "";

  // Step one of setup when no key exists. This used to be a one-line
  // warning naming an environment variable and pointing at the README;
  // the password field stayed enabled, so the first sign of trouble was a
  // failed save. Now the key can be created right here, and the password
  // field stays disabled until it exists, so that failure can't happen.
  if (!settings.encryption_configured) {
    const setupStatus = el("p", { class: "field-hint", role: "status" });
    const box = el("div", { class: "setup-callout" });
    if (settings.encryption_source === "env_invalid") {
      box.appendChild(el("strong", { text: tx("Encryption key is invalid") }));
      box.appendChild(el("p", {
        text: tx("RECIPE_APP_ENCRYPTION_KEY is set in your compose file, but its value isn't a valid key. Fix it or delete that line, then restart the container."),
      }));
    } else if (settings.encryption_source === "file_invalid") {
      // The key file exists but is empty or damaged. "Set up encryption"
      // would refuse to replace it, so it isn't offered here.
      box.appendChild(el("strong", { text: tx("Encryption key file is damaged") }));
      box.appendChild(el("p", {
        text: tx("encryption.key in the data folder doesn't contain a valid key (it may be empty or damaged). Put back the copy it came from. If that's not possible, delete the file and reopen this panel to set up encryption again; the email password then has to be entered again."),
      }));
    } else {
      box.appendChild(el("strong", { text: tx("Step 1: set up encryption") }));
      box.appendChild(el("p", {
        text: tx("Your email password is stored encrypted, so a key has to exist first. This creates one in the app's data folder (encryption.key). If you ever move the data folder, the key goes with it. The backup zip leaves it out, so after restoring from a backup on a new install you re-enter the password."),
      }));
      box.appendChild(el("button", {
        class: "btn-primary", type: "button", text: tx("Set up encryption"),
        onclick: async (e) => {
          const btn = e.currentTarget;
          btn.disabled = true;
          setupStatus.textContent = tx("Creating key\u2026");
          try {
            const res = await adminFetch(`${API}/email-settings/encryption-key`, { method: "POST" });
            if (res.ok || res.status === 409) {
              announce(tx("Encryption is set up. You can now enter the email password."));
              await renderEmailSettings();
              return;
            }
            const err = await res.json().catch(() => ({}));
            setupStatus.textContent = err.detail || tx("Couldn't create the key (error {1}).", { 1: res.status });
          } catch {
            setupStatus.textContent = tx("Couldn't reach Open the Pantry. Check your connection and try again.");
          }
          btn.disabled = false;
        },
      }));
      box.appendChild(setupStatus);
    }
    emailPanel.appendChild(box);
  }

  const enabledInput = el("input", { type: "checkbox", id: "email-enabled" });
  enabledInput.checked = !!settings.enabled;

  const imapHost = el("input", { type: "text", id: "email-imap-host", value: settings.imap_host || "" });
  const imapPort = el("input", { type: "number", id: "email-imap-port", value: String(settings.imap_port ?? 993) });
  const smtpHost = el("input", { type: "text", id: "email-smtp-host", value: settings.smtp_host || "" });
  const smtpPort = el("input", { type: "number", id: "email-smtp-port", value: String(settings.smtp_port ?? 587) });
  const username = el("input", { type: "text", id: "email-username", value: settings.username || "" });
  const password = el("input", {
    type: "password", id: "email-password", autocomplete: "new-password",
    placeholder: !settings.encryption_configured
      ? tx("Set up encryption first (above)")
      : settings.password_set ? tx("Saved \u2014 leave blank to keep") : tx("App password"),
  });
  if (!settings.encryption_configured) password.disabled = true;
  const notifyEmail = el("input", { type: "email", id: "email-notify", value: settings.notify_email || "" });
  const keyword = el("input", { type: "text", id: "email-keyword", value: settings.subject_keyword || "[RECIPE]" });
  const senders = el("input", { type: "text", id: "email-senders", value: settings.allowed_senders || "",
    placeholder: tx("you@example.com, @family.example") });
  const scanHour = el("input", { type: "number", id: "email-scan-hour", min: "0", max: "23", value: String(settings.daily_scan_hour ?? 3) });
  const cooldown = el("input", { type: "number", id: "email-cooldown", min: "0", value: String(settings.cooldown_minutes ?? 30) });

  // pre-line: the test result puts sending and reading on separate lines.
  const statusLine = el("div", { id: "email-status-line", class: "field-hint", role: "status", style: "margin-top:0.5rem;white-space:pre-line;" });

  emailPanel.appendChild(el("div", { class: "field" }, [
    el("label", { for: "email-enabled", style: "display:flex;align-items:center;gap:0.5rem;" }, [
      enabledInput,
      el("span", { text: tx("Enable daily inbox scan") }),
    ]),
  ]));

  // The overnight scan can't email you that emailing is broken; this is
  // where it says so instead.
  if (settings.last_problem || settings.pending_notifications) {
    const lines = [];
    if (settings.last_problem) {
      const when = settings.last_problem_at ? fmtDateTime(settings.last_problem_at) : tx("the last scan");
      lines.push(el("strong", { text: tx("Problem during the scan at {1}", { 1: when }) }));
      lines.push(el("p", { text: settings.last_problem }));
    }
    if (settings.pending_notifications) {
      lines.push(el("p", { text: txn(settings.pending_notifications, "{n} scan result waiting to be emailed.", "{n} scan results waiting to be emailed.") }));
    }
    if (settings.last_problem) {
      lines.push(el("p", { class: "field-hint", text: tx("Send test email checks both halves; this clears once a scan or test goes through.") }));
    }
    emailPanel.appendChild(el("div", { class: "email-problem", role: "status" }, lines));
  }

  emailPanel.appendChild(emailField("IMAP host (reading)", imapHost, "e.g. imap.gmail.com"));
  emailPanel.appendChild(emailField("IMAP port", imapPort,
    "993 connects with TLS from the start (implicit TLS). Any other port starts unencrypted and upgrades via STARTTLS -- that's how port 143 is normally used."));
  emailPanel.appendChild(emailField("SMTP host (notifications)", smtpHost, "e.g. smtp.gmail.com"));
  emailPanel.appendChild(emailField("SMTP port", smtpPort,
    "465 connects with TLS from the start (implicit TLS). Any other port -- typically 587 -- starts unencrypted and upgrades via STARTTLS. Guessed from the port number above; there's no separate setting for it."));
  emailPanel.appendChild(emailField("Username", username, "Used for both IMAP and SMTP."));
  emailPanel.appendChild(emailField("Password", password,
    "Stored encrypted. Use an app-specific password, not your main account password."));
  emailPanel.appendChild(emailField("Send notifications to", notifyEmail));
  emailPanel.appendChild(emailField("Subject keyword", keyword,
    "Only emails whose subject contains this are considered. Everything else is ignored."));
  emailPanel.appendChild(emailField("Only accept email from", senders,
    "Addresses, or a domain (example.com) for everyone there, separated by commas. Leave empty to accept anyone who knows the address and keyword. Recommended: list the addresses you send from."));
  emailPanel.appendChild(emailField("Daily scan hour (0-23)", scanHour,
    settings.server_timezone
      ? tx("In the server's time zone ({1}). If that isn't yours, set TZ in docker-compose.yml.", { 1: settings.server_timezone })
      : tx("In the server's time zone. If that isn't yours, set TZ in docker-compose.yml.")));
  emailPanel.appendChild(emailField("Notification cooldown (minutes)", cooldown,
    "Results within this window are batched into one email."));

  if (settings.last_scan_at) {
    emailPanel.appendChild(el("div", { class: "field-hint", text: tx("Last scan: {1}", { 1: fmtDateTime(settings.last_scan_at) }) }));
  }

  const saveBtn = el("button", {
    class: "btn-primary", type: "button", text: tx("Save"),
    onclick: async () => {
      statusLine.textContent = tx("Saving...");
      const payload = {
        enabled: enabledInput.checked,
        imap_host: imapHost.value.trim() || null,
        imap_port: parseInt(imapPort.value, 10) || 993,
        // 993 and 465 are implicit TLS by convention (encrypted from the
        // first byte); everything else -- 587, 143, 25 -- connects in the
        // clear and upgrades via STARTTLS. Both flags used to be sent as
        // true regardless of port, which for SMTP meant STARTTLS even on
        // 465: the client waited for a plaintext greeting the server
        // never sends, and timed out.
        imap_use_ssl: (parseInt(imapPort.value, 10) || 993) === 993,
        smtp_host: smtpHost.value.trim() || null,
        smtp_port: parseInt(smtpPort.value, 10) || 587,
        smtp_use_tls: (parseInt(smtpPort.value, 10) || 587) !== 465,
        username: username.value.trim() || null,
        notify_email: notifyEmail.value.trim() || null,
        subject_keyword: keyword.value.trim() || "[RECIPE]",
        allowed_senders: senders.value.trim(),
        daily_scan_hour: parseInt(scanHour.value, 10) || 0,
        cooldown_minutes: parseInt(cooldown.value, 10) || 0,
      };
      if (password.value) payload.password = password.value;
      const res = await adminFetch(`${API}/email-settings`, jsonBody("PUT", payload));
      if (res.ok) {
        const body = await res.json().catch(() => ({}));
        // Changing the host/username without retyping the password
        // clears it -- previously the only visible sign was the password
        // field's placeholder changing, easy to miss.
        const msg = body.password_cleared
          ? tx("Saved. The stored password was cleared because the server or username changed — enter it again if you still need email ingest.")
          : tx("Saved.");
        if (body.password_cleared) announceError(msg); else announce(tx("Email settings saved."));
        // renderEmailSettings() rebuilds the panel, status line included,
        // so the message is written to the new one afterwards.
        await renderEmailSettings();
        const newStatus = $("#email-status-line");
        if (newStatus) newStatus.textContent = msg;
      } else {
        const err = await res.json().catch(() => ({}));
        statusLine.textContent = err.detail || tx("Could not save.");
      }
    },
  });

  const testBtn = el("button", {
    class: "btn-secondary", type: "button", text: tx("Send test email"),
    onclick: async () => {
      statusLine.textContent = tx("Testing...");
      try {
        const res = await adminFetch(`${API}/email-settings/test`, { method: "POST" });
        const body = await res.json();
        statusLine.textContent = (body.success ? "\u2713 " : "\u2717 ") + body.message;
      } catch {
        statusLine.textContent = tx("Test failed.");
      }
    },
  });

  const scanBtn = el("button", {
    class: "btn-secondary", type: "button", text: tx("Scan inbox now"),
    onclick: () => runInboxScan(statusLine),
  });

  const buttons = [saveBtn, testBtn, scanBtn];
  if (settings.password_set) {
    buttons.push(el("button", {
      class: "btn-secondary", type: "button", text: tx("Clear password"),
      onclick: async () => {
        if (!confirm(tx("Remove the stored email password? This also disables email ingest."))) return;
        if (!(await ensureUnlocked())) return;
        const result = await fetchWithTimeout(`${API}/email-settings/password`, { method: "DELETE" });
        if (!result.ok) { announceError(writeFailureMessage(result, tx("The stored password"))); return; }
        announce(tx("Stored email password removed."));
        await renderEmailSettings();
      },
    }));
  }

  emailPanel.appendChild(el("div", { style: "display:flex;gap:0.5rem;flex-wrap:wrap;margin-top:0.75rem;" }, buttons));
  emailPanel.appendChild(statusLine);

  setMailState(settings);
  if (settings.can_send || (settings.recent_recipients || []).length) {
    emailPanel.appendChild(recentRecipientsEditor());
  }
}

// The settings password, and the panels behind it: email, HTTPS, Logs.
export function initLockedPanels() {
  $("#admin-close").addEventListener("click", () => closeModal(adminOverlay));
  adminOverlay.addEventListener("click", (e) => { if (e.target === adminOverlay) closeModal(adminOverlay); });
  $("#admin-lock-now").addEventListener("click", async () => {
    await fetch(`${API}/admin/lock`, { method: "POST" }).catch(() => null);
    // Close what the password was protecting, so nothing stays on screen.
    for (const [panel, toggle] of [[emailPanel, emailToggle], [httpsPanel, httpsToggle], [logsPanel, logsToggle]]) {
      if (!panel.hidden) toggle.click();
    }
    announce(tx("Locked."));
    refreshAdminSection();
  });
  $("#admin-change-toggle").addEventListener("click", async () => {
    await settingsPasswordDialog("change");
  });

  emailToggle.addEventListener("click", async () => {
    const willOpen = emailPanel.hidden;
    if (willOpen && !(await ensureUnlocked())) return;
    emailPanel.hidden = !willOpen;
    emailToggle.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) await renderEmailSettings();
  });

  httpsToggle.addEventListener("click", async () => {
    const willOpen = httpsPanel.hidden;
    if (willOpen && !(await ensureUnlocked())) return;
    httpsPanel.hidden = !willOpen;
    httpsToggle.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) await renderHttpsSettings();
    else clearTimeout(httpsPollTimer);
  });

  logsToggle.addEventListener("click", async () => {
    const willOpen = logsPanel.hidden;
    if (willOpen && !(await ensureUnlocked())) return;
    logsPanel.hidden = !willOpen;
    logsToggle.setAttribute("aria-expanded", String(willOpen));
    logsToggle.textContent = willOpen ? tx("Hide log") : tx("View log");
    if (willOpen) await renderLogs();
  });

  $("#logs-download").addEventListener("click", async () => {
    if (!(await ensureUnlocked())) return;
    try {
      const res = await adminFetch(`${API}/logs/download`);
      if (!res.ok) throw new Error(String(res.status));
      saveBlob(await res.blob(), filenameFromResponse(res) || "open-the-pantry-log.txt");
      announce(tx("Download started."));
    } catch {
      announceError(tx("Couldn't reach Open the Pantry, so nothing was downloaded. Check your connection and try again."));
    }
  });

  $("#logs-clear").addEventListener("click", async () => {
    if (!(await ensureUnlocked())) return;
    if (!confirm(tx("Clear the log? Everything recorded so far is deleted. Download it first if you might need it."))) return;
    const result = await fetchWithTimeout(`${API}/logs`, { method: "DELETE" });
    if (!result.ok) { announceError(writeFailureMessage(result, tx("The log"))); return; }
    announce(tx("Log cleared."));
    if (!logsPanel.hidden) await renderLogs();
  });
}

// Photos on/off: a switch in Settings (it was a toolbar button). The list
// behind the Settings dialog updates as soon as it's flipped.
// -------------------------------------------------------------------
// Settings -> Learning: models trained on the user's own recipes
// (backend app/ml). Shows what is in use and lets it be retrained or
// turned off.
// -------------------------------------------------------------------
const learningEnabled = $("#learning-enabled");
const learningTrainBtn = $("#learning-train");
const pct = (x) => Math.round((x || 0) * 100);

function renderLearning(s) {
  learningEnabled.checked = !!s.enabled;
  learningTrainBtn.hidden = !s.enabled;
  learningTrainBtn.disabled = !!s.training;
  const seg = s.segmenter || {}, tags = s.tags || {};
  let imports, tagText;
  if (!s.enabled) {
    imports = tx("Off: imports and tag suggestions use the built-in rules only.");
    tagText = "";
  } else {
    imports = {
      active: () => tx("Reading imports: learned ({2}% right vs {3}% for the rules, on {1} recipes).",
        { 1: seg.imported_recipes, 2: pct(seg.learned_accuracy), 3: pct(seg.rules_accuracy) }),
      rules_better: () => tx("Reading imports: built-in rules (learned {2}% vs {3}% on {1} recipes; it needs {4} points more).",
        { 1: seg.imported_recipes, 2: pct(seg.learned_accuracy), 3: pct(seg.rules_accuracy), 4: pct(seg.min_gain != null ? seg.min_gain : 0.02) }),
      not_enough_data: () => tx("Reading imports: built-in rules until {1} imported recipes ({2} so far).",
        { 1: seg.needed, 2: seg.imported_recipes || 0 }),
    }[seg.status];
    imports = imports ? imports() : tx("Reading imports: not trained yet.");
    tagText = {
      active: () => tx("Tags: keywords, plus learned for {1} ({2} tagged recipes).",
        { 1: (tags.learned || []).map((n) => tagLabel(n)).join(", "), 2: tags.tagged_recipes || 0 }),
      not_enough_data: () => tx("Tags: keywords only until {1} tagged recipes ({2} so far).",
        { 1: tags.needed, 2: tags.tagged_recipes || 0 }),
      no_tags: () => tx("Tags: keywords only; no tag is on enough recipes yet ({1} tagged).", { 1: tags.tagged_recipes || 0 }),
      nothing_learned: () => tx("Tags: keywords only; no tag can be predicted reliably yet ({1} tagged recipes).",
        { 1: tags.tagged_recipes || 0 }),
    }[tags.status];
    tagText = tagText ? tagText() : tx("Tags: not trained yet.");
  }
  $("#learning-imports").textContent = imports;
  $("#learning-tags").textContent = tagText;
  $("#learning-tags").hidden = !tagText;
  // Why the other tags have no model: how often their extra suggestions
  // were right in cross-validation, against what's needed.
  let details = "";
  const notUsed = (s.enabled && (tags.status === "active" || tags.status === "nothing_learned") && tags.not_used) || [];
  // Only the tags that came close are worth naming; "too few right" for
  // every tag is what "no tag can be predicted reliably yet" already says.
  const close = notUsed.filter((t) => t.precision != null);
  if (close.length) {
    details = tx("Closest: {1} ({2}% needed).", {
      1: close.map((t) => tx("{1} {2}%", { 1: tagLabel(t), 2: pct(t.precision) })).join(", "),
      2: pct(tags.precision_needed || 0.85),
    });
  }
  $("#learning-tag-details").textContent = details;
  $("#learning-tag-details").hidden = !details;
  const left = s.enabled ? (s.left_out_to_review || 0) : 0;
  $("#learning-review").textContent = left
    ? txn(left, "{n} recipe to review isn't used yet.", "{n} recipes to review aren't used yet.")
    : "";
  $("#learning-review").hidden = !left;
  let trained = "";
  if (s.training) trained = tx("Training…");
  else if (s.error) trained = tx("The last training failed: {1}", { 1: s.error });
  else if (s.trained_at && s.enabled) trained = tx("Last trained: {1}", { 1: fmtDateTime(s.trained_at) });
  $("#learning-trained").textContent = trained;
  $("#learning-trained").hidden = !trained;
}

async function refreshLearning() {
  const res = await fetch(`${API}/learning`).catch(() => null);
  if (res && res.ok) renderLearning(await res.json());
}

// -------------------------------------------------------------------
// Settings -> What to eat? (backend: picker.py): the read-only picker
// page on its own port, and the link to it in the top bar. The link is
// there only while the picker is switched on AND running (a busy port
// counts as off); it is added and removed, never greyed out.
// -------------------------------------------------------------------
const pickerEnabled = $("#picker-enabled");
const pickerStatusLine = $("#picker-status");
const pickerWarning = $("#picker-warning");
// A die: "pick one for me". Fixed markup.
const PICKER_ICON_SVG = '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true" focusable="false">' +
  '<rect x="4" y="4" width="16" height="16" rx="3" fill="none" stroke="currentColor" stroke-width="2"/>' +
  '<circle cx="9" cy="9" r="1.5" fill="currentColor"/><circle cx="15" cy="15" r="1.5" fill="currentColor"/>' +
  '<circle cx="12" cy="12" r="1.5" fill="currentColor"/></svg>';
let pickerProbeTicket = 0;
let pickerRetryTimer = null;

// Only an http(s) address the server gave becomes a link.
function pickerAddress(st) {
  try {
    const u = new URL(st && st.url);
    return (u.protocol === "http:" || u.protocol === "https:") ? u.href : null;
  } catch { return null; }
}

function renderPickerLink(st) {
  const url = st && st.enabled ? pickerAddress(st) : null;
  let link = $("#picker-link");
  if (!url) { if (link) link.remove(); return; }
  if (!link) {
    // Icon only on a phone (the label is hidden by CSS); the aria-label
    // names it either way.
    link = el("a", { id: "picker-link", class: "sidebar-toggle topbar-link", "aria-label": tx("What to eat?"),
      trustedStaticHtml: PICKER_ICON_SVG }, [el("span", { class: "topbar-link-label", text: tx("What to eat?") })]);
    $("#settings-toggle").before(link);
  }
  link.href = url;
}

async function refreshPickerLink() {
  try {
    const res = await fetch(`${API}/picker/status`, { cache: "no-store" });
    const st = res.ok ? await res.json() : null;
    renderPickerLink(st);
    return st;
  } catch {
    renderPickerLink(null);   // a failed fetch hides the link
    return null;
  }
}

function renderPickerSettings(st) {
  clearTimeout(pickerRetryTimer);
  pickerProbeTicket += 1;
  pickerEnabled.checked = !!st.on;
  // With an API key set, say plainly that the key doesn't protect this
  // page (on or off: it matters most before switching it on).
  $("#picker-key-note").hidden = !st.api_key_set;
  pickerStatusLine.replaceChildren();
  pickerWarning.hidden = true;
  pickerStatusLine.hidden = !st.on;
  if (!st.on) return;
  const url = pickerAddress(st);
  if (st.enabled && url) {
    pickerStatusLine.append(tx("Address:") + " ", el("a", { href: url, text: url }));
    probePicker(st, url);
  } else if (st.listen_error === "port_in_use") {
    pickerStatusLine.textContent = tx("Port {1} is in use inside the container.", { 1: st.port });
  } else if (st.listen_error) {
    pickerStatusLine.textContent = tx("The page couldn't start. Settings → Logs has the details.");
  } else {
    pickerStatusLine.textContent = tx("Starting…");
    pickerRetryTimer = setTimeout(() => { if (!settingsOverlay.hidden) refreshPicker(); }, 2000);
  }
}

// The container can't tell whether the compose file publishes the port;
// this device can. Any answer at all counts (no-cors: the response itself
// can't be read); a network error means unreachable. Skipped on an
// https:// page, where the browser blocks any request to the plain
// http:// picker (mixed content), reachable or not.
async function probePicker(st, url) {
  if (location.protocol === "https:" && url.startsWith("http:")) return;
  const ticket = ++pickerProbeTicket;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 5000);
  let reachable = true;
  try {
    await fetch(`${url.replace(/\/+$/, "")}/healthz`, { mode: "no-cors", cache: "no-store", signal: ctrl.signal });
  } catch { reachable = false; }
  clearTimeout(timer);
  if (reachable || ticket !== pickerProbeTicket) return;
  pickerWarning.textContent = tx("On, but this device can't reach port {1}. Your compose file may be missing the {2} line under ports:.",
    { 1: st.public_port, 2: `${st.public_port}:${st.port}` });
  pickerWarning.hidden = false;
}

// A status fetched before the switch was last flipped is out of date:
// shown, it would flip the switch back under the user's finger.
let pickerSwitchTicket = 0;
async function refreshPicker() {
  const ticket = pickerSwitchTicket;
  const st = await refreshPickerLink();
  if (st && ticket === pickerSwitchTicket) renderPickerSettings(st);
}

const thumbnailsSetting = $("#thumbnails-setting");

// Settings -> Recipe view: checkboxes beside ingredients and steps.
const checkboxesSetting = $("#checkboxes-setting");

// Settings -> Learning, What to eat?, photos on cards and checkboxes.
export function initPreferencePanels() {
  learningEnabled.addEventListener("change", async () => {
    const res = await fetch(`${API}/learning`, jsonBody("PUT", { enabled: learningEnabled.checked })).catch(() => null);
    if (res && res.ok) { renderLearning(await res.json()); return; }
    learningEnabled.checked = !learningEnabled.checked;
    announceError(await responseErrorText(res));
  });

  learningTrainBtn.addEventListener("click", async () => {
    learningTrainBtn.disabled = true;
    $("#learning-trained").hidden = false;
    $("#learning-trained").textContent = tx("Training…");
    const res = await fetch(`${API}/learning/train`, { method: "POST" }).catch(() => null);
    if (res && res.ok) {
      renderLearning(await res.json());
      announce(tx("Training finished."));
      return;
    }
    learningTrainBtn.disabled = false;
    announceError(await responseErrorText(res));
    refreshLearning();
  });

  pickerEnabled.addEventListener("change", async () => {
    const want = pickerEnabled.checked;
    const ticket = ++pickerSwitchTicket;
    pickerProbeTicket += 1;
    pickerWarning.hidden = true;
    pickerStatusLine.hidden = !want;
    pickerStatusLine.textContent = want ? tx("Starting…") : "";
    const res = await fetch(`${API}/picker/settings`, jsonBody("PUT", { enabled: want })).catch(() => null);
    if (ticket !== pickerSwitchTicket) return;     // flipped again since; that answer decides
    if (res && res.ok) {
      const st = await res.json();
      renderPickerSettings(st);
      renderPickerLink(st);
      return;
    }
    announceError(await responseErrorText(res));
    refreshPicker();
  });
  thumbnailsSetting.checked = state.showThumbnails;
  thumbnailsSetting.addEventListener("change", () => {
    state.showThumbnails = thumbnailsSetting.checked;
    try { localStorage.setItem("recipe-app-show-thumbnails", String(state.showThumbnails)); } catch { /* private mode */ }
    renderRecipeList(currentRecipes);
  });
  checkboxesSetting.checked = state.showTicks;
  checkboxesSetting.addEventListener("change", () => {
    state.showTicks = checkboxesSetting.checked;
    storageSet("recipe-app-checkboxes", String(state.showTicks));
    if (!detailOverlay.hidden && detailRecipe && detailRecipeId === detailRecipe.id) renderRecipeDetail(detailRecipe);
  });
}

export { ensureUnlocked, adminFetch, refreshHttpsSummary, runInboxScan, refreshPickerLink };
