(() => {
  "use strict";

  const API = "/api";

  // -------------------------------------------------------------------
  // Utilities
  // -------------------------------------------------------------------
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  function announce(msg) {
    $("#status-region").textContent = msg;
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


  $("#settings-toggle").addEventListener("click", () => openModal(settingsOverlay));
  $("#settings-close").addEventListener("click", () => closeModal(settingsOverlay));
  settingsOverlay.addEventListener("click", (e) => { if (e.target === settingsOverlay) closeModal(settingsOverlay); });

  const shareSheetOverlay = $("#share-sheet-overlay");
  $("#share-sheet-close").addEventListener("click", () => closeModal(shareSheetOverlay));
  shareSheetOverlay.addEventListener("click", (e) => { if (e.target === shareSheetOverlay) closeModal(shareSheetOverlay); });

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
      settings = await res.json();
    } catch {
      emailPanel.innerHTML = "";
      emailPanel.appendChild(el("div", { class: "field-hint", text: "Could not load email settings." }));
      return;
    }

    emailPanel.innerHTML = "";

    if (!settings.encryption_configured) {
      emailPanel.appendChild(el("div", {
        class: "badge badge-confidence-low",
        style: "display:block;margin-bottom:0.6rem;padding:0.5rem;",
        text: "RECIPE_APP_ENCRYPTION_KEY isn't set on the server. Credentials can't be stored securely until it is \u2014 see the README.",
      }));
    }

    const enabledInput = el("input", { type: "checkbox", id: "email-enabled" });
    enabledInput.checked = !!settings.enabled;

    const imapHost = el("input", { type: "text", id: "email-imap-host", value: settings.imap_host || "" });
    const imapPort = el("input", { type: "number", id: "email-imap-port", value: String(settings.imap_port ?? 993) });
    const smtpHost = el("input", { type: "text", id: "email-smtp-host", value: settings.smtp_host || "" });
    const smtpPort = el("input", { type: "number", id: "email-smtp-port", value: String(settings.smtp_port ?? 587) });
    const username = el("input", { type: "text", id: "email-username", value: settings.username || "" });
    const password = el("input", {
      type: "password", id: "email-password",
      placeholder: settings.password_set ? "(saved \u2014 leave blank to keep)" : "",
    });
    const notifyEmail = el("input", { type: "email", id: "email-notify", value: settings.notify_email || "" });
    const keyword = el("input", { type: "text", id: "email-keyword", value: settings.subject_keyword || "[RECIPE]" });
    const scanHour = el("input", { type: "number", id: "email-scan-hour", min: "0", max: "23", value: String(settings.daily_scan_hour ?? 3) });
    const cooldown = el("input", { type: "number", id: "email-cooldown", min: "0", value: String(settings.cooldown_minutes ?? 30) });

    const statusLine = el("div", { class: "field-hint", style: "margin-top:0.5rem;" });

    emailPanel.appendChild(el("div", { class: "field" }, [
      el("label", { for: "email-enabled", style: "display:flex;align-items:center;gap:0.5rem;" }, [
        enabledInput,
        el("span", { text: "Enable daily inbox scan" }),
      ]),
    ]));

    emailPanel.appendChild(emailField("IMAP host (reading)", imapHost, "e.g. imap.gmail.com"));
    emailPanel.appendChild(emailField("IMAP port", imapPort));
    emailPanel.appendChild(emailField("SMTP host (notifications)", smtpHost, "e.g. smtp.gmail.com"));
    emailPanel.appendChild(emailField("SMTP port", smtpPort));
    emailPanel.appendChild(emailField("Username", username, "Used for both IMAP and SMTP."));
    emailPanel.appendChild(emailField("Password", password,
      "Stored encrypted. Use an app-specific password, not your main account password."));
    emailPanel.appendChild(emailField("Send notifications to", notifyEmail));
    emailPanel.appendChild(emailField("Subject keyword", keyword,
      "Only emails whose subject contains this are considered. Everything else is ignored."));
    emailPanel.appendChild(emailField("Daily scan hour (0-23)", scanHour, "Container-local time."));
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
          imap_use_ssl: true,
          smtp_host: smtpHost.value.trim() || null,
          smtp_port: parseInt(smtpPort.value, 10) || 587,
          smtp_use_tls: true,
          username: username.value.trim() || null,
          notify_email: notifyEmail.value.trim() || null,
          subject_keyword: keyword.value.trim() || "[RECIPE]",
          daily_scan_hour: parseInt(scanHour.value, 10) || 0,
          cooldown_minutes: parseInt(cooldown.value, 10) || 0,
        };
        if (password.value) payload.password = password.value;
        const res = await fetch(`${API}/email-settings`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (res.ok) {
          statusLine.textContent = "Saved.";
          announce("Email settings saved.");
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
      onclick: async () => {
        statusLine.textContent = "Scanning inbox...";
        try {
          const res = await fetch(`${API}/email-settings/scan`, { method: "POST" });
          const body = await res.json();
          if (body.scanned === 0 && body.messages.length) {
            statusLine.textContent = body.messages[0];
          } else {
            statusLine.textContent = `Scanned ${body.scanned}: ${body.succeeded} ingested, ${body.failed} failed.`;
            if (body.succeeded) { await loadTags(); await loadTimeBuckets(); loadRecipes(); }
          }
        } catch {
          statusLine.textContent = "Scan failed.";
        }
      },
    });

    const buttons = [saveBtn, testBtn, scanBtn];
    if (settings.password_set) {
      buttons.push(el("button", {
        class: "btn-secondary", type: "button", text: "Clear password",
        onclick: async () => {
          if (!confirm("Remove the stored email password? This also disables email ingest.")) return;
          await fetch(`${API}/email-settings/password`, { method: "DELETE" });
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
  sidebarToggle.addEventListener("click", () => {
    const collapsed = sidebar.getAttribute("data-collapsed") === "true";
    sidebar.setAttribute("data-collapsed", String(!collapsed));
    sidebarToggle.setAttribute("aria-expanded", String(collapsed));
  });
  if (window.innerWidth <= 720) {
    sidebar.setAttribute("data-collapsed", "true");
    sidebarToggle.setAttribute("aria-expanded", "false");
  }

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
  };
  let uiTimeLevel1 = null;   // UI-only: which coarse hour bucket is expanded in the time filter

  // -------------------------------------------------------------------
  // Tag sidebar
  // -------------------------------------------------------------------
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

    for (const [catKey, catLabel] of categories) {
      const tagsInCat = state.allTags.filter((t) => t.category === catKey);
      if (!tagsInCat.length) continue;

      const group = el("div", { class: "tag-tree-group" });
      group.appendChild(el("h2", { text: catLabel }));

      const mainTags = tagsInCat.filter((t) => !t.subgroup);
      for (const t of mainTags) group.appendChild(makeTagButton(t));

      const subgroups = [...new Set(tagsInCat.filter((t) => t.subgroup).map((t) => t.subgroup))];
      for (const sg of subgroups) {
        group.appendChild(el("div", { class: "subgroup-label", text: sg === "cocktail" ? "Cocktail Prep" : sg }));
        for (const t of tagsInCat.filter((t2) => t2.subgroup === sg)) group.appendChild(makeTagButton(t));
      }

      container.appendChild(group);
    }
  }

  function toggleTagFilter(tagName) {
    if (state.filterTags.has(tagName)) state.filterTags.delete(tagName);
    else state.filterTags.add(tagName);
    renderSidebar();
    if (!$("#filter-panel").hidden) renderFilterPanel();
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
              loadRecipes();
            },
          })
        )));
      }
    }
    panel.appendChild(timeSection);

    if (state.filterTags.size || state.maxMinutes != null) {
      panel.appendChild(el("div", { class: "active-filters-summary" }, [
        el("span", { text: `${state.filterTags.size} tag filter${state.filterTags.size === 1 ? "" : "s"}${state.maxMinutes != null ? " + time filter" : ""} active` }),
        el("button", {
          type: "button", text: "Clear all",
          onclick: () => {
            state.filterTags.clear();
            state.maxMinutes = null;
            uiTimeLevel1 = null;
            renderSidebar();
            renderFilterPanel();
            loadRecipes();
          },
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
    $("#batch-bar").hidden = !state.selectMode || state.selectedIds.size === 0;
  }

  function toggleSelect(id) {
    if (state.selectedIds.has(id)) state.selectedIds.delete(id);
    else state.selectedIds.add(id);
    updateBatchBar();
    renderRecipeList(currentRecipes);
  }

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
    await fetch(`${API}/recipes/batch-delete`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    announce(`${ids.length} recipe(s) deleted.`);
    state.selectedIds.clear();
    state.selectMode = false;
    $("#select-mode-toggle").setAttribute("aria-pressed", "false");
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
    const res = await fetch(`${API}/recipes?${params.toString()}`);
    currentRecipes = await res.json();
    renderRecipeList(currentRecipes);
    announce(`${currentRecipes.length} recipe${currentRecipes.length === 1 ? "" : "s"} found`);
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

  function ratingPopoverControl(recipe, field, icon, options, shortLabel) {
    let current = recipe[field];
    const popover = el("div", { class: "rating-popover", hidden: "" });
    const btn = el("button", {
      type: "button", "data-has-value": String(current != null),
      "aria-haspopup": "true", "aria-expanded": "false",
      text: current != null ? `${icon} ${shortLabel(current)}` : icon,
      "aria-label": `Set ${field.replace("_", " ")}`,
    });
    btn.addEventListener("click", () => {
      const willOpen = popover.hidden;
      closeAllRatingPopovers();
      popover.hidden = !willOpen;
      btn.setAttribute("aria-expanded", String(willOpen));
    });

    for (const opt of options) {
      popover.appendChild(el("button", {
        type: "button",
        "aria-pressed": String(current === opt),
        text: field === "tastiness_rating" ? "\u2b50".repeat(opt) : (opt.charAt(0).toUpperCase() + opt.slice(1)),
        onclick: async () => {
          popover.hidden = true;
          btn.setAttribute("aria-expanded", "false");
          const ok = await patchRating(recipe.id, { [field]: opt });
          if (ok) {
            current = opt;
            recipe[field] = opt;
            btn.setAttribute("data-has-value", "true");
            btn.textContent = `${icon} ${shortLabel(opt)}`;
          } else {
            announce("Could not save rating.");
          }
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

    wrap.appendChild(ratingPopoverControl(recipe, "tastiness_rating", "\u2b50", [1, 2, 3, 4, 5], (v) => String(v)));
    wrap.appendChild(ratingPopoverControl(recipe, "cook_time_rating", "\u23f1", ["quick", "moderate", "long"], (v) => v.charAt(0).toUpperCase() + v.slice(1)));
    wrap.appendChild(ratingPopoverControl(recipe, "difficulty_rating", "\ud83c\udf9a", ["easy", "medium", "hard"], (v) => v.charAt(0).toUpperCase() + v.slice(1)));

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
      class: "recipe-card",
      onclick: (e) => {
        e.preventDefault();
        if (state.selectMode) { toggleSelect(recipe.id); return; }
        openRecipeDetail(recipe.id);
      },
    });
    card.appendChild(el("img", {
      class: "recipe-card-image",
      src: recipe.image_path ? `/uploads/${recipe.image_path}` : "",
      alt: "", loading: "lazy",
    }));

    const tab = el("div", { class: "card-tab" }, [sourceBadge(recipe), confidenceBadge(recipe), matchedViaBadge(recipe)]);
    card.appendChild(tab);

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
    card.appendChild(el("div", { class: "recipe-card-body" }, [
      el("h3", { class: "recipe-card-title recipe-title", text: recipe.title }),
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
    if (!res.ok) { announce("Could not load recipe."); return; }
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
          announce("Could not save notes.");
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
        if (!upRes.ok) { announce("Could not upload photo."); return; }
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
          announce("Could not save photo.");
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
              announce("Could not remove photo.");
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
      .map((i) => [i.quantity, i.unit, i.name || i.raw_line].filter(Boolean).join(" "))
      .join("\n");

    const stepsArea = el("textarea", { id: "edit-steps", style: "min-height:9rem;" });
    stepsArea.value = recipe.steps.map((s) => s.text).join("\n");

    const tagsInput = el("input", {
      type: "text", id: "edit-tags",
      value: recipe.tags.map((t) => t.name).join(", "),
    });

    const statusLine = el("div", { class: "field-hint", style: "margin-top:0.5rem;" });

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
        el("button", {
          class: "btn-secondary", type: "button", text: "\u270e Edit recipe",
          onclick: () => renderRecipeEditForm(recipe),
        }),
      ]),
      el("div", { class: "field" }, [
        el("label", { text: "Real-world cook time" }),
        el("div", { style: "display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;" }, [duration.element, timeSaveBtn]),
      ]),
      notesSection(recipe),
      el("h2", { text: "Ingredients" }),
      el("ul", { class: "ingredients-list" }, recipe.ingredients.map((i) =>
        el("li", { text: [i.quantity, i.unit, i.name || i.raw_line].filter(Boolean).join(" ") })
      )),
      el("h2", { text: "Instructions" }),
      el("ol", { class: "steps-list" }, recipe.steps.map((s) => el("li", { text: s.text }))),
      el("button", {
        class: "btn-secondary", type: "button", text: "Delete recipe",
        style: "margin-top:1.5rem;",
        onclick: () => deleteRecipe(recipe.id),
      }),
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
      announce("Could not prepare the download; opening it instead.");
      window.open(url, "_blank", "noopener");
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
            recipe.ingredients.map((i) => `- ${[i.quantity, i.unit, i.name || i.raw_line].filter(Boolean).join(" ")}`).join("\n") +
            `\n\nInstructions:\n` + recipe.steps.map((s, idx) => `${idx + 1}. ${s.text}`).join("\n");
          try { await navigator.clipboard.writeText(text); announce("Recipe copied as text."); }
          catch { announce("Could not copy to clipboard."); }
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

  async function deleteRecipe(id) {
    if (!confirm("Delete this recipe? This cannot be undone.")) return;
    await fetch(`${API}/recipes/${id}`, { method: "DELETE" });
    closeModal(detailOverlay);
    announce("Recipe deleted.");
    await loadTimeBuckets();
    loadRecipes();
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
          setBusy(true, `Fetching ${urls.length} recipe(s)...`);
          try {
            const res = await fetch(`${API}/ingest/url/batch`, {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ urls }),
            });
            const result = await res.json();
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
          setBusy(true, `Processing ${input.files.length} PDF(s)...`);
          try {
            const fd = new FormData();
            for (const f of input.files) fd.append("files", f);
            const res = await fetch(`${API}/ingest/pdf/batch`, { method: "POST", body: fd });
            const result = await res.json();
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
      .map((i) => [i.quantity, i.unit, i.name || i.raw_line].filter(Boolean).join(" "))
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
            if (upRes.ok) storedFile = (await upRes.json()).stored_file;
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
