(() => {
  "use strict";

  const API = "/api";

  // Every state-changing request to the API must carry X-Requested-With;
  // the server refuses writes without it (cross_site_guard in main.py), which
  // stops other websites from making changes through your browser. Added
  // here once, for every fetch the app makes to its own API.
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init = {}) => {
    const url = typeof input === "string" ? input : input.url;
    const method = (init.method || (typeof input !== "string" && input.method) || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD" && new URL(url, location.href).origin === location.origin) {
      const headers = new Headers(init.headers || (typeof input !== "string" ? input.headers : undefined));
      headers.set("X-Requested-With", "OpenThePantry");
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
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

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
    region.appendChild(el("div", { class: "toast toast-error", role: "alert" }, [
      el("span", { text: msg }),
      el("button", { class: "toast-dismiss", type: "button", "aria-label": "Dismiss", text: "×", onclick: () => dismissToast() }),
    ]));
    clearTimeout(toastTimer);
    toastTimer = setTimeout(dismissToast, 8000);
  }
  function dismissToast() {
    clearTimeout(toastTimer);
    const region = $("#toast-region");
    if (region) region.innerHTML = "";
  }

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined) continue;
      if (k === "text") node.textContent = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const c of [].concat(children)) if (c) node.appendChild(c);
    return node;
  }

  let lastFocusedBeforeModal = null;
  function openModal(overlay) {
    lastFocusedBeforeModal = document.activeElement;
    overlay.hidden = false;
    const focusable = overlay.querySelector("button, input, textarea, select, a[href]");
    (focusable || overlay).focus();
    document.addEventListener("keydown", trapFocus);
  }
  function closeModal(overlay) {
    overlay.hidden = true;
    document.removeEventListener("keydown", trapFocus);
    if (lastFocusedBeforeModal) lastFocusedBeforeModal.focus();
  }
  function trapFocus(e) {
    if (e.key === "Escape") {
      const openOverlay = $$(".modal-overlay").find((o) => !o.hidden);
      if (openOverlay === addOverlay) discardCurrentDraftFiles();
      // Escape is a third way out of the recipe detail -- release the
      // wake lock here too, or the screen stays on after dismissal.
      if (openOverlay === detailOverlay) releaseWakeLock();
      if (openOverlay) closeModal(openOverlay);
      return;
    }
    if (e.key !== "Tab") return;
    const openOverlay = $$(".modal-overlay").find((o) => !o.hidden);
    if (!openOverlay) return;
    const focusables = $$('button, input, textarea, select, a[href]', openOverlay)
      .filter((n) => !n.disabled && n.offsetParent !== null);
    if (!focusables.length) return;
    const first = focusables[0], last = focusables[focusables.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }

  // -------------------------------------------------------------------
  // Text size control (persisted; scales the whole app via rem units)
  // -------------------------------------------------------------------
  const SIZE_STEPS = [14, 16, 18, 20, 22];
  function getSizeIndex() {
    const stored = parseInt(localStorage.getItem("recipe-app-font-size-idx"), 10);
    return Number.isInteger(stored) && stored >= 0 && stored < SIZE_STEPS.length ? stored : 1;
  }
  function applySizeIndex(idx) {
    idx = Math.max(0, Math.min(SIZE_STEPS.length - 1, idx));
    document.documentElement.style.setProperty("--base-font-size", SIZE_STEPS[idx] + "px");
    localStorage.setItem("recipe-app-font-size-idx", String(idx));
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
    const stored = localStorage.getItem("recipe-app-theme");
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
    localStorage.setItem("recipe-app-theme", choice);
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
  //   2. Support is partial (Chrome/Edge/Android, Safari 16.4+, not
  //      Firefox). Unsupported browsers hide the controls entirely
  //      rather than showing a toggle that does nothing.
  // -------------------------------------------------------------------
  const wakeLockSupported = "wakeLock" in navigator;
  let wakeLockSentinel = null;
  let wakeLockWanted = false;  // whether we *want* a lock right now (a recipe is open with it enabled)
  const wakeLockListeners = new Set();  // UI callbacks notified when the actual state changes

  function getWakeLockDefault() {
    return localStorage.getItem("recipe-app-wakelock-default") === "true";
  }
  function setWakeLockDefault(on) {
    localStorage.setItem("recipe-app-wakelock-default", String(on));
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
    document.addEventListener("visibilitychange", () => {
      // Re-acquire on return to the tab, but only if a recipe is still
      // open with the lock enabled.
      if (document.visibilityState === "visible" && wakeLockWanted && !wakeLockSentinel) {
        acquireWakeLock();
      }
    });

    const wakeLockSettingEl = $("#wakelock-setting");
    const wakeLockDefaultInput = $("#wakelock-default");
    wakeLockSettingEl.hidden = false;
    wakeLockDefaultInput.checked = getWakeLockDefault();
    wakeLockDefaultInput.addEventListener("change", async () => {
      setWakeLockDefault(wakeLockDefaultInput.checked);
      // Apply immediately if a recipe is already open.
      if (!detailOverlay.hidden) await setWakeLockWanted(wakeLockDefaultInput.checked);
    });
  }


  // One chooser, one Download button. There used to be two separate buttons
  // with the explanation in small print underneath, which read as "a backup
  // button" plus something else rather than as a choice of format.
  const backupDownloadBtn = $("#backup-download-btn");
  const backupNotesOption = document.querySelector(".backup-notes-option");
  const selectedBackupFormat = () =>
    (document.querySelector('input[name="backup-format"]:checked') || {}).value || "database";
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
        status.textContent = "Building a PDF of every recipe. This can take a while.";
      } else {
        url = `${API}/backup/database.zip`;
        fallbackName = "open-the-pantry-backup.zip";
        status.textContent = "Preparing your backup…";
      }
      backupDownloadBtn.disabled = true;
      const original = backupDownloadBtn.textContent;
      backupDownloadBtn.textContent = "Preparing…";
      try {
        const res = await fetch(url);
        if (!res.ok) {
          status.textContent = `The server couldn't build the file (error ${res.status}). Nothing was downloaded.`;
          return;
        }
        const blob = await res.blob();
        saveBlob(blob, filenameFromResponse(res) || fallbackName);
        status.textContent = `Ready: ${formatBytes(blob.size)}. Check your downloads.`;
      } catch {
        status.textContent = "Couldn't reach Open the Pantry, so nothing was downloaded. Check your connection and try again.";
      } finally {
        backupDownloadBtn.disabled = false;
        backupDownloadBtn.textContent = original;
      }
    });
  }

  $("#settings-toggle").addEventListener("click", () => { refreshBackupStats(); openModal(settingsOverlay); });
  $("#settings-close").addEventListener("click", () => closeModal(settingsOverlay));
  settingsOverlay.addEventListener("click", (e) => { if (e.target === settingsOverlay) closeModal(settingsOverlay); });

  const shareSheetOverlay = $("#share-sheet-overlay");
  $("#share-sheet-close").addEventListener("click", () => closeModal(shareSheetOverlay));
  shareSheetOverlay.addEventListener("click", (e) => { if (e.target === shareSheetOverlay) closeModal(shareSheetOverlay); });

  const ratingSheetOverlay = $("#rating-sheet-overlay");
  $("#rating-sheet-close").addEventListener("click", () => closeModal(ratingSheetOverlay));
  ratingSheetOverlay.addEventListener("click", (e) => { if (e.target === ratingSheetOverlay) closeModal(ratingSheetOverlay); });

  // -------------------------------------------------------------------
  // Email ingest settings (optional feature, collapsed by default)
  // -------------------------------------------------------------------
  const emailPanel = $("#email-settings-panel");
  const emailToggle = $("#email-settings-toggle");

  emailToggle.addEventListener("click", async () => {
    const willOpen = emailPanel.hidden;
    emailPanel.hidden = !willOpen;
    emailToggle.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) await renderEmailSettings();
  });

  // One scan routine for both places it can be started: Settings (next to
  // the connection settings, for testing) and the + menu (for everyday use).
  // Shows the summary plus one line per email -- the per-email lines carry
  // the reason for each failure, and their only other route is the result
  // email, which can't arrive when sending is what's broken.
  async function runInboxScan(statusEl, button) {
    statusEl.textContent = "Checking the inbox\u2026";
    if (button) button.disabled = true;
    try {
      const res = await fetch(`${API}/email-settings/scan`, { method: "POST" });
      const body = await res.json();
      if (!res.ok) {
        statusEl.textContent = body.detail || `The scan failed (error ${res.status}).`;
      } else if (body.scanned === 0 && body.messages.length) {
        statusEl.textContent = body.messages.join("\n");
      } else if (body.scanned === 0) {
        statusEl.textContent = "No new recipe emails.";
      } else {
        const summary = `Scanned ${body.scanned}: ${body.succeeded} ingested, ${body.failed} failed.`;
        statusEl.textContent = [summary, ...(body.messages || [])].join("\n");
        if (body.succeeded) { await loadTags(); await loadTimeBuckets(); loadRecipes(); }
      }
      announce(statusEl.textContent.split("\n")[0]);
    } catch {
      statusEl.textContent = "Couldn't reach Open the Pantry to run the scan. Check your connection and try again.";
    } finally {
      if (button) button.disabled = false;
    }
  }

  function emailField(labelText, input, hint) {
    return el("div", { class: "field" }, [
      el("label", { for: input.id, text: labelText }),
      input,
      hint ? el("div", { class: "field-hint", text: hint }) : null,
    ]);
  }

  async function renderEmailSettings() {
    emailPanel.innerHTML = "";
    emailPanel.appendChild(el("div", { class: "field-hint", text: "Loading..." }));

    let settings;
    try {
      const res = await fetch(`${API}/email-settings`);
      if (!res.ok) throw new Error(String(res.status));
      settings = await res.json();
    } catch {
      emailPanel.innerHTML = "";
      emailPanel.appendChild(el("div", { class: "field-hint", text: "Could not load email settings." }));
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
        box.appendChild(el("strong", { text: "Encryption key is invalid" }));
        box.appendChild(el("p", {
          text: "RECIPE_APP_ENCRYPTION_KEY is set in your compose file, but its value isn't a valid key. " +
                "Fix it or delete that line, then restart the container.",
        }));
      } else {
        box.appendChild(el("strong", { text: "Step 1: set up encryption" }));
        box.appendChild(el("p", {
          text: "Your email password is stored encrypted, so a key has to exist first. " +
                "This creates one in the app's data folder (encryption.key). " +
                "If you ever move the data folder, the key goes with it. The backup zip leaves it out, " +
                "so after restoring from a backup on a new install you re-enter the password.",
        }));
        box.appendChild(el("button", {
          class: "btn-primary", type: "button", text: "Set up encryption",
          onclick: async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            setupStatus.textContent = "Creating key\u2026";
            try {
              const res = await fetch(`${API}/email-settings/encryption-key`, { method: "POST" });
              if (res.ok || res.status === 409) {
                announce("Encryption is set up. You can now enter the email password.");
                await renderEmailSettings();
                return;
              }
              const err = await res.json().catch(() => ({}));
              setupStatus.textContent = err.detail || `Couldn't create the key (error ${res.status}).`;
            } catch {
              setupStatus.textContent = "Couldn't reach Open the Pantry. Check your connection and try again.";
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
        ? "Set up encryption first (above)"
        : settings.password_set ? "Saved \u2014 leave blank to keep" : "App password",
    });
    if (!settings.encryption_configured) password.disabled = true;
    const notifyEmail = el("input", { type: "email", id: "email-notify", value: settings.notify_email || "" });
    const keyword = el("input", { type: "text", id: "email-keyword", value: settings.subject_keyword || "[RECIPE]" });
    const senders = el("input", { type: "text", id: "email-senders", value: settings.allowed_senders || "",
      placeholder: "you@example.com, @family.example" });
    const scanHour = el("input", { type: "number", id: "email-scan-hour", min: "0", max: "23", value: String(settings.daily_scan_hour ?? 3) });
    const cooldown = el("input", { type: "number", id: "email-cooldown", min: "0", value: String(settings.cooldown_minutes ?? 30) });

    // pre-line: the test result puts sending and reading on separate lines.
    const statusLine = el("div", { class: "field-hint", role: "status", style: "margin-top:0.5rem;white-space:pre-line;" });

    emailPanel.appendChild(el("div", { class: "field" }, [
      el("label", { for: "email-enabled", style: "display:flex;align-items:center;gap:0.5rem;" }, [
        enabledInput,
        el("span", { text: "Enable daily inbox scan" }),
      ]),
    ]));

    emailPanel.appendChild(emailField("IMAP host (reading)", imapHost, "e.g. imap.gmail.com"));
    emailPanel.appendChild(emailField("IMAP port", imapPort,
      "993 connects with TLS from the start (implicit TLS). Any other port starts " +
      "unencrypted and upgrades via STARTTLS -- that's how port 143 is normally used."));
    emailPanel.appendChild(emailField("SMTP host (notifications)", smtpHost, "e.g. smtp.gmail.com"));
    emailPanel.appendChild(emailField("SMTP port", smtpPort,
      "465 connects with TLS from the start (implicit TLS). Any other port -- typically " +
      "587 -- starts unencrypted and upgrades via STARTTLS. Guessed from the port " +
      "number above; there's no separate setting for it."));
    emailPanel.appendChild(emailField("Username", username, "Used for both IMAP and SMTP."));
    emailPanel.appendChild(emailField("Password", password,
      "Stored encrypted. Use an app-specific password, not your main account password."));
    emailPanel.appendChild(emailField("Send notifications to", notifyEmail));
    emailPanel.appendChild(emailField("Subject keyword", keyword,
      "Only emails whose subject contains this are considered. Everything else is ignored."));
    emailPanel.appendChild(emailField("Only accept email from", senders,
      "Addresses, or a domain (example.com) for everyone there, separated by commas. Leave empty to accept anyone " +
      "who knows the address and keyword. Recommended: list the addresses you send from."));
    emailPanel.appendChild(emailField("Daily scan hour (0-23)", scanHour,
      `In the server's time zone${settings.server_timezone ? ` (${settings.server_timezone})` : ""}. ` +
      "If that isn't yours, set TZ in docker-compose.yml."));
    emailPanel.appendChild(emailField("Notification cooldown (minutes)", cooldown,
      "Results within this window are batched into one email."));

    if (settings.last_scan_at) {
      emailPanel.appendChild(el("div", { class: "field-hint", text: `Last scan: ${new Date(settings.last_scan_at).toLocaleString()}` }));
    }

    const saveBtn = el("button", {
      class: "btn-primary", type: "button", text: "Save",
      onclick: async () => {
        statusLine.textContent = "Saving...";
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
        const res = await fetch(`${API}/email-settings`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (res.ok) {
          const body = await res.json().catch(() => ({}));
          // Changing the host/username without retyping the password
          // clears it -- previously the only visible sign was the password
          // field's placeholder changing, easy to miss.
          const msg = body.password_cleared
            ? "Saved. The stored password was cleared because the server or username changed — enter it again if you still need email ingest."
            : "Saved.";
          statusLine.textContent = msg;
          if (body.password_cleared) announceError(msg); else announce("Email settings saved.");
          await renderEmailSettings();
        } else {
          const err = await res.json().catch(() => ({}));
          statusLine.textContent = err.detail || "Could not save.";
        }
      },
    });

    const testBtn = el("button", {
      class: "btn-secondary", type: "button", text: "Send test email",
      onclick: async () => {
        statusLine.textContent = "Testing...";
        try {
          const res = await fetch(`${API}/email-settings/test`, { method: "POST" });
          const body = await res.json();
          statusLine.textContent = (body.success ? "\u2713 " : "\u2717 ") + body.message;
        } catch {
          statusLine.textContent = "Test failed.";
        }
      },
    });

    const scanBtn = el("button", {
      class: "btn-secondary", type: "button", text: "Scan inbox now",
      onclick: () => runInboxScan(statusLine),
    });

    const buttons = [saveBtn, testBtn, scanBtn];
    if (settings.password_set) {
      buttons.push(el("button", {
        class: "btn-secondary", type: "button", text: "Clear password",
        onclick: async () => {
          if (!confirm("Remove the stored email password? This also disables email ingest.")) return;
          const result = await fetchWithTimeout(`${API}/email-settings/password`, { method: "DELETE" });
          if (!result.ok) { announceError(writeFailureMessage(result, "The stored password")); return; }
          announce("Stored email password removed.");
          await renderEmailSettings();
        },
      }));
    }

    emailPanel.appendChild(el("div", { style: "display:flex;gap:0.5rem;flex-wrap:wrap;margin-top:0.75rem;" }, buttons));
    emailPanel.appendChild(statusLine);
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
    grouping: "all",         // 'all' | 'meal_type' | 'cooking_style' | 'main_ingredient'
    allTags: [],
    timeBuckets: [],
    selectMode: false,
    selectedIds: new Set(),
    currentDraftFiles: new Set(),  // temp file(s) awaiting save/discard for the open add-recipe session
    showThumbnails: localStorage.getItem("recipe-app-show-thumbnails") !== "false",  // default on
    // Which sidebar category groups are open. Default is ALL CLOSED: fully
    // expanded, the tree runs well past a phone screen. This has to live in
    // state rather than the DOM because renderSidebar() tears the tree down
    // and rebuilds it on every tag click -- keeping it in the markup would
    // re-open every group the moment you picked a filter.
    expandedGroups: loadExpandedGroups(),
    // Sort is persisted: it is a standing preference about how you like to
    // read your library, not a per-visit choice.
    sort: localStorage.getItem("recipe-app-sort") || "created",
    direction: localStorage.getItem("recipe-app-sort-dir") || "desc",
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
      + (state.query ? 1 : 0);
  }

  function clearAllFilters() {
    state.filterTags.clear();
    state.maxMinutes = null;
    uiTimeLevel1 = null;
    state.query = "";
    const searchInput = $("#search-input");
    if (searchInput) searchInput.value = "";
    renderSidebar();
    if (!$("#filter-panel").hidden) renderFilterPanel();
    syncClearFiltersButton();
    loadRecipes();
    announce("Filters cleared.");
  }

  // The toolbar button carries the count and hides itself when nothing is
  // active -- a permanently visible "Clear filters" that usually does
  // nothing is just noise.
  function syncClearFiltersButton() {
    const btn = $("#clear-filters-btn");
    if (!btn) return;
    const n = activeFilterCount();
    btn.hidden = n === 0;
    btn.textContent = `Clear filters (${n})`;
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
      onclick: () => toggleGroup(key),
    }, [
      el("span", { class: "tag-group-chevron", "aria-hidden": "true", text: "›" }),
      el("span", { class: "tag-group-label", text: label }),
      // When a group is closed, its active filters are invisible. The count
      // keeps them discoverable so you can't forget a filter is narrowing
      // the list.
      opts.activeCount ? el("span", { class: "tag-group-count", text: String(opts.activeCount) }) : null,
    ]);
    if (forced) header.setAttribute("data-forced-open", "true");

    return el("div", { class: "tag-group" }, [header, body]);
  }

  async function loadTags() {
    const res = await fetch(`${API}/tags`);
    state.allTags = await res.json();
    renderSidebar();
  }

  async function loadTimeBuckets() {
    const res = await fetch(`${API}/time-buckets`);
    state.timeBuckets = await res.json();
  }

  function renderSidebar() {
    const container = $("#tag-tree");
    container.innerHTML = "";
    const categories = [
      ["meal_type", "Meal Type"],
      ["cooking_style", "Cooking Style"],
      ["main_ingredient", "Main Ingredient"],
      ["custom", "Custom"],
    ];

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
          text: `Clear filters (${activeFilterCount()})`,
          onclick: clearAllFilters,
        }),
      ]));
    }

    container.appendChild(el("div", { class: "tag-tree-actions" }, [
      el("button", {
        type: "button", class: "tag-tree-expand-all",
        text: anyOpen ? "Collapse all" : "Expand all",
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
          sg === "cocktail" ? "Cocktail Prep" : sg,
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
      text: tag.name,
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
      text: tag.name,
      onclick: () => toggleTagFilter(tag.name),
    });
  }

  function renderFilterPanel() {
    const panel = $("#filter-panel");
    panel.innerHTML = "";

    const categories = [
      ["meal_type", "Meal Type"],
      ["cooking_style", "Cooking Style"],
      ["main_ingredient", "Main Ingredient"],
      ["custom", "Custom"],
    ];
    for (const [catKey, catLabel] of categories) {
      const tagsInCat = state.allTags.filter((t) => t.category === catKey);
      if (!tagsInCat.length) continue;
      panel.appendChild(el("div", { class: "filter-section" }, [
        el("div", { class: "filter-section-title", text: catLabel }),
        el("div", { class: "filter-chip-row" }, tagsInCat.map(makeFilterChip)),
      ]));
    }

    const timeSection = el("div", { class: "filter-section" });
    timeSection.appendChild(el("div", { class: "filter-section-title", text: "Cook Time" }));
    if (!state.timeBuckets.length) {
      timeSection.appendChild(el("div", { class: "field-hint", text: "No logged cook times yet \u2014 this filter appears once recipes have a real-world time logged." }));
    } else {
      const hourBuckets = state.timeBuckets.filter((b) => b.minutes % 60 === 0);
      timeSection.appendChild(el("div", { class: "time-filter-row" }, hourBuckets.map((b) =>
        el("button", {
          class: "filter-chip", type: "button",
          "aria-pressed": String(uiTimeLevel1 === b.minutes),
          text: `\u2264 ${b.label}`,
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
            text: `\u2264 ${b.label}`,
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

    if (activeFilterCount()) {
      const bits = [];
      if (state.filterTags.size) bits.push(`${state.filterTags.size} tag filter${state.filterTags.size === 1 ? "" : "s"}`);
      if (state.maxMinutes != null) bits.push("time filter");
      if (state.query) bits.push("search");
      panel.appendChild(el("div", { class: "active-filters-summary" }, [
        el("span", { text: `${bits.join(" + ")} active` }),
        el("button", {
          type: "button", class: "btn-secondary", text: "Clear filters",
          onclick: clearAllFilters,
        }),
      ]));
    }
  }

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

  // -------------------------------------------------------------------
  // Grouping toggle
  // -------------------------------------------------------------------
  $$(".grouping-toggle button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.grouping = btn.dataset.group;
      $$(".grouping-toggle button").forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
      const filtersToggle = $("#filters-toggle");
      filtersToggle.hidden = state.grouping !== "all";
      if (state.grouping !== "all") $("#filter-panel").hidden = true;
      renderRecipeList(currentRecipes);
    });
  });

  // -------------------------------------------------------------------
  // Select mode / batch delete
  // -------------------------------------------------------------------
  function updateBatchBar() {
    $("#batch-count").textContent = `${state.selectedIds.size} selected`;
    const barVisible = state.selectMode && state.selectedIds.size > 0;
    $("#batch-bar").hidden = !barVisible;
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

  $("#thumbnails-toggle").addEventListener("click", () => {
    state.showThumbnails = !state.showThumbnails;
    localStorage.setItem("recipe-app-show-thumbnails", String(state.showThumbnails));
    $("#thumbnails-toggle").setAttribute("aria-pressed", String(state.showThumbnails));
    renderRecipeList(currentRecipes);
  });
  $("#thumbnails-toggle").setAttribute("aria-pressed", String(state.showThumbnails));

  $("#batch-cancel-btn").addEventListener("click", () => {
    state.selectMode = false;
    state.selectedIds.clear();
    $("#select-mode-toggle").setAttribute("aria-pressed", "false");
    updateBatchBar();
    renderRecipeList(currentRecipes);
  });

  $("#batch-delete-btn").addEventListener("click", async () => {
    if (!state.selectedIds.size) return;
    if (!confirm(`Delete ${state.selectedIds.size} recipe(s)? This cannot be undone.`)) return;
    const ids = [...state.selectedIds];
    const btn = $("#batch-delete-btn");
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = "Deleting…";

    const result = await fetchWithTimeout(`${API}/recipes/batch-delete`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });

    btn.disabled = false;
    btn.textContent = label;

    if (!result.ok) {
      // Keep the selection on failure. It used to be cleared unconditionally
      // alongside an unconditional "deleted" message, so a failed batch told
      // you it had worked AND threw away the selection you would need to
      // retry with.
      announceError(writeFailureMessage(result, "The selected recipes"));
      if (result.timedOut) loadRecipes();
      return;
    }

    // The endpoint reports per-item outcomes (deleted / missing / failed).
    // Those were being discarded, so a batch where half the items failed
    // still announced complete success.
    let summary;
    try {
      const body = await result.res.json();
      const parts = [`${body.deleted.length} deleted`];
      if (body.missing.length) parts.push(`${body.missing.length} already gone`);
      if (body.failed.length) parts.push(`${body.failed.length} failed`);
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

  async function loadRecipes() {
    const params = new URLSearchParams();
    if (state.query) params.set("q", state.query);
    for (const t of state.filterTags) params.append("tags", t);
    if (state.maxMinutes != null) params.set("max_minutes", String(state.maxMinutes));
    params.set("sort", state.sort);
    params.set("direction", state.direction);
    // An error used to be parsed as the recipe list, which came out empty
    // and read "No recipes match" -- e.g. a 429 from the rate limiter.
    let res;
    try {
      res = await fetch(`${API}/recipes?${params.toString()}`);
    } catch {
      showListError("Couldn't reach Open the Pantry. Check your connection and try again.");
      return;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showListError(res.status === 429
        ? "The server is busy with too many requests. Wait a moment and try again."
        : `Couldn't load recipes (${typeof err.detail === "string" ? err.detail : "error " + res.status}).`);
      return;
    }
    currentRecipes = await res.json();
    renderRecipeList(currentRecipes);
    announce(`${currentRecipes.length} recipe${currentRecipes.length === 1 ? "" : "s"} found`);
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
      el("button", { class: "btn-secondary", type: "button", text: "Try again", onclick: () => loadRecipes() }),
    ]));
    announce(message);
  }

  function sourceBadge(recipe) {
    const label = { url: "Web", pdf: "PDF", screenshot: "Screenshot", manual: "Handwritten/Manual", email: "Email" }[recipe.source_type] || recipe.source_type;
    return el("span", { class: `badge badge-source-${recipe.source_type}`, text: label });
  }

  function confidenceBadge(recipe) {
    if (recipe.ocr_confidence == null) return null;
    const c = recipe.ocr_confidence;
    const tier = c >= 80 ? "high" : c >= 55 ? "med" : "low";
    const label = tier === "low" ? "OCR quality: low" : tier === "med" ? "OCR quality: fair" : "OCR quality: good";
    return el("span", { class: `badge badge-confidence-${tier}`, text: label });
  }

  function matchedViaBadge(recipe) {
    if (!recipe.matched_via || !recipe.matched_via.length) return null;
    return el("span", { class: "badge badge-matched", text: `matched: ${recipe.matched_via.join(", ")}` });
  }

  // -------------------------------------------------------------------
  // Card-level quick controls: favorite toggle + 3 rating popovers
  // -------------------------------------------------------------------
  async function patchRating(id, payload) {
    const res = await fetch(`${API}/recipes/${id}/rating`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    return res.ok;
  }

  async function patchNotes(id, notes) {
    const res = await fetch(`${API}/recipes/${id}/notes`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes }),
    });
    return res.ok;
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
    tastiness_rating: "How good was it?",
    cook_time_rating: "How long does it take?",
    difficulty_rating: "How hard is it?",
  };

  function ratingOptionLabel(field, opt) {
    return field === "tastiness_rating"
      ? "\u2b50".repeat(opt)
      : opt.charAt(0).toUpperCase() + opt.slice(1);
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
    return typeof v === "number" ? String(v) : String(v).charAt(0).toUpperCase();
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
    $("#rating-sheet-heading").textContent = RATING_TITLES[field] || "Rating";
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
          if (ok) applyRating(recipe, field, opt, btn, icon, compactRatingLabel);
          else announceError("Could not save rating.");
        },
      }));
    }

    // Setting a rating by mistake needs a way out, and on a card there is no
    // other affordance for it.
    if (recipe[field] != null) {
      actions.appendChild(el("button", {
        type: "button", class: "rating-clear",
        text: "Clear rating",
        onclick: async () => {
          closeModal(overlay);
          const ok = await patchRating(recipe.id, { [field]: null });
          if (ok) applyRating(recipe, field, null, btn, icon, compactRatingLabel);
          else announceError("Could not clear rating.");
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
      "aria-label": `Set ${field.replace(/_/g, " ")}`,
      title: current != null ? `${field.replace(/_/g, " ")}: ${current}` : null,
    });

    if (!inline) {
      btn.setAttribute("aria-haspopup", "dialog");
      btn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        openRatingSheet(recipe, field, icon, options, shortLabel, btn);
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
        text: ratingOptionLabel(field, opt),
        onclick: async () => {
          popover.hidden = true;
          btn.setAttribute("aria-expanded", "false");
          const ok = await patchRating(recipe.id, { [field]: opt });
          if (ok) applyRating(recipe, field, opt, btn, icon, shortLabel);
          else announceError("Could not save rating.");
        },
      }));
    }

    return el("div", { class: "rating-control" }, [btn, popover]);
  }

  function cardQuickControls(recipe, inline = false) {
    const wrap = el("div", {
      class: inline ? "card-quick-controls inline" : "card-quick-controls",
      onclick: (e) => e.stopPropagation(),
    });

    const favBtn = el("button", {
      type: "button", class: "favorite-btn",
      "aria-pressed": String(!!recipe.favorite),
      "aria-label": recipe.favorite ? "Remove from favorites" : "Add to favorites",
      text: recipe.favorite ? "\u2665" : "\u2661",
      onclick: async () => {
        const newVal = !recipe.favorite;
        const ok = await patchRating(recipe.id, { favorite: newVal });
        if (ok) {
          recipe.favorite = newVal;
          favBtn.setAttribute("aria-pressed", String(newVal));
          favBtn.textContent = newVal ? "\u2665" : "\u2661";
        }
      },
    });
    wrap.appendChild(favBtn);

    wrap.appendChild(ratingPopoverControl(recipe, "tastiness_rating", "\u2b50", [1, 2, 3, 4, 5], (v) => String(v), inline));
    wrap.appendChild(ratingPopoverControl(recipe, "cook_time_rating", "\u23f1", ["quick", "moderate", "long"], (v) => v.charAt(0).toUpperCase() + v.slice(1), inline));
    wrap.appendChild(ratingPopoverControl(recipe, "difficulty_rating", "\ud83c\udf9a", ["easy", "medium", "hard"], (v) => v.charAt(0).toUpperCase() + v.slice(1), inline));

    // Icon-only share, pushed to the far end of the row so it sits opposite
    // the ratings. Only on list cards -- the detail page has its own
    // labelled Share button in the header, so a second one here would be
    // redundant.
    if (!inline) {
      wrap.appendChild(el("button", {
        type: "button", class: "card-share-btn",
        "aria-label": `Share ${recipe.title}`,
        title: "Share",
        html: SHARE_ICON_SVG,
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); openShareSheet(recipe); },
      }));
    }

    return wrap;
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
        alt: "", loading: "lazy",
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
        "aria-label": `Select ${recipe.title}`,
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
      el("h3", { class: "recipe-card-title recipe-title", text: recipe.title }),
      matched ? el("div", { class: "card-matched-row" }, [matched]) : null,
      state.selectMode ? null : cardQuickControls(recipe),
    ]));
    return card;
  }

  function renderRecipeList(recipes) {
    const region = $("#recipe-list-region");
    region.innerHTML = "";
    $$(".recipe-grid").forEach((g) => g.dataset && (g.dataset.selectMode = String(state.selectMode)));

    if (!recipes.length) {
      region.appendChild(el("div", { class: "empty-state", text: "No recipes match. Tap + to add one, or adjust your filters." }));
      return;
    }

    if (state.grouping === "all") {
      region.appendChild(el("div", { class: "recipe-grid", "data-select-mode": String(state.selectMode), "data-show-thumbnails": String(state.showThumbnails) }, recipes.map(recipeCard)));
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

    for (const b of mainBuckets) region.appendChild(groupSection(b.tag.name, b.recipes));

    if (subBuckets.length) {
      const subWrap = el("div", { class: "group-section" });
      subWrap.appendChild(el("h2", { text: "Cocktail Prep" }));
      for (const b of subBuckets) subWrap.appendChild(groupSection(b.tag.name, b.recipes));
      region.appendChild(subWrap);
    }

    if (uncategorized.length) region.appendChild(groupSection("Uncategorized", uncategorized));
  }

  function groupSection(title, recipes) {
    return el("section", { class: "group-section" }, [
      el("h3", { text: title }),
      el("div", { class: "recipe-grid", "data-select-mode": String(state.selectMode), "data-show-thumbnails": String(state.showThumbnails) }, recipes.map(recipeCard)),
    ]);
  }

  // -------------------------------------------------------------------
  // Duration input helper (dd:hh:mm), used for the real-world cook time
  // -------------------------------------------------------------------
  function makeDurationInput(prefix, initialDdHhMm) {
    let initD = "", initH = "", initM = "";
    if (initialDdHhMm) {
      const parts = initialDdHhMm.split(":").map((p) => parseInt(p, 10));
      if (parts.length === 3 && parts.every((n) => !Number.isNaN(n))) {
        [initD, initH, initM] = parts.map(String);
      }
    }
    const days = el("input", { type: "number", min: "0", id: `${prefix}-days`, "aria-label": "Days", value: initD, placeholder: "0" });
    const hours = el("input", { type: "number", min: "0", max: "23", id: `${prefix}-hours`, "aria-label": "Hours", value: initH, placeholder: "0" });
    const minutes = el("input", { type: "number", min: "0", max: "59", id: `${prefix}-minutes`, "aria-label": "Minutes", value: initM, placeholder: "0" });
    const wrap = el("div", { class: "duration-input" }, [
      days, el("span", { text: "d" }), hours, el("span", { text: "h" }), minutes, el("span", { text: "m" }),
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
    closeModal(detailOverlay);
  }

  $("#recipe-detail-close").addEventListener("click", closeRecipeDetail);
  detailOverlay.addEventListener("click", (e) => { if (e.target === detailOverlay) closeRecipeDetail(); });

  async function openRecipeDetail(id) {
    const res = await fetch(`${API}/recipes/${id}`);
    if (!res.ok) { announceError("Could not load recipe."); return; }
    const recipe = await res.json();
    renderRecipeDetail(recipe);
    openModal(detailOverlay);
    if (wakeLockSupported && getWakeLockDefault()) await setWakeLockWanted(true);
  }

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

    const btn = el("button", { class: "btn-secondary wakelock-btn", type: "button" });

    function sync() {
      const on = wakeLockActive();
      btn.setAttribute("aria-pressed", String(on));
      btn.setAttribute("data-active", String(on));
      btn.textContent = on ? "\u2600\ufe0e Screen staying on" : "\u2600\ufe0e Keep screen on";
      btn.setAttribute("aria-label", on
        ? "Screen is being kept awake. Tap to allow it to sleep."
        : "Keep the screen awake while reading this recipe.");
    }

    btn.addEventListener("click", async () => {
      await setWakeLockWanted(!wakeLockActive());
      if (wakeLockWanted && !wakeLockActive()) {
        announce("The browser wouldn't keep the screen awake \u2014 it may be blocked on low battery.");
      }
    });

    wakeLockListeners.add(sync);
    sync();
    return btn;
  }

  function notesSection(recipe) {
    const hasNotes = () => !!recipe.notes;

    const textarea = el("textarea", {
      id: `notes-textarea-${recipe.id}`,
      placeholder: "Substitutions, timing tweaks, how it turned out...",
      style: "min-height:6rem;",
    });
    textarea.value = recipe.notes || "";

    const editorWrap = el("div", { class: "notes-editor", hidden: "" });

    const toggleBtn = el("button", {
      class: "btn-secondary notes-toggle-btn", type: "button",
      "data-has-notes": String(hasNotes()),
      "aria-expanded": "false",
      "aria-label": hasNotes() ? "View or edit notes" : "Add notes",
      text: hasNotes() ? "\ud83d\udcdd Notes" : "\ud83d\udcdd Add notes",
      onclick: () => {
        const willOpen = editorWrap.hidden;
        editorWrap.hidden = !willOpen;
        toggleBtn.setAttribute("aria-expanded", String(willOpen));
        if (willOpen) textarea.focus();
      },
    });

    const syncButtonState = () => {
      toggleBtn.setAttribute("data-has-notes", String(hasNotes()));
      toggleBtn.textContent = hasNotes() ? "\ud83d\udcdd Notes" : "\ud83d\udcdd Add notes";
      toggleBtn.setAttribute("aria-label", hasNotes() ? "View or edit notes" : "Add notes");
    };

    const saveBtn = el("button", {
      class: "btn-primary", type: "button", text: "Save notes",
      onclick: async () => {
        const newNotes = textarea.value.trim();
        const ok = await patchNotes(recipe.id, newNotes);
        if (ok) {
          recipe.notes = newNotes || null;
          syncButtonState();
          editorWrap.hidden = true;
          toggleBtn.setAttribute("aria-expanded", "false");
          announce("Notes saved.");
        } else {
          announceError("Could not save notes.");
        }
      },
    });

    const cancelBtn = el("button", {
      class: "btn-secondary", type: "button", text: "Cancel",
      onclick: () => {
        textarea.value = recipe.notes || "";
        editorWrap.hidden = true;
        toggleBtn.setAttribute("aria-expanded", "false");
      },
    });

    editorWrap.appendChild(el("div", { class: "field" }, [
      el("label", { for: `notes-textarea-${recipe.id}`, text: "Notes" }),
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
        announce("Uploading photo...");
        const fd = new FormData(); fd.append("file", fileInput.files[0]);
        const upRes = await fetch(`${API}/upload-image`, { method: "POST", body: fd });
        if (!upRes.ok) { announceError("Could not upload photo."); return; }
        const { stored_file } = await upRes.json();
        const patchRes = await fetch(`${API}/recipes/${recipe.id}/image`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image_path: stored_file }),
        });
        if (patchRes.ok) {
          recipe.image_path = (await patchRes.json()).image_path;
          announce("Photo updated.");
          render();
          loadRecipes();  // keep the list thumbnail in sync
        } else {
          fetch(`${API}/ingest/draft/${encodeURIComponent(stored_file)}`, { method: "DELETE" }).catch(() => {});
          announceError("Could not save photo.");
        }
      });

      const changeBtn = el("button", {
        class: "btn-secondary", type: "button",
        text: recipe.image_path ? "Change photo" : "Add photo",
        onclick: () => fileInput.click(),
      });

      const controls = [changeBtn, fileInput];
      if (recipe.image_path) {
        controls.push(el("button", {
          class: "btn-secondary", type: "button", text: "Remove photo",
          onclick: async () => {
            if (!confirm("Remove this recipe's photo?")) return;
            const res = await fetch(`${API}/recipes/${recipe.id}/image`, {
              method: "PATCH", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ image_path: null }),
            });
            if (res.ok) {
              recipe.image_path = null;
              announce("Photo removed.");
              render();
              loadRecipes();
            } else {
              announceError("Could not remove photo.");
            }
          },
        }));
      }
      container.appendChild(el("div", { style: "display:flex;gap:0.5rem;margin-top:0.5rem;flex-wrap:wrap;" }, controls));
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
    const servingsInput = el("input", { type: "text", id: "edit-servings", value: recipe.servings || "" });
    const prepInput = el("input", { type: "text", id: "edit-prep", value: recipe.prep_time || "" });
    const cookInput = el("input", { type: "text", id: "edit-cook", value: recipe.cook_time || "" });
    const totalInput = el("input", { type: "text", id: "edit-total", value: recipe.total_time || "" });

    const ingredientsArea = el("textarea", { id: "edit-ingredients", style: "min-height:9rem;" });
    ingredientsArea.value = recipe.ingredients
      .map((i) => ingredientText(i))
      .join("\n");

    const stepsArea = el("textarea", { id: "edit-steps", style: "min-height:9rem;" });
    stepsArea.value = recipe.steps.map((s) => s.text).join("\n");

    const tagsInput = el("input", {
      type: "text", id: "edit-tags",
      value: recipe.tags.map((t) => t.name).join(", "),
    });

    // pre-line: the test result puts sending and reading on separate lines.
    const statusLine = el("div", { class: "field-hint", role: "status", style: "margin-top:0.5rem;white-space:pre-line;" });

    const saveBtn = el("button", {
      class: "btn-primary", type: "button", text: "Save changes",
      onclick: async () => {
        statusLine.textContent = "Saving...";
        const tagNames = tagsInput.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
        const tagObjs = tagNames.map((name) => {
          const known = state.allTags.find((t) => t.name.toLowerCase() === name.toLowerCase());
          return known
            ? { name: known.name, category: known.category, subgroup: known.subgroup }
            : { name, category: "custom", subgroup: null };
        });

        const payload = {
          title: titleInput.value.trim() || "Untitled Recipe",
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
          actual_cook_time: recipe.actual_cook_time || null,
          ingredients: ingredientsArea.value.split("\n").filter((l) => l.trim())
            .map((line) => ({ raw_line: line, name: line })),
          steps: stepsArea.value.split("\n").filter((l) => l.trim()),
          tags: tagObjs,
        };

        try {
          const res = await fetch(`${API}/recipes/${recipe.id}`, {
            method: "PUT", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (!res.ok) throw new Error("Could not save changes.");
          const updated = await res.json();
          announce("Recipe updated.");
          renderRecipeDetail(updated);
          await loadTags();
          loadRecipes();
        } catch (err) {
          statusLine.textContent = err.message;
        }
      },
    });

    const cancelBtn = el("button", {
      class: "btn-secondary", type: "button", text: "Cancel",
      onclick: () => renderRecipeDetail(recipe),
    });

    const fields = el("div", {}, [
      el("div", { class: "field" }, [el("label", { for: "edit-title", text: "Title" }), titleInput]),
      el("div", { class: "field" }, [
        el("label", { for: "edit-ingredients", text: "Ingredients (one per line)" }),
        ingredientsArea,
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "edit-steps", text: "Steps (one per line)" }),
        stepsArea,
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "edit-tags", text: "Tags (comma or semicolon separated)" }),
        tagsInput,
      ]),
      el("div", { class: "field" }, [el("label", { for: "edit-servings", text: "Servings" }), servingsInput]),
      el("div", { class: "field" }, [el("label", { for: "edit-prep", text: "Prep time" }), prepInput]),
      el("div", { class: "field" }, [el("label", { for: "edit-cook", text: "Cook time" }), cookInput]),
      el("div", { class: "field" }, [el("label", { for: "edit-total", text: "Total time" }), totalInput]),
    ]);

    // Raw source text as a read-only reference pane. For a low-confidence
    // OCR scrape this is the whole point of the screen: compare the
    // parsed fields against what was actually extracted.
    const referencePane = recipe.raw_text
      ? el("div", {}, [
          el("h4", { text: "Original extracted text (reference)" }),
          el("div", { class: "review-raw", text: recipe.raw_text }),
          el("div", { class: "field-hint", text: "Read-only. What the scraper or OCR actually produced." }),
        ])
      : el("div", { class: "field-hint", text: "No original extracted text stored for this recipe." });

    body.appendChild(el("div", { class: "recipe-detail" }, [
      el("h1", { id: "recipe-detail-heading", class: "recipe-title", text: "Edit recipe" }),
      confidenceBadge(recipe),
      el("div", { class: "review-columns", style: "margin-top:0.75rem;" }, [referencePane, fields]),
      el("div", { style: "display:flex;gap:0.5rem;margin-top:1rem;flex-wrap:wrap;" }, [saveBtn, cancelBtn]),
      statusLine,
    ]));
  }

  function renderRecipeDetail(recipe) {
    const body = $("#recipe-detail-body");
    body.innerHTML = "";
    // The previous detail view's toggle is about to be destroyed; drop
    // its listener so callbacks don't accumulate one per recipe opened.
    wakeLockListeners.clear();

    const duration = makeDurationInput(`detail-time-${recipe.id}`, recipe.actual_cook_time);
    const timeSaveBtn = el("button", {
      class: "btn-secondary", type: "button", text: "Save time",
      onclick: async () => {
        const ok = await patchRating(recipe.id, { actual_cook_time: duration.getValue() || "" });
        if (ok) { announce("Cook time updated."); await loadTimeBuckets(); }
      },
    });

    body.appendChild(el("div", { class: "recipe-detail" }, [
      // Share lives at the top right of the recipe card; the source/OCR
      // badges move under the title so the corner stays a single action.
      el("div", { class: "recipe-detail-header" }, [
        el("div", { class: "recipe-detail-header-main" }, [
          el("h1", { id: "recipe-detail-heading", class: "recipe-title", text: recipe.title }),
          el("div", { class: "card-tab", style: "position:static;" }, [sourceBadge(recipe), confidenceBadge(recipe)]),
        ]),
        shareMenu(recipe),
      ]),
      showcaseImageSection(recipe),
      el("div", { class: "recipe-meta" }, [
        recipe.servings ? el("span", { text: `Servings: ${recipe.servings}` }) : null,
        recipe.prep_time ? el("span", { text: `Prep: ${recipe.prep_time}` }) : null,
        recipe.cook_time ? el("span", { text: `Cook: ${recipe.cook_time}` }) : null,
        recipe.total_time ? el("span", { text: `Total: ${recipe.total_time}` }) : null,
      ]),
      cardQuickControls(recipe, true),
      el("div", { style: "display:flex;gap:0.5rem;flex-wrap:wrap;align-items:center;" }, [
        wakeLockToggle(),
      ]),
      el("div", { class: "field" }, [
        el("label", { text: "Real-world cook time" }),
        el("div", { style: "display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;" }, [duration.element, timeSaveBtn]),
      ]),
      notesSection(recipe),
      el("h2", { text: "Ingredients" }),
      el("ul", { class: "ingredients-list" }, recipe.ingredients.map((i) =>
        el("li", { text: ingredientText(i) })
      )),
      el("h2", { text: "Instructions" }),
      el("ol", { class: "steps-list" }, recipe.steps.map((s) => el("li", { text: s.text }))),
      // Edit and Delete are grouped at the foot of the recipe: both are
      // operations on the record rather than part of reading it, and Edit
      // was previously orphaned between the ratings row and the cook-time
      // form, where it read as part of the time-logging block. Delete gets
      // btn-danger so the pair don't look like two equivalent safe actions.
      el("div", { class: "recipe-record-actions" }, [
        el("button", {
          class: "btn-secondary", type: "button", text: "✎ Edit recipe",
          onclick: () => renderRecipeEditForm(recipe),
        }),
        el("button", {
          class: "btn-secondary btn-danger", type: "button", text: "Delete recipe",
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
      const blob = await res.blob();
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
      announce("Download started.");
    } catch (err) {
      announceError("Could not prepare the download; opening it instead.");
      window.open(url, "_blank", "noopener");
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
    // Revoke late: Safari can still be reading the blob as the click is handled.
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
    if (!n) return "0 B";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
  }

  async function refreshBackupStats() {
    const elStats = $("#backup-stats");
    if (!elStats) return;
    try {
      const res = await fetch(`${API}/backup/info`);
      if (!res.ok) throw new Error(String(res.status));
      const d = await res.json();
      elStats.textContent =
        `${d.recipe_count} recipe${d.recipe_count === 1 ? "" : "s"}, `
        + `${d.upload_count} uploaded file${d.upload_count === 1 ? "" : "s"} `
        + `(${formatBytes(d.database_bytes + d.upload_bytes)} total).`;
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
  function shareActionButtons(recipe, onDone) {
    return [
      el("button", { type: "button", text: "Print", onclick: () => { onDone(); window.print(); } }),
      el("button", {
        type: "button", text: "Download PDF",
        onclick: () => {
          onDone();
          const includeNotes = recipe.notes ? confirm("Include your notes in the PDF?") : false;
          const url = `${API}/recipes/${recipe.id}/export.pdf${includeNotes ? "?include_notes=true" : ""}`;
          downloadExport(url, safeFilename(recipe.title, "pdf"));
        },
      }),
      el("button", {
        type: "button", text: "Download HTML",
        onclick: () => {
          onDone();
          downloadExport(`${API}/recipes/${recipe.id}/export.html`, safeFilename(recipe.title, "html"));
        },
      }),
      el("button", {
        type: "button", text: "Copy as text",
        onclick: async () => {
          onDone();
          const text = `${recipe.title}\n\nIngredients:\n` +
            recipe.ingredients.map((i) => `- ${ingredientText(i)}`).join("\n") +
            `\n\nInstructions:\n` + recipe.steps.map((s, idx) => `${idx + 1}. ${s.text}`).join("\n");
          try { await navigator.clipboard.writeText(text); announce("Recipe copied as text."); }
          catch { announceError("Could not copy to clipboard."); }
        },
      }),
    ];
  }

  // Share from a recipe card in the list. Cards clip their own overflow, so
  // this is a centred modal rather than the detail page's dropdown.
  function openShareSheet(recipe) {
    const overlay = $("#share-sheet-overlay");
    $("#share-sheet-title").textContent = recipe.title;
    const actions = $("#share-sheet-actions");
    actions.innerHTML = "";
    shareActionButtons(recipe, () => closeModal(overlay)).forEach((b) => actions.appendChild(b));
    openModal(overlay);
  }

  function shareMenu(recipe) {
    const menu = el("div", { class: "share-menu", id: "share-menu", hidden: "" },
      shareActionButtons(recipe, () => { menu.hidden = true; toggleBtn.setAttribute("aria-expanded", "false"); }));
    const toggleBtn = el("button", {
      class: "btn-secondary share-toggle", type: "button", "aria-haspopup": "true",
      "aria-expanded": "false", "aria-label": "Share recipe",
      html: `${SHARE_ICON_SVG}<span>Share</span>`,
      onclick: () => {
        const willOpen = menu.hidden;
        menu.hidden = !willOpen;
        toggleBtn.setAttribute("aria-expanded", String(willOpen));
      },
    });
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
      return `The server didn't respond within ${WRITE_TIMEOUT_MS / 1000} seconds. `
        + `${subject} may or may not have gone through — the list has been refreshed so you can check.`;
    }
    if (result.networkError) return `Couldn't reach Open the Pantry. Check your connection and try again.`;
    switch (result.status) {
      case 401:
      case 403: return "Not authorised — the API key is missing or wrong.";
      case 404: return `${subject} was already gone. The list has been refreshed.`;
      case 429: return "Too many requests just now. Wait a moment and try again.";
      default:
        return result.status >= 500
          ? `The server returned an error (${result.status}). Nothing was changed.`
          : `That didn't work (status ${result.status}).`;
    }
  }

  async function deleteRecipe(id, btn) {
    if (!confirm("Delete this recipe? This cannot be undone.")) return;

    // Guard against a double-fire, but deliberately do NOT touch the modal's
    // close button or the Escape handler: the modal now stays open until the
    // request resolves, so those are the only way out if this hangs.
    if (btn) { btn.disabled = true; btn.textContent = "Deleting…"; }
    const restore = () => { if (btn) { btn.disabled = false; btn.textContent = "Delete recipe"; } };

    const result = await fetchWithTimeout(`${API}/recipes/${id}`, { method: "DELETE" });

    if (result.ok) {
      closeModal(detailOverlay);
      announce("Recipe deleted.");
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
      announceError(writeFailureMessage(result, "That recipe"));
      return;
    }

    // Everything else: stay put, say what happened, let them retry. On a
    // timeout the outcome is genuinely unknown, so refresh the list behind
    // the modal rather than claiming either result -- again announcing last.
    restore();
    if (result.timedOut) await loadRecipes();
    announceError(writeFailureMessage(result, "The recipe"));
  }

  // -------------------------------------------------------------------
  // Add-recipe modal: source picker -> ingestion form -> review -> save
  // -------------------------------------------------------------------
  const addOverlay = $("#add-recipe-overlay");
  const addBody = $("#add-recipe-body");

  async function discardCurrentDraftFiles() {
    const files = [...state.currentDraftFiles];
    state.currentDraftFiles.clear();
    await Promise.all(files.map((f) =>
      fetch(`${API}/ingest/draft/${encodeURIComponent(f)}`, { method: "DELETE" }).catch(() => {})
    ));
  }

  function trackDraftFile(filename) {
    if (filename) state.currentDraftFiles.add(filename);
  }

  $("#add-recipe-fab").addEventListener("click", () => { state.currentDraftFiles.clear(); showSourcePicker(); openModal(addOverlay); });
  $("#add-recipe-close").addEventListener("click", async () => { await discardCurrentDraftFiles(); closeModal(addOverlay); });
  addOverlay.addEventListener("click", async (e) => { if (e.target === addOverlay) { await discardCurrentDraftFiles(); closeModal(addOverlay); } });

  function showSourcePicker() {
    addBody.innerHTML = "";
    const picker = el("div", { class: "source-type-picker" }, [
      el("button", { type: "button", text: "\ud83d\udd17 URL", onclick: showUrlForm }),
      el("button", { type: "button", text: "\ud83d\udcc4 PDF", onclick: showPdfForm }),
      el("button", { type: "button", text: "\ud83d\udcf7 Screenshot/Photo", onclick: showImageForm }),
      el("button", { type: "button", text: "\u270d\ufe0f Manual/Handwritten", onclick: showManualForm }),
    ]);
    addBody.appendChild(picker);

    // Only offered once email ingest is switched on; otherwise it would be
    // a button that can only say "not configured".
    fetch(`${API}/email-settings`).then((r) => (r.ok ? r.json() : null)).then((settings) => {
      if (!settings || !settings.enabled || !picker.isConnected) return;
      const status = el("p", { class: "field-hint scan-status", role: "status" });
      const btn = el("button", {
        type: "button", text: "\ud83d\udce5 Check email inbox",
        onclick: () => runInboxScan(status, btn),
      });
      picker.appendChild(btn);
      addBody.appendChild(status);
    }).catch(() => {});
  }

  function renderBatchResults(container, result) {
    container.innerHTML = "";
    const list = el("ul", { class: "batch-results-list" });
    for (const s of result.succeeded) list.appendChild(el("li", { class: "ok", text: `\u2713 ${s.title}` }));
    for (const f of result.failed) list.appendChild(el("li", { class: "fail", text: `\u2717 ${f.url || f.filename}: ${f.error}` }));
    container.appendChild(list);
  }

  function modeSwitchControl(onSingle, onBatch, labels = ["Single", "Batch"]) {
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

  function showUrlForm() {
    addBody.innerHTML = "";
    addBody.appendChild(el("h3", { text: "Add from URL" }));
    const formArea = el("div");

    function renderSingle() {
      formArea.innerHTML = "";
      const input = el("input", { type: "url", id: "url-input", required: "", placeholder: "https://example.com/recipe" });
      formArea.appendChild(el("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          setBusy(true, "Fetching recipe...");
          try {
            const res = await fetch(`${API}/ingest/url`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ url: input.value.trim() }),
            });
            if (!res.ok) { const err = await res.json(); throw new Error(err.detail || "Extraction failed."); }
            const draft = await res.json();
            trackDraftFile(draft.image_path);
            showReviewScreen(draft);
          } catch (err) {
            alert(err.message + "\n\nYou can add this recipe manually instead.");
            showSourcePicker();
          } finally { setBusy(false); }
        },
      }, [
        el("div", { class: "field" }, [el("label", { for: "url-input", text: "Recipe URL" }), input]),
        el("button", { class: "btn-primary", type: "submit", text: "Fetch recipe" }),
      ]));
    }

    function renderBatch() {
      formArea.innerHTML = "";
      const textarea = el("textarea", { id: "url-batch-input", placeholder: "One URL per line", style: "min-height:8rem;" });
      const resultsEl = el("div");
      formArea.appendChild(el("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          const urls = textarea.value.split("\n").map((s) => s.trim()).filter(Boolean);
          if (!urls.length) return;
          if (urls.length > 50) {  // MAX_BATCH_URLS on the server
            resultsEl.textContent = `That's ${urls.length} links; the limit is 50 per batch. Send the rest separately.`;
            return;
          }
          setBusy(true, `Fetching ${urls.length} recipe(s)...`);
          try {
            const res = await fetch(`${API}/ingest/url/batch`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ urls }),
            });
            const result = await res.json();
            if (!res.ok) { resultsEl.textContent = result.detail || `Batch import failed (error ${res.status}).`; return; }
            renderBatchResults(resultsEl, result);
            announce(`${result.succeeded.length} added, ${result.failed.length} failed.`);
            await loadTags(); await loadTimeBuckets(); loadRecipes();
          } catch {
            alert("Batch import failed.");
          } finally { setBusy(false); }
        },
      }, [
        el("div", { class: "field" }, [
          el("label", { for: "url-batch-input", text: "Recipe URLs (one per line)" }),
          textarea,
          el("div", { class: "field-hint", text: "Each recipe is saved directly using auto-detected fields \u2014 open it afterward to correct anything." }),
        ]),
        el("button", { class: "btn-primary", type: "submit", text: "Import all" }),
        resultsEl,
      ]));
    }

    addBody.appendChild(modeSwitchControl(renderSingle, renderBatch));
    addBody.appendChild(formArea);
    renderSingle();
  }

  function showPdfForm() {
    addBody.innerHTML = "";
    addBody.appendChild(el("h3", { text: "Add from PDF" }));
    const formArea = el("div");

    function renderSingle() {
      formArea.innerHTML = "";
      const input = el("input", { type: "file", id: "pdf-input", accept: "application/pdf", required: "" });
      formArea.appendChild(el("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          if (!input.files.length) return;
          setBusy(true, "Reading PDF (this can take a moment for scanned pages)...");
          try {
            const fd = new FormData(); fd.append("file", input.files[0]);
            const res = await fetch(`${API}/ingest/pdf`, { method: "POST", body: fd });
            if (!res.ok) throw new Error("Could not process PDF.");
            const draft = await res.json();
            trackDraftFile(draft.stored_file);
            trackDraftFile(draft.image_path);
            showReviewScreen(draft);
          } catch (err) {
            alert(err.message);
            showSourcePicker();
          } finally { setBusy(false); }
        },
      }, [
        el("div", { class: "field" }, [
          el("label", { for: "pdf-input", text: "PDF file" }),
          input,
          el("div", { class: "field-hint", text: "Text-based PDFs are read directly; scanned pages fall back to OCR automatically." }),
        ]),
        el("button", { class: "btn-primary", type: "submit", text: "Process PDF" }),
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
            resultsEl.textContent = `That's ${input.files.length} PDFs; the limit is 20 per batch. Send the rest separately.`;
            return;
          }
          setBusy(true, `Processing ${input.files.length} PDF(s)...`);
          try {
            const fd = new FormData();
            for (const f of input.files) fd.append("files", f);
            const res = await fetch(`${API}/ingest/pdf/batch`, { method: "POST", body: fd });
            const result = await res.json();
            if (!res.ok) { resultsEl.textContent = result.detail || `Batch import failed (error ${res.status}).`; return; }
            renderBatchResults(resultsEl, result);
            announce(`${result.succeeded.length} added, ${result.failed.length} failed.`);
            await loadTags(); await loadTimeBuckets(); loadRecipes();
          } catch {
            alert("Batch import failed.");
          } finally { setBusy(false); }
        },
      }, [
        el("div", { class: "field" }, [
          el("label", { for: "pdf-batch-input", text: "PDF files" }),
          input,
          el("div", { class: "field-hint", text: "Each is saved directly using auto-detected fields." }),
        ]),
        el("button", { class: "btn-primary", type: "submit", text: "Import all" }),
        resultsEl,
      ]));
    }

    addBody.appendChild(modeSwitchControl(renderSingle, renderBatch));
    addBody.appendChild(formArea);
    renderSingle();
  }

  function showImageForm() {
    addBody.innerHTML = "";
    addBody.appendChild(el("h3", { text: "Add from screenshot / photo" }));
    const formArea = el("div");

    function renderSingle() {
      formArea.innerHTML = "";
      const input = el("input", { type: "file", id: "image-input", accept: "image/*", capture: "environment", required: "" });
      formArea.appendChild(el("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          if (!input.files.length) return;
          setBusy(true, "Reading image with OCR...");
          try {
            const fd = new FormData(); fd.append("file", input.files[0]);
            const res = await fetch(`${API}/ingest/image`, { method: "POST", body: fd });
            if (!res.ok) throw new Error("Could not process image.");
            const draft = await res.json();
            trackDraftFile(draft.stored_file);
            showReviewScreen(draft);
          } catch (err) {
            alert(err.message);
            showSourcePicker();
          } finally { setBusy(false); }
        },
      }, [
        el("div", { class: "field" }, [
          el("label", { for: "image-input", text: "Screenshot or photo" }),
          input,
          el("div", { class: "field-hint", text: "Best for rendered/screenshot text. Handwriting recognizes poorly \u2014 use Manual entry for handwritten cards instead." }),
        ]),
        el("button", { class: "btn-primary", type: "submit", text: "Process image" }),
      ]));
    }

    function renderCombine() {
      formArea.innerHTML = "";
      const input = el("input", { type: "file", id: "image-combine-input", accept: "image/*", multiple: "", required: "" });
      formArea.appendChild(el("form", {
        onsubmit: async (e) => {
          e.preventDefault();
          if (!input.files.length) return;
          setBusy(true, `Reading ${input.files.length} image(s) with OCR...`);
          try {
            const fd = new FormData();
            for (const f of input.files) fd.append("files", f);
            const res = await fetch(`${API}/ingest/images`, { method: "POST", body: fd });
            if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || "Could not process images."); }
            const draft = await res.json();
            trackDraftFile(draft.stored_file);
            showReviewScreen(draft);
          } catch (err) {
            alert(err.message);
            showSourcePicker();
          } finally { setBusy(false); }
        },
      }, [
        el("div", { class: "field" }, [
          el("label", { for: "image-combine-input", text: "Screenshots (select all, in reading order)" }),
          input,
          el("div", { class: "field-hint", text: "For a recipe that spans multiple screenshots \u2014 each is read with OCR and combined into one recipe. Most file pickers preserve the order you select files in; the first image becomes the showcase photo (changeable afterward)." }),
        ]),
        el("button", { class: "btn-primary", type: "submit", text: "Process & combine" }),
      ]));
    }

    addBody.appendChild(modeSwitchControl(renderSingle, renderCombine, ["Single", "Combine multiple"]));
    addBody.appendChild(formArea);
    renderSingle();
  }

  function setBusy(isBusy, msg) {
    if (isBusy) announce(msg || "Working...");
    $$(".modal button, .modal input").forEach((n) => { n.disabled = isBusy; });
  }

  // -------------------------------------------------------------------
  // Review / edit screen (shared by URL / PDF / screenshot ingestion)
  // DOM order is raw-text-first, then editable fields, so screen readers
  // encounter the source material before the fields meant to correct it.
  // -------------------------------------------------------------------
  function showReviewScreen(draft) {
    addBody.innerHTML = "";
    addBody.appendChild(el("h3", { text: "Review & confirm" }));

    if (draft.ocr_confidence != null) {
      const tier = draft.ocr_confidence >= 80 ? "high" : draft.ocr_confidence >= 55 ? "med" : "low";
      const msg = tier === "low"
        ? "OCR quality was low on this input \u2014 please check the fields carefully."
        : `OCR confidence: ${Math.round(draft.ocr_confidence)}%`;
      addBody.appendChild(el("div", { class: `badge badge-confidence-${tier}`, text: msg, style: "margin-bottom:0.75rem;display:inline-block;" }));
    }

    const titleInput = el("input", { type: "text", id: "review-title", value: draft.title || "" });
    const ingredientsArea = el("textarea", { id: "review-ingredients" });
    ingredientsArea.value = (draft.ingredients || [])
      .map((i) => ingredientText(i))
      .join("\n");
    const stepsArea = el("textarea", { id: "review-steps" });
    stepsArea.value = (draft.steps || []).map((s) => (typeof s === "string" ? s : s.text)).join("\n");

    const tagsInput = el("input", {
      type: "text", id: "review-tags",
      value: (draft.suggested_tags || []).map((t) => t.name).join(", "),
    });

    const duration = makeDurationInput("review-time");

    let useImage = !!draft.image_path;
    const imagePreviewWrap = el("div", { class: "field" });
    function renderImagePreview() {
      imagePreviewWrap.innerHTML = "";
      if (!draft.image_path || !useImage) return;
      imagePreviewWrap.appendChild(el("label", { text: "Showcase image (auto-detected)" }));
      imagePreviewWrap.appendChild(el("img", {
        src: `/tmp-preview/${draft.image_path}`, alt: "",
        style: "max-width:100%;max-height:12rem;border-radius:var(--radius);display:block;margin-bottom:0.4rem;",
      }));
      imagePreviewWrap.appendChild(el("button", {
        class: "btn-secondary", type: "button", text: "Don't use this image",
        onclick: () => { useImage = false; renderImagePreview(); },
      }));
    }
    renderImagePreview();

    const columns = el("div", { class: "review-columns" }, [
      el("div", {}, [
        el("h4", { text: "Extracted text (source)" }),
        el("div", { class: "review-raw", text: draft.raw_text || "(no raw text captured)" }),
      ]),
      el("div", {}, [
        el("div", { class: "field" }, [el("label", { for: "review-title", text: "Title" }), titleInput]),
        imagePreviewWrap,
        el("div", { class: "field" }, [
          el("label", { for: "review-ingredients", text: "Ingredients (one per line)" }),
          ingredientsArea,
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "review-steps", text: "Steps (one per line)" }),
          stepsArea,
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "review-tags", text: "Tags (comma or semicolon separated)" }),
          tagsInput,
          el("div", { class: "field-hint", text: "Auto-suggested from keywords \u2014 edit freely." }),
        ]),
        el("div", { class: "field" }, [
          el("label", { text: "Real-world cook time (optional)" }),
          duration.element,
          el("div", { class: "field-hint", text: "How long it actually took you \u2014 separate from any time listed by the source. Leave blank to skip." }),
        ]),
      ]),
    ]);
    addBody.appendChild(columns);

    const saveBtn = el("button", {
      class: "btn-primary", type: "button", text: "Save recipe",
      style: "margin-top:1rem;",
      onclick: async () => {
        setBusy(true, "Saving recipe...");
        try {
          const tagNames = tagsInput.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
          const tagObjs = tagNames.map((name) => {
            const known = state.allTags.find((t) => t.name.toLowerCase() === name.toLowerCase());
            return known
              ? { name: known.name, category: known.category, subgroup: known.subgroup }
              : { name, category: "custom", subgroup: null };
          });

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
            title: titleInput.value.trim() || "Untitled Recipe",
            source_type: draft.source_type,
            source_url: draft.source_url || null,
            servings: draft.servings || null,
            prep_time: draft.prep_time || null,
            cook_time: draft.cook_time || null,
            total_time: draft.total_time || null,
            image_path: imageRef,
            raw_text: draft.raw_text || null,
            ocr_confidence: draft.ocr_confidence != null ? draft.ocr_confidence : null,
            actual_cook_time: duration.getValue(),
            ingredients: ingredientsArea.value.split("\n").filter((l) => l.trim())
              .map((line) => ({ raw_line: line, name: line })),
            steps: stepsArea.value.split("\n").filter((l) => l.trim()),
            tags: tagObjs,
          };

          const res = await fetch(`${API}/recipes`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (!res.ok) throw new Error("Could not save recipe.");

          // Every tracked draft file for this session gets cleaned up
          // except whichever one actually became the recipe's image
          // (promoted server-side already) -- e.g. a source PDF's own temp
          // file is never kept even when its extracted thumbnail is.
          const filesToDiscard = [...state.currentDraftFiles].filter((f) => f !== imageRef);
          state.currentDraftFiles.clear();
          filesToDiscard.forEach((f) => {
            fetch(`${API}/ingest/draft/${encodeURIComponent(f)}`, { method: "DELETE" }).catch(() => {});
          });

          closeModal(addOverlay);
          announce("Recipe saved.");
          await loadTags(); await loadTimeBuckets();
          loadRecipes();
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
    addBody.appendChild(el("h3", { text: "Manual / handwritten entry" }));

    const titleInput = el("input", { type: "text", id: "manual-title", required: "" });
    const ingredientsArea = el("textarea", { id: "manual-ingredients", placeholder: "One ingredient per line" });
    const stepsArea = el("textarea", { id: "manual-steps", placeholder: "One step per line" });
    const tagsInput = el("input", { type: "text", id: "manual-tags", placeholder: "e.g. dinner, stovetop, beef" });
    const imageInput = el("input", { type: "file", id: "manual-image", accept: "image/*" });
    const duration = makeDurationInput("manual-time");

    const form = el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        setBusy(true, "Saving recipe...");
        let storedFile = null;
        try {
          if (imageInput.files.length) {
            const fd = new FormData(); fd.append("file", imageInput.files[0]);
            const upRes = await fetch(`${API}/upload-image`, { method: "POST", body: fd });
            if (!upRes.ok) {
              // Used to save the recipe without the photo and say nothing.
              const err = await upRes.json().catch(() => ({}));
              const why = typeof err.detail === "string" ? err.detail : `error ${upRes.status}`;
              announceError(`The photo couldn't be uploaded (${why}). Nothing was saved; remove the photo or try another.`);
              return;
            }
            storedFile = (await upRes.json()).stored_file;
          }

          const tagNames = tagsInput.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
          const tagObjs = tagNames.map((name) => {
            const known = state.allTags.find((t) => t.name.toLowerCase() === name.toLowerCase());
            return known
              ? { name: known.name, category: known.category, subgroup: known.subgroup }
              : { name, category: "custom", subgroup: null };
          });

          const payload = {
            title: titleInput.value.trim() || "Untitled Recipe",
            source_type: "manual",
            image_path: storedFile,
            actual_cook_time: duration.getValue(),
            ingredients: ingredientsArea.value.split("\n").filter((l) => l.trim()).map((line) => ({ raw_line: line, name: line })),
            steps: stepsArea.value.split("\n").filter((l) => l.trim()),
            tags: tagObjs,
          };

          const res = await fetch(`${API}/recipes`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (!res.ok) throw new Error("Could not save recipe.");
          closeModal(addOverlay);
          announce("Recipe saved.");
          await loadTags(); await loadTimeBuckets();
          loadRecipes();
        } catch (err) {
          if (storedFile) fetch(`${API}/ingest/draft/${encodeURIComponent(storedFile)}`, { method: "DELETE" }).catch(() => {});
          alert(err.message);
        } finally { setBusy(false); }
      },
    }, [
      el("div", { class: "field" }, [el("label", { for: "manual-title", text: "Title" }), titleInput]),
      el("div", { class: "field" }, [el("label", { for: "manual-ingredients", text: "Ingredients (one per line)" }), ingredientsArea]),
      el("div", { class: "field" }, [el("label", { for: "manual-steps", text: "Steps (one per line)" }), stepsArea]),
      el("div", { class: "field" }, [el("label", { for: "manual-tags", text: "Tags (comma or semicolon separated)" }), tagsInput]),
      el("div", { class: "field" }, [
        el("label", { text: "Real-world cook time (optional)" }),
        duration.element,
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "manual-image", text: "Attach a photo of the card (optional)" }),
        imageInput,
      ]),
      el("button", { class: "btn-primary", type: "submit", text: "Save recipe" }),
    ]);
    addBody.appendChild(form);
  }

  // -------------------------------------------------------------------
  // Init
  // -------------------------------------------------------------------
  (async function init() {
    $("#filters-toggle").hidden = state.grouping !== "all";
    await loadTags();
    await loadTimeBuckets();
    await loadRecipes();
  })();

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/service-worker.js").catch(() => {});
    });
  }
})();
