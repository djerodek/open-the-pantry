(() => {
  "use strict";

  const API = "/api";

  // Interface language helpers (js/i18n.js). Static page text is
  // translated first, before anything is rendered from script.
  const { lang: LANG, setLang, tx, txn, tagLabel, groupLabel, valueLabel, fmtDateTime, durationLabel, translateStatic } = window.OTP_I18N;
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
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // Pinch zoom: Safari has ignored user-scalable=no since iOS 10, so the
  // gesture is cancelled here as well. Settings -> Text size replaces it.
  document.addEventListener("gesturestart", (e) => e.preventDefault(), { passive: false });

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
      if (v === null || v === undefined) continue;
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
      if (openOverlay === detailOverlay) releaseWakeLock();
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
        status.textContent = tx("Building a PDF of every recipe. This can take a while.");
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

  $("#settings-toggle").addEventListener("click", () => { refreshBackupStats(); openModal(settingsOverlay); });
  $("#settings-close").addEventListener("click", () => closeModal(settingsOverlay));
  settingsOverlay.addEventListener("click", (e) => { if (e.target === settingsOverlay) closeModal(settingsOverlay); });

  const shareSheetOverlay = $("#share-sheet-overlay");
  $("#share-sheet-close").addEventListener("click", () => closeModal(shareSheetOverlay));
  shareSheetOverlay.addEventListener("click", (e) => { if (e.target === shareSheetOverlay) closeModal(shareSheetOverlay); });

  const emailSheetOverlay = $("#email-sheet-overlay");
  $("#email-sheet-close").addEventListener("click", () => closeModal(emailSheetOverlay));
  emailSheetOverlay.addEventListener("click", (e) => { if (e.target === emailSheetOverlay) closeModal(emailSheetOverlay); });

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

  // -------------------------------------------------------------------
  // Settings -> HTTPS (https_setup.py does the work). The cPanel login comes
  // from .env; everything else is set here.
  // -------------------------------------------------------------------
  const httpsPanel = $("#https-settings-panel");
  const httpsToggle = $("#https-settings-toggle");
  const httpsSummary = $("#https-summary");
  let httpsPollTimer = null;
  let httpsLastState = null;

  httpsToggle.addEventListener("click", async () => {
    const willOpen = httpsPanel.hidden;
    httpsPanel.hidden = !willOpen;
    httpsToggle.setAttribute("aria-expanded", String(willOpen));
    if (willOpen) await renderHttpsSettings();
    else clearTimeout(httpsPollTimer);
  });

  async function fetchHttpsStatus() {
    const res = await fetch(`${API}/https`);
    if (!res.ok) throw new Error(String(res.status));
    return res.json();
  }

  function httpsSummaryText(st) {
    if (st.state === "working") return tx("Setting up HTTPS…");
    if (st.enabled && st.url) return tx("On: {1}", { 1: st.url });
    return null;
  }

  async function refreshHttpsSummary() {
    try {
      const st = await fetchHttpsStatus();
      const text = httpsSummaryText(st);
      if (text) httpsSummary.textContent = text;
    } catch { /* keep the static hint */ }
  }

  function isIPv4(host) {
    return /^\d{1,3}(\.\d{1,3}){3}$/.test(host);
  }

  async function renderHttpsSettings() {
    clearTimeout(httpsPollTimer);
    let st;
    try {
      st = await fetchHttpsStatus();
    } catch {
      httpsPanel.innerHTML = "";
      httpsPanel.appendChild(el("div", { class: "field-hint", text: tx("Could not load the HTTPS settings.") }));
      return;
    }
    const keep = {};  // values typed so far survive a re-render while polling
    $$("input", httpsPanel).forEach((i) => { keep[i.id] = i.value; });
    httpsPanel.innerHTML = "";
    const summary = httpsSummaryText(st);
    if (summary) httpsSummary.textContent = summary;

    if (!st.credentials.found) {
      const box = el("div", { class: "setup-callout" }, [
        el("strong", { text: tx("Step 1: add your cPanel login") }),
        el("p", { text: tx("The app creates its DNS records through your domain's cPanel. For safety the login isn't entered here: put it in a file named .env next to docker-compose.yml, then restart the app (docker compose up -d).") }),
        el("p", { text: tx("In cPanel: Security → Manage API Tokens → Create. Then add these lines to .env:") }),
        el("pre", { class: "setup-code", text: "CPANEL_USERNAME=your-cpanel-username\nCPANEL_TOKEN=paste-the-api-token\nCPANEL_BASE_URL=https://your-cpanel-address:2083" }),
        el("p", { class: "field-hint", text: tx("Missing now: {1}", { 1: st.credentials.missing.join(", ") }) }),
      ]);
      httpsPanel.appendChild(box);
    } else {
      httpsPanel.appendChild(el("div", { class: "field-hint", text: tx("cPanel login found ({1}).", { 1: st.credentials.host }) }));
    }

    const disabled = !st.credentials.found || st.state === "working";
    const field = (id, label, input, hint) => el("div", { class: "field" }, [
      el("label", { for: id, text: label }), input, hint ? el("div", { class: "field-hint", text: hint }) : null,
    ]);
    const guessLan = st.lan_address || (isIPv4(location.hostname) ? location.hostname : "");
    const domainIn = el("input", { type: "text", id: "https-domain", placeholder: "pantry.example.com",
      autocomplete: "off", autocapitalize: "off", spellcheck: "false", value: keep["https-domain"] ?? st.domain });
    const lanIn = el("input", { type: "text", id: "https-lan", placeholder: "192.168.1.20", inputmode: "decimal",
      autocomplete: "off", value: keep["https-lan"] ?? guessLan });
    const emailIn = el("input", { type: "email", id: "https-email", placeholder: "you@example.com",
      autocomplete: "email", value: keep["https-email"] ?? (st.email || state.mail.from || "") });
    const portIn = el("input", { type: "number", id: "https-port", min: "1", max: "65535",
      value: keep["https-port"] ?? String(st.port || 8443) });
    [domainIn, lanIn, emailIn, portIn].forEach((i) => { i.disabled = disabled; });

    httpsPanel.appendChild(field("https-domain", tx("Address"), domainIn,
      tx("A name in your domain that isn't in use yet. The app creates its DNS record.")));
    httpsPanel.appendChild(field("https-lan", tx("This server's address on your network"), lanIn,
      tx("Where the name will point. Filled in from the address you're using now, if it's a number.")));
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
        const res = await fetch(`${API}/https/setup`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ domain: domainIn.value.trim(), lan_address: lanIn.value.trim(),
            email: emailIn.value.trim(), port: parseInt(portIn.value, 10) || 8443 }),
        }).catch(() => null);
        if (!res || !res.ok) {
          const body = res ? await res.json().catch(() => ({})) : {};
          announceError(apiErrorText(body, res ? res.status : 0));
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
          const res = await fetch(`${API}/https`, { method: "DELETE" }).catch(() => null);
          if (!res || !res.ok) { announceError(tx("Couldn't reach Open the Pantry. Check your connection and try again.")); return; }
          announce(tx("HTTPS turned off."));
          httpsSummary.textContent = tx("Optional. A secure https:// address on your network, with a free Let's Encrypt certificate. Needed for keeping the screen awake.");
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
      httpsPollTimer = setTimeout(() => { if (!httpsPanel.hidden) renderHttpsSettings(); }, 2000);
    }
  }

  // -------------------------------------------------------------------
  // Settings -> Logs: view (newest at the bottom), download, clear.
  // -------------------------------------------------------------------
  const logsPanel = $("#logs-panel");
  const logsToggle = $("#logs-view-toggle");
  let logsProblemsOnly = false;

  logsToggle.addEventListener("click", async () => {
    const willOpen = logsPanel.hidden;
    logsPanel.hidden = !willOpen;
    logsToggle.setAttribute("aria-expanded", String(willOpen));
    logsToggle.textContent = willOpen ? tx("Hide log") : tx("View log");
    if (willOpen) await renderLogs();
  });

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
        const res = await fetch(`${API}/logs?lines=500&problems=${logsProblemsOnly}`);
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

  $("#logs-download").addEventListener("click", async () => {
    try {
      const res = await fetch(`${API}/logs/download`);
      if (!res.ok) throw new Error(String(res.status));
      saveBlob(await res.blob(), filenameFromResponse(res) || "open-the-pantry-log.txt");
      announce(tx("Download started."));
    } catch {
      announceError(tx("Couldn't reach Open the Pantry, so nothing was downloaded. Check your connection and try again."));
    }
  });

  $("#logs-clear").addEventListener("click", async () => {
    if (!confirm(tx("Clear the log? Everything recorded so far is deleted. Download it first if you might need it."))) return;
    const result = await fetchWithTimeout(`${API}/logs`, { method: "DELETE" });
    if (!result.ok) { announceError(writeFailureMessage(result, tx("The log"))); return; }
    announce(tx("Log cleared."));
    if (!logsPanel.hidden) await renderLogs();
  });

  // One scan routine for both places it can be started: Settings (next to
  // the connection settings, for testing) and the + menu (for everyday use).
  // Shows the summary plus one line per email -- the per-email lines carry
  // the reason for each failure, and their only other route is the result
  // email, which can't arrive when sending is what's broken.
  async function runInboxScan(statusEl, button) {
    statusEl.textContent = tx("Checking the inbox\u2026");
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
        if (body.succeeded) { await loadTags(); await loadTimeBuckets(); loadRecipes(); }
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
      const res = await fetch(`${API}/email-settings`);
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
              const res = await fetch(`${API}/email-settings/encryption-key`, { method: "POST" });
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
          const res = await fetch(`${API}/email-settings/test`, { method: "POST" });
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

  async function loadMailState() {
    try {
      const res = await fetch(`${API}/email-settings`);
      if (res.ok) setMailState(await res.json());
    } catch { /* offline: the Email PDF action just stays hidden */ }
  }

  async function saveRecentRecipients(list) {
    const res = await fetch(`${API}/email-settings/recipients`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recipients: list }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(typeof err.detail === "string" ? err.detail : tx("Could not save."));
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
          const res = await fetch(`${API}/recipes/${recipe.id}/email`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ to, message: messageInput.value, include_notes: Boolean(notesBox && notesBox.checked) }),
          });
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
    // Share -> Email PDF: whether sending is set up, the From address, and
    // recent recipients. Read from the email settings at start-up.
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
      + (state.query ? 1 : 0);
  }

  function clearAllFilters() {
    state.filterTags.clear();
    state.maxMinutes = null;
    uiTimeLevel1 = null;
    state.pace.clear();
    state.query = "";
    const searchInput = $("#search-input");
    if (searchInput) searchInput.value = "";
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

  async function loadTags() {
    const [res, gres] = await Promise.all([fetch(`${API}/tags`), fetch(`${API}/tag-groups`).catch(() => null)]);
    state.allTags = await res.json();
    if (gres && gres.ok) state.tagGroups = await gres.json();
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
    const res = await fetch(`${API}/time-buckets`);
    state.timeBuckets = await res.json();
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
          const res = await fetch(`${API}/recipes/${c.id}/autotag-ignore`, {
            method: "PATCH", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ignored: true }),
          }).catch(() => null);
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
        const res = await fetch(`${API}/tags/auto-apply`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ selections: Object.entries(selections).map(([id, tags]) => ({ id: Number(id), tags })) }),
        });
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
      const err = res ? await res.json().catch(() => ({})) : {};
      announceError(typeof err.detail === "string" ? err.detail : tx("Couldn't save that change."));
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

  // Photos on/off: a switch in Settings (it was a toolbar button). The list
  // behind the Settings dialog updates as soon as it's flipped.
  const thumbnailsSetting = $("#thumbnails-setting");
  thumbnailsSetting.checked = state.showThumbnails;
  thumbnailsSetting.addEventListener("change", () => {
    state.showThumbnails = thumbnailsSetting.checked;
    try { localStorage.setItem("recipe-app-show-thumbnails", String(state.showThumbnails)); } catch { /* private mode */ }
    renderRecipeList(currentRecipes);
  });

  $("#batch-cancel-btn").addEventListener("click", () => {
    state.selectMode = false;
    state.selectedIds.clear();
    $("#select-mode-toggle").setAttribute("aria-pressed", "false");
    updateBatchBar();
    renderRecipeList(currentRecipes);
  });

  $("#batch-delete-btn").addEventListener("click", async () => {
    if (!state.selectedIds.size) return;
    if (!confirm(tx("Delete {1} recipe(s)? This cannot be undone.", { 1: state.selectedIds.size }))) return;
    const ids = [...state.selectedIds];
    const btn = $("#batch-delete-btn");
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = tx("Deleting…");

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
      const parts = [tx("{1} deleted", { 1: body.deleted.length })];
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

  async function loadRecipes() {
    const params = new URLSearchParams();
    if (state.query) params.set("q", state.query);
    for (const t of state.filterTags) params.append("tags", t);
    if (state.maxMinutes != null) params.set("max_minutes", String(state.maxMinutes));
    for (const p of state.pace) params.append("pace", p);
    params.set("sort", state.sort);
    params.set("direction", state.direction);
    // An error used to be parsed as the recipe list, which came out empty
    // and read "No recipes match" -- e.g. a 429 from the rate limiter.
    let res;
    try {
      res = await fetch(`${API}/recipes?${params.toString()}`);
    } catch {
      showListError(tx("Couldn't reach Open the Pantry. Check your connection and try again."));
      return;
    }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showListError(res.status === 429
        ? tx("The server is busy with too many requests. Wait a moment and try again.")
        : tx("Couldn't load recipes ({1}).", { 1: typeof err.detail === "string" ? err.detail : tx("error {1}", { 1: res.status }) }));
      return;
    }
    currentRecipes = await res.json();
    renderRecipeList(currentRecipes);
    announce(txn(currentRecipes.length, "{n} recipe found", "{n} recipes found"));
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
    const label = { url: tx("Web"), pdf: "PDF", screenshot: tx("Screenshot"), manual: tx("Handwritten/Manual"), email: tx("Email") }[recipe.source_type] || recipe.source_type;
    return el("span", { class: `badge badge-source-${recipe.source_type}`, text: label });
  }

  function confidenceBadge(recipe) {
    if (recipe.ocr_confidence == null) return null;
    const c = recipe.ocr_confidence;
    const tier = c >= 80 ? "high" : c >= 55 ? "med" : "low";
    const label = tier === "low" ? tx("OCR quality: low") : tier === "med" ? tx("OCR quality: fair") : tx("OCR quality: good");
    return el("span", { class: `badge badge-confidence-${tier}`, text: label });
  }

  function matchedViaBadge(recipe) {
    if (!recipe.matched_via || !recipe.matched_via.length) return null;
    return el("span", { class: "badge badge-matched", text: tx("matched: {1}", { 1: recipe.matched_via.join(", ") }) });
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

    // Setting a rating by mistake needs a way out, and on a card there is no
    // other affordance for it.
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
        text: ratingOptionLabel(field, opt),
        onclick: async () => {
          popover.hidden = true;
          btn.setAttribute("aria-expanded", "false");
          const ok = await patchRating(recipe.id, { [field]: opt });
          if (ok) applyRating(recipe, field, opt, btn, icon, shortLabel);
          else announceError(tx("Could not save rating."));
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
      "aria-label": recipe.favorite ? tx("Remove from favorites") : tx("Add to favorites"),
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
      region.appendChild(el("div", { class: "empty-state", text: tx("No recipes match. Tap + to add one, or adjust your filters.") }));
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
    closeModal(detailOverlay);
  }

  $("#recipe-detail-close").addEventListener("click", closeRecipeDetail);
  detailOverlay.addEventListener("click", (e) => { if (e.target === detailOverlay) closeRecipeDetail(); });

  async function openRecipeDetail(id) {
    const res = await fetch(`${API}/recipes/${id}`);
    if (!res.ok) { announceError(tx("Could not load recipe.")); return; }
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
        const patchRes = await fetch(`${API}/recipes/${recipe.id}/image`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image_path: stored_file }),
        });
        if (patchRes.ok) {
          recipe.image_path = (await patchRes.json()).image_path;
          announce(tx("Photo updated."));
          render();
          loadRecipes();  // keep the list thumbnail in sync
        } else {
          fetch(`${API}/ingest/draft/${encodeURIComponent(stored_file)}`, { method: "DELETE" }).catch(() => {});
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
            const res = await fetch(`${API}/recipes/${recipe.id}/image`, {
              method: "PATCH", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ image_path: null }),
            });
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
      value: recipe.tags.map((t) => tagLabel(t)).join(", "),
    });
    // Suggested tags keep the tagger's category (Oven is a cooking style),
    // not "custom", when saved -- including ones that don't exist yet.
    const suggestedCats = new Map();
    const currentTagNames = () => tagsInput.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
    const suggestHint = el("div", { class: "field-hint", role: "status" });
    const suggestBtn = el("button", {
      type: "button", class: "btn-secondary btn-small", text: tx("Suggest tags"),
      onclick: async () => {
        suggestBtn.disabled = true;
        try {
          const res = await fetch(`${API}/tags/suggest`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              title: titleInput.value, current_tags: currentTagNames(),
              ingredients: ingredientsArea.value.split("\n").filter((l) => l.trim()),
              steps: stepsArea.value.split("\n").filter((l) => l.trim()),
            }),
          });
          if (!res.ok) throw new Error(`error ${res.status}`);
          const found = await res.json();
          found.forEach((t) => suggestedCats.set(tagLabel(t).toLowerCase(), t));
          if (found.length) tagsInput.value = [...currentTagNames(), ...found.map((t) => tagLabel(t))].join(", ");
          suggestHint.textContent = found.length
            ? tx("Added {1}. Remove any that don't fit, then save.", { 1: found.map((t) => tagLabel(t)).join(", ") })
            : tx("No new suggestions.");
        } catch (e) {
          suggestHint.textContent = tx("Couldn't get suggestions ({1}).", { 1: e.message });
        }
        suggestBtn.disabled = false;
      },
    });
    const includeScan = el("input", { type: "checkbox", id: "edit-autotag-include" });
    includeScan.checked = !recipe.autotag_ignored;

    // pre-line: the test result puts sending and reading on separate lines.
    const statusLine = el("div", { class: "field-hint", role: "status", style: "margin-top:0.5rem;white-space:pre-line;" });

    const saveBtn = el("button", {
      class: "btn-primary", type: "button", text: tx("Save changes"),
      onclick: async () => {
        statusLine.textContent = tx("Saving...");
        const tagNames = tagsInput.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
        const tagObjs = tagNames.map((name) => {
          const sug = suggestedCats.get(name.toLowerCase());
          if (sug) return { name: sug.name, category: sug.category, subgroup: sug.subgroup || null };
          const known = findTagByLabel(name);
          return known
            ? { name: known.name, category: known.category, subgroup: known.subgroup }
            : { name, category: "custom", subgroup: null };
        });

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
          if (!res.ok) throw new Error(tx("Could not save changes."));
          let updated = await res.json();
          if (includeScan.checked === !!recipe.autotag_ignored) {
            const r2 = await fetch(`${API}/recipes/${recipe.id}/autotag-ignore`, {
              method: "PATCH", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ ignored: !includeScan.checked }),
            });
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

    const fields = el("div", {}, [
      el("div", { class: "field" }, [el("label", { for: "edit-title", text: tx("Title") }), titleInput]),
      el("div", { class: "field" }, [
        el("label", { for: "edit-ingredients", text: tx("Ingredients (one per line)") }),
        ingredientsArea,
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "edit-steps", text: tx("Steps (one per line)") }),
        stepsArea,
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
      el("h1", { id: "recipe-detail-heading", class: "recipe-title", text: tx("Edit recipe") }),
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
      class: "btn-secondary", type: "button", text: tx("Save time"),
      onclick: async () => {
        const res = await fetch(`${API}/recipes/${recipe.id}/rating`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ actual_cook_time: duration.getValue() || "" }),
        }).catch(() => null);
        if (res && res.ok) { announce(tx("Cook time updated.")); await loadTimeBuckets(); return; }
        // A refused time used to fail silently. The server's message (a
        // 422's validation list, translated server-side) says what's wrong.
        const body = res ? await res.json().catch(() => ({})) : {};
        announceError(apiErrorText(body, res ? res.status : 0));
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
        recipe.servings ? el("span", { text: tx("Servings: {1}", { 1: recipe.servings }) }) : null,
        recipe.prep_time ? el("span", { text: tx("Prep: {1}", { 1: recipe.prep_time }) }) : null,
        recipe.cook_time ? el("span", { text: tx("Cook: {1}", { 1: recipe.cook_time }) }) : null,
        recipe.total_time ? el("span", { text: tx("Total: {1}", { 1: recipe.total_time }) }) : null,
      ]),
      cardQuickControls(recipe, true),
      wakeLockToggle(),
      el("div", { class: "field" }, [
        el("label", { text: tx("Real-world cook time") }),
        el("div", { style: "display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;" }, [duration.element, timeSaveBtn]),
      ]),
      notesSection(recipe),
      el("h2", { text: tx("Ingredients") }),
      el("ul", { class: "ingredients-list" }, recipe.ingredients.map((i) =>
        el("li", { text: ingredientText(i) })
      )),
      el("h2", { text: tx("Instructions") }),
      el("ol", { class: "steps-list" }, recipe.steps.map((s) => el("li", { text: s.text }))),
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
  function shareActionButtons(recipe, onDone) {
    return [
      el("button", { type: "button", text: tx("Print"), onclick: () => { onDone(); window.print(); } }),
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
        onclick: () => { onDone(); openEmailSheet(recipe); },
      }) : null,
      el("button", {
        type: "button", text: tx("Download HTML"),
        onclick: () => {
          onDone();
          downloadExport(`${API}/recipes/${recipe.id}/export.html`, safeFilename(recipe.title, "html"));
        },
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

  async function deleteRecipe(id, btn) {
    if (!confirm(tx("Delete this recipe? This cannot be undone."))) return;

    // Guard against a double-fire, but deliberately do NOT touch the modal's
    // close button or the Escape handler: the modal now stays open until the
    // request resolves, so those are the only way out if this hangs.
    if (btn) { btn.disabled = true; btn.textContent = tx("Deleting…"); }
    const restore = () => { if (btn) { btn.disabled = false; btn.textContent = tx("Delete recipe"); } };

    const result = await fetchWithTimeout(`${API}/recipes/${id}`, { method: "DELETE" });

    if (result.ok) {
      closeModal(detailOverlay);
      announce(tx("Recipe deleted."));
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
      el("button", { type: "button", text: tx("\ud83d\udd17 URL"), onclick: showUrlForm }),
      el("button", { type: "button", text: tx("\ud83d\udcc4 PDF"), onclick: showPdfForm }),
      el("button", { type: "button", text: tx("\ud83d\udcf7 Screenshot/Photo"), onclick: showImageForm }),
      el("button", { type: "button", text: tx("\u270d\ufe0f Manual/Handwritten"), onclick: showManualForm }),
    ]);
    addBody.appendChild(picker);

    // Only offered once email ingest is switched on; otherwise it would be
    // a button that can only say "not configured".
    fetch(`${API}/email-settings`).then((r) => (r.ok ? r.json() : null)).then((settings) => {
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

  function renderBatchResults(container, result) {
    container.innerHTML = "";
    const list = el("ul", { class: "batch-results-list" });
    for (const s of result.succeeded) list.appendChild(el("li", { class: "ok", text: `\u2713 ${s.title}` }));
    for (const f of result.failed) list.appendChild(el("li", { class: "fail", text: `\u2717 ${f.url || f.filename}: ${f.error}` }));
    container.appendChild(list);
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

  function showUrlForm() {
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
            const res = await fetch(`${API}/ingest/url`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ url: input.value.trim() }),
            });
            if (!res.ok) {
              const err = await res.json().catch(() => ({}));
              showPdfFallback(typeof err.detail === "string" ? err.detail : tx("Extraction failed."), null);
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
            const res = await fetch(`${API}/ingest/url/batch`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ urls }),
            });
            const result = await res.json();
            if (!res.ok) { resultsEl.textContent = result.detail || tx("Batch import failed (error {1}).", { 1: res.status }); return; }
            renderBatchResults(resultsEl, result);
            announce(tx("{1} added, {2} failed.", { 1: result.succeeded.length, 2: result.failed.length }));
            await loadTags(); await loadTimeBuckets(); loadRecipes();
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
            if (!res.ok) throw new Error(tx("Could not process PDF."));
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
            const result = await res.json();
            if (!res.ok) { resultsEl.textContent = result.detail || tx("Batch import failed (error {1}).", { 1: res.status }); return; }
            renderBatchResults(resultsEl, result);
            announce(tx("{1} added, {2} failed.", { 1: result.succeeded.length, 2: result.failed.length }));
            await loadTags(); await loadTimeBuckets(); loadRecipes();
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

  function showImageForm() {
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
            if (!res.ok) throw new Error(tx("Could not process image."));
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
            if (!res.ok) { const err = await res.json().catch(() => ({})); throw new Error(err.detail || tx("Could not process images.")); }
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
          el("label", { for: "image-combine-input", text: tx("Screenshots (select all, in reading order)") }),
          input,
          el("div", { class: "field-hint", text: tx("For a recipe that spans multiple screenshots \u2014 each is read with OCR and combined into one recipe. Most file pickers preserve the order you select files in; the first image becomes the showcase photo (changeable afterward).") }),
        ]),
        el("button", { class: "btn-primary", type: "submit", text: tx("Process & combine") }),
      ]));
    }

    addBody.appendChild(modeSwitchControl(renderSingle, renderCombine, [tx("Single"), tx("Combine multiple")]));
    addBody.appendChild(formArea);
    renderSingle();
  }

  function setBusy(isBusy, msg) {
    if (isBusy) announce(msg || tx("Working..."));
    $$(".modal button, .modal input").forEach((n) => { n.disabled = isBusy; });
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

    const titleInput = el("input", { type: "text", id: "review-title", value: draft.title || "" });
    const ingredientsArea = el("textarea", { id: "review-ingredients" });
    ingredientsArea.value = (draft.ingredients || [])
      .map((i) => ingredientText(i))
      .join("\n");
    const stepsArea = el("textarea", { id: "review-steps" });
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
        imagePreviewWrap,
        el("div", { class: "field" }, [
          el("label", { for: "review-ingredients", text: tx("Ingredients (one per line)") }),
          ingredientsArea,
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "review-steps", text: tx("Steps (one per line)") }),
          stepsArea,
        ]),
        el("div", { class: "field" }, [
          el("label", { for: "review-tags", text: tx("Tags (comma or semicolon separated)") }),
          tagsInput,
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

    const saveBtn = el("button", {
      class: "btn-primary", type: "button", text: tx("Save recipe"),
      style: "margin-top:1rem;",
      onclick: async () => {
        setBusy(true, tx("Saving recipe..."));
        try {
          const tagNames = tagsInput.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
          const tagObjs = tagNames.map((name) => {
            const known = findTagByLabel(name);
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
            title: titleInput.value.trim() || tx("Untitled Recipe"),
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
          if (!res.ok) throw new Error(tx("Could not save recipe."));

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
          announce(tx("Recipe saved."));
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
    addBody.appendChild(el("h3", { text: tx("Manual / handwritten entry") }));

    const titleInput = el("input", { type: "text", id: "manual-title", required: "" });
    const ingredientsArea = el("textarea", { id: "manual-ingredients", placeholder: tx("One ingredient per line") });
    const stepsArea = el("textarea", { id: "manual-steps", placeholder: tx("One step per line") });
    const tagsInput = el("input", { type: "text", id: "manual-tags", placeholder: tx("e.g. dinner, stovetop, beef") });
    const imageInput = el("input", { type: "file", id: "manual-image", accept: "image/*" });
    const duration = makeDurationInput("manual-time");

    const form = el("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        setBusy(true, tx("Saving recipe..."));
        let storedFile = null;
        try {
          if (imageInput.files.length) {
            const fd = new FormData(); fd.append("file", imageInput.files[0]);
            const upRes = await fetch(`${API}/upload-image`, { method: "POST", body: fd });
            if (!upRes.ok) {
              // Used to save the recipe without the photo and say nothing.
              const err = await upRes.json().catch(() => ({}));
              const why = typeof err.detail === "string" ? err.detail : `error ${upRes.status}`;
              announceError(tx("The photo couldn't be uploaded ({1}). Nothing was saved; remove the photo or try another.", { 1: why }));
              return;
            }
            storedFile = (await upRes.json()).stored_file;
          }

          const tagNames = tagsInput.value.split(/[,;]/).map((s) => s.trim()).filter(Boolean);
          const tagObjs = tagNames.map((name) => {
            const known = findTagByLabel(name);
            return known
              ? { name: known.name, category: known.category, subgroup: known.subgroup }
              : { name, category: "custom", subgroup: null };
          });

          const payload = {
            title: titleInput.value.trim() || tx("Untitled Recipe"),
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
          if (!res.ok) throw new Error(tx("Could not save recipe."));
          closeModal(addOverlay);
          announce(tx("Recipe saved."));
          await loadTags(); await loadTimeBuckets();
          loadRecipes();
        } catch (err) {
          if (storedFile) fetch(`${API}/ingest/draft/${encodeURIComponent(storedFile)}`, { method: "DELETE" }).catch(() => {});
          alert(err.message);
        } finally { setBusy(false); }
      },
    }, [
      el("div", { class: "field" }, [el("label", { for: "manual-title", text: tx("Title") }), titleInput]),
      el("div", { class: "field" }, [el("label", { for: "manual-ingredients", text: tx("Ingredients (one per line)") }), ingredientsArea]),
      el("div", { class: "field" }, [el("label", { for: "manual-steps", text: tx("Steps (one per line)") }), stepsArea]),
      el("div", { class: "field" }, [el("label", { for: "manual-tags", text: tx("Tags (comma or semicolon separated)") }), tagsInput]),
      el("div", { class: "field" }, [
        el("label", { text: tx("Real-world cook time (optional)") }),
        duration.element,
      ]),
      el("div", { class: "field" }, [
        el("label", { for: "manual-image", text: tx("Attach a photo of the card (optional)") }),
        imageInput,
      ]),
      el("button", { class: "btn-primary", type: "submit", text: tx("Save recipe") }),
    ]);
    addBody.appendChild(form);
  }

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
    await loadTimeBuckets();
    await loadRecipes();
    await loadMailState();
    refreshHttpsSummary();
  })();

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/service-worker.js").catch(() => {});
    });
  }
})();
