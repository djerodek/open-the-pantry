"""Things kept in two places that have to stay the same (Qwen review)."""
import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[2] / "frontend" / "css" / "styles.css"


def _declarations(block: str) -> dict:
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


@pytest.mark.skipif(not CSS.exists(), reason="frontend not present")
def test_dark_theme_blocks_match():
    css = CSS.read_text(encoding="utf-8")
    media = re.search(r':root:not\(\s*\[\s*data-theme\s*=\s*"light"\s*\]\s*\)\s*\{(.*?)\}', css, re.S)
    forced = re.search(r'html\[\s*data-theme\s*=\s*"dark"\s*\]\s*\{(.*?)\}', css, re.S)
    assert media and forced, "dark-theme blocks not found in styles.css; update this test's selectors"
    media, forced = media.group(1), forced.group(1)
    a, b = _declarations(media), _declarations(forced)
    assert len(a) > 20 and a == b


def test_restore_instructions_give_the_same_commands():
    from app.backup import RESTORE_INSTRUCTIONS, RESTORE_INSTRUCTIONS_FR

    def commands(text):
        # Indented lines are the commands and compose snippets; the archive
        # name's date placeholder differs by language (YYYY vs AAAA).
        lines = [l.strip() for l in text.splitlines() if l.startswith("    ") and l.strip()]
        return [re.sub(r"backup-\S+\.zip", "backup-X.zip", l) for l in lines
                if not l.startswith(("recipes.db", "uploads/", "manifest.json"))]

    en, fr = commands(RESTORE_INSTRUCTIONS), commands(RESTORE_INSTRUCTIONS_FR)
    en = [l.replace("/path/to/new/folder", "PATH") for l in en]
    fr = [l.replace("/chemin/vers/nouveau/dossier", "PATH") for l in fr]
    assert en == fr and any("docker compose down" in l for l in en)


def test_ocr_skips_a_zero_size_render():
    from PIL import Image
    from app.ingestion import pdf_ingest
    assert pdf_ingest._ocr_image(Image.new("L", (0, 10))) == ("", None)


# ---------------------------------------------------------------------------
# Keep screen awake and optional HTTPS
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not (ROOT / "docker-compose.https.yml").exists(), reason="repo files not present")
def test_https_setup_is_off_by_default_and_names_no_real_domain():
    """The HTTPS override takes every site-specific value from .env and
    refuses to start without them; the repo carries only placeholders."""
    yml = (ROOT / "docker-compose.https.yml").read_text(encoding="utf-8")
    example = (ROOT / "https.env.example").read_text(encoding="utf-8")
    example += (ROOT / "traefik.env.example").read_text(encoding="utf-8")
    for var in ("PANTRY_DOMAIN", "ACME_EMAIL", "ACME_DNS_PROVIDER"):
        assert re.search(r"\$\{" + var + r":\?", yml), var
    # Any domain-looking name must be a documentation placeholder.
    names = set(re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|ca|io|dev|app|fr|uk|de)\b", yml + example))
    allowed = {"example.com", "pantry.example.com", "cpanel.example.com", "ns1.example.com", "go-acme.github.io"}
    assert names <= allowed, names - allowed
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and "traefik.env" in ignored and "letsencrypt/" in ignored
    # Plain `docker compose up -d` doesn't include it.
    assert "traefik" not in (ROOT / "docker-compose.yml").read_text(encoding="utf-8").lower()
    # Claude review round 4: the DNS token is Traefik's alone (the app gets
    # .env), and the example doesn't steer cPanel users into this route.
    assert re.search(r"open-the-pantry-https:.*?env_file:\s*\n\s*- traefik\.env", yml, re.S)
    https_example = (ROOT / "https.env.example").read_text(encoding="utf-8")
    assert "ACME_DNS_PROVIDER=cpanel" not in https_example and "TOKEN" not in https_example.replace("token", "")
    assert "FORWARDED_ALLOW_IPS" in yml and "FORWARDED_ALLOW_IPS: \"*\"" not in yml


def test_source_link_only_for_web_addresses():
    """The "View original" link is built from stored data, which a restored
    backup can fill with anything; only http(s) may become a link."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function sourceLink("):js.index("function confidenceBadge(")]
    assert 'url.protocol !== "http:" && url.protocol !== "https:"' in body
    assert 'rel: "noopener noreferrer"' in body and 'target: "_blank"' in body
    assert "sourceLink(recipe)" in js
    # On paper the link can't be followed, so print shows the address.
    css = CSS.read_text(encoding="utf-8")
    print_css = css[css.index("@media print"):]
    assert ".badge-link::after { content: attr(href)" in print_css


def test_search_clear_button_is_wired_up():
    """The X in the search box: present in the page, kept in step with the
    box (including when "Clear filters" empties it from code, which fires no
    input event), and the browser's own clear control hidden so there is one."""
    root = CSS.parents[1]
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "js" / "app.js").read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'id="search-clear"' in html and 'aria-label="Clear search"' in html
    assert '$("#search-input").addEventListener("input", syncSearchClear)' in js
    clear_all = js[js.index("function clearAllFilters("):js.index("function syncClearFiltersButton(")]
    assert "syncSearchClear()" in clear_all
    assert "::-webkit-search-cancel-button" in css
    # Text the browser restores (back button, Firefox reload) fires no input event.
    assert 'window.addEventListener("pageshow", syncSearchClear)' in js


def test_card_controls_do_not_follow_the_card_link():
    """The favourite and rating buttons sit inside the card's <a href>. The
    row stops the click from reaching the card, so it must also cancel the
    link, or a tap on the heart navigates (on iOS: list redrawn at the top).
    Only taps on a control are held back: the empty part of the row opens
    the recipe like the rest of the card (it used to do nothing)."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function cardQuickControls("):js.index("function recipeCard(")]
    assert 'if (!e.target.closest("button, .rating-popover")) return;\n        e.stopPropagation(); e.preventDefault();' in body
    row = body[:body.index("return wrap;")]
    assert 'el("a"' not in row and 'el("input"' not in row, "a real link or field in the row would be cancelled too"


def test_card_share_sheet_skips_the_missing_email_button():
    """Without email sending, Email PDF is null in the share actions; the
    card's share sheet appended it anyway, threw, and lost the buttons after it."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function openShareSheet("):js.index("function openShareSheet(") + 600]
    assert ".filter(Boolean).forEach((b) => actions.appendChild(b))" in body


def test_recipe_dialog_opens_on_the_tap():
    """The recipe dialog opens before the recipe has downloaded, so a slow
    response doesn't read as a missed tap; only the latest tap is shown,
    and a dialog closed while loading stays closed."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("async function openRecipeDetail("):js.index("function wakeLockToggle(")]
    assert body.index("openModal(detailOverlay)") < body.index("await fetch(")
    assert "if (ticket !== detailTicket || detailOverlay.hidden) return;" in body


def test_largest_text_size_fits_a_phone():
    """Settings -> Text size goes up to 26px. At that size 15rem is 390px,
    wider than most phones: the card grid must not demand it, and the
    recipe header must be free to put Share below the title rather than
    under the close button."""
    css = CSS.read_text(encoding="utf-8")
    assert "repeat(auto-fill, minmax(min(15rem, 100%), 1fr))" in css
    assert ".recipe-detail-header { display: flex; flex-wrap: wrap;" in css


# ---------------------------------------------------------------------------
# What to eat? (frontend-picker/, app/picker.py)
# ---------------------------------------------------------------------------

PICKER_DIR = ROOT / "frontend-picker"


@pytest.mark.skipif(not (PICKER_DIR / "picker.css").exists(), reason="frontend not present")
def test_picker_tokens_match_the_main_app():
    """The picker copies the main app's design tokens (light, and dark from
    the system setting); a change to one copy must reach the other."""
    def blocks(css):
        root = re.search(r"(?m)^:root\s*\{(.*?)\n\}", css, re.S)
        dark = re.search(r'@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-theme="light"\]\)\s*\{(.*?)\}', css, re.S)
        assert root and dark, "token blocks not found; update this test's selectors"
        return _declarations(root.group(1)), _declarations(dark.group(1))

    main_root, main_dark = blocks(CSS.read_text(encoding="utf-8"))
    pick_root, pick_dark = blocks((PICKER_DIR / "picker.css").read_text(encoding="utf-8"))
    assert len(main_root) > 20 and pick_root == main_root
    assert len(main_dark) > 20 and pick_dark == main_dark


@pytest.mark.skipif(not (PICKER_DIR / "picker.js").exists(), reason="frontend not present")
def test_picker_page_accessibility_and_motion():
    html = (PICKER_DIR / "index.html").read_text(encoding="utf-8")
    js = (PICKER_DIR / "picker.js").read_text(encoding="utf-8")
    css = (PICKER_DIR / "picker.css").read_text(encoding="utf-8")
    # Chips are buttons with aria-pressed, each group a labelled role=group.
    assert 'class: "chip"' in js and '"aria-pressed": String(' in js
    assert 'role: "group", "aria-labelledby": labelId' in js
    # One polite live region, apart from the card the shuffle cycles through,
    # which hears only the final pick.
    assert 'id="pick-status" role="status" aria-live="polite"' in html
    assert html.count("aria-live") == 1
    assert '"aria-hidden": "true" }, [' in js[js.index("function shuffle("):js.index("function announce(")]
    # Reduced motion skips the shuffle and the landing; the pick is decided
    # before the shuffle runs.
    assert '"(prefers-reduced-motion: reduce)"' in js[js.index("const reduceMotion"):js.index("function el(")]
    shuffle = js[js.index("function shuffle("):js.index("function announce(")]
    assert "if (reduceMotion() || !titles.length)" in shuffle
    assert 'if (!reduceMotion()) card.classList.add("stage-card--landed")' in js
    assert re.search(r"@media \(prefers-reduced-motion: reduce\) \{[^}]*stage-card--landed[^}]*animation: none", css)
    pick = js[js.index("async function pick("):js.index("function clearAll(")]
    assert pick.index("await api(query(") < pick.index("await shuffle(")
    # Busy without `disabled`, so focus stays on the button pressed.
    busy = js[js.index("function setBusy("):js.index("function query(")]
    assert "aria-disabled" in busy and ".disabled" not in busy
    # Clear sends focus back to the first chip group.
    clear = js[js.index("function clearAll("):js.index('pickButton.addEventListener("click"')]
    assert 'querySelector("#choices .chip")' in clear and ".focus()" in clear
    # The big button stays a comfortable target at every text size.
    assert re.search(r"\.pick-button \{[^}]*min-height: 3\.4rem", css)
    # The stage's photo frame is the same size in every state (waiting,
    # rolling, the pick, no match), so nothing jumps when a pick lands.
    assert re.search(r"\.stage-photo \{[^}]*aspect-ratio: 3 / 2", css)
    assert js.count("photoArea(") >= 4
    # Remembered choices: both localStorage calls sit in try/catch (storage
    # can throw in private modes), and nothing else touches it.
    assert js.count("localStorage.") == 2
    for fn, nxt in (("function loadChoices(", "function saveChoices("), ("function saveChoices(", "function webAddress(")):
        body = js[js.index(fn):js.index(nxt)]
        assert "localStorage." in body and "try {" in body and "catch" in body


@pytest.mark.skipif(not (PICKER_DIR / "picker.js").exists(), reason="frontend not present")
def test_picker_links_only_to_web_addresses():
    js = (PICKER_DIR / "picker.js").read_text(encoding="utf-8")
    body = js[js.index("function webAddress("):js.index("async function api(")]
    assert 'u.protocol === "http:" || u.protocol === "https:"' in body
    assert "state.mainUrl = webAddress(options.main_url, state.mainUrl)" in js
    # The recipe opens in the picker; the main app is only for editing.
    assert "href: `${state.mainUrl}/#recipe-${recipe.id}`" in js[js.index("function renderReader("):]
    assert "onclick: () => openReader(recipe.id)" in js[js.index("function resultCard("):js.index("// Picking")]
    # "View original": http(s) only, new tab, no opener.
    src = js[js.index("function sourceLink("):js.index("function ratingsList(")]
    assert 'url.protocol !== "http:" && url.protocol !== "https:"' in src
    assert 'rel: "noopener noreferrer"' in src


@pytest.mark.skipif(not (PICKER_DIR / "picker.js").exists(), reason="frontend not present")
def test_picker_reader_is_read_only_and_closes_every_way():
    """The recipe sheet in the picker: no field, no form, no request that
    changes anything; Escape, the close button, the backdrop and phone Back
    close it; focus stays inside it while open and goes back after."""
    html = (PICKER_DIR / "index.html").read_text(encoding="utf-8")
    js = (PICKER_DIR / "picker.js").read_text(encoding="utf-8")
    reader = js[js.index("const readerOverlay"):js.index("// Start")]
    for tag in ('el("input"', 'el("textarea"', 'el("select"', 'el("form"', "method:"):
        assert tag not in reader, tag
    assert "method:" not in js            # the page sends nothing but GETs
    assert 'role="dialog" aria-modal="true" aria-labelledby="reader-title"' in html
    assert 'if (e.key === "Escape") { e.preventDefault(); closeReader(); return; }' in reader
    assert 'window.addEventListener("popstate"' in reader and "history.pushState(" in reader
    assert 'readerOverlay.addEventListener("click", (e) => { if (e.target === readerOverlay) closeReader(); });' in reader
    assert "if (e.key !== \"Tab\") return;" in reader
    assert "back.focus()" in reader
    # Only a recipe read through the picker's own route.
    assert "api(`/api/picker/recipes/${id}`)" in reader
    # An out-of-date answer (closed, or another recipe opened) is dropped.
    assert "if (ticket !== readerTicket || readerOverlay.hidden) return;" in reader


def test_main_app_opens_a_linked_recipe_and_clears_the_link():
    """/#recipe-<id> opens that recipe, at start-up and on hashchange; every
    way of closing it clears the hash, so a reload doesn't reopen it."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    assert 'window.addEventListener("hashchange", openRecipeFromHash)' in js
    init = js[js.index("(async function init() {"):]
    assert init.index("await loadTags()") < init.index("openRecipeFromHash()") < init.index("await loadRecipes()")
    close = js[js.index("function closeModal("):js.index("function trapFocus(")]
    assert 'if (overlay.id === "recipe-detail-overlay") clearRecipeHash();' in close
    clear = js[js.index("function clearRecipeHash("):js.index('window.addEventListener("hashchange"')]
    assert "history.replaceState(" in clear


def test_main_app_link_to_the_picker_follows_the_server():
    """The top-bar link exists only while /api/picker/status says enabled
    (switched on and running); otherwise it is removed, not greyed out, and a
    failed fetch removes it too. Settings checks from the browser whether the
    port answers, except from an https:// page, where that can't work."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    link = js[js.index("function renderPickerLink("):js.index("async function refreshPickerLink(")]
    assert "st && st.enabled ? pickerAddress(st) : null" in link and "link.remove()" in link
    assert '$("#settings-toggle").before(link)' in link and '"aria-label": tx("What to eat?")' in link
    refresh = js[js.index("async function refreshPickerLink("):js.index("function renderPickerSettings(")]
    assert "renderPickerLink(null)" in refresh
    assert "refreshPickerLink();" in js[js.index("(async function init() {"):]
    probe = js[js.index("async function probePicker("):js.index("async function refreshPicker(")]
    assert 'mode: "no-cors"' in probe and 'location.protocol === "https:" && url.startsWith("http:")' in probe
    html = (CSS.parents[1] / "index.html").read_text(encoding="utf-8")
    assert 'id="picker-link"' not in html          # added by script, only while on
    assert 'id="picker-enabled"' in html and 'id="picker-warning"' in html
    sw = (CSS.parents[1] / "service-worker.js").read_text(encoding="utf-8")
    assert r"/^\/api\/picker/" in sw
    # The service worker leaves other origins alone: it used to answer a
    # refused connection to the picker's port with its own "Offline" 503,
    # which the check above took for an answer.
    handler = sw[sw.index('self.addEventListener("fetch"'):]
    assert handler.index("if (new URL(event.request.url).origin !== self.location.origin) return;") \
        < handler.index("event.respondWith(")


@pytest.mark.skipif(not (ROOT / "docker-compose.yml").exists(), reason="repo files not present")
def test_picker_port_is_published_and_copied_into_the_image():
    for name in ("docker-compose.yml", "docker-compose.build.yml", "DOCKERHUB.md"):
        assert '"8091:8091"' in (ROOT / name).read_text(encoding="utf-8"), name
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY frontend-picker ./picker-static" in dockerfile and "EXPOSE 8091" in dockerfile
    assert "frontend-picker" not in (ROOT / ".dockerignore").read_text(encoding="utf-8")
    manifest = __import__("json").loads((PICKER_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "What to eat?" and manifest["start_url"] == "/"


def test_ingredient_and_step_boxes_are_tall():
    """Ingredients and Steps on Add manually, the review screen and Edit
    recipe: one class, twice the height of the other text boxes."""
    css = CSS.read_text(encoding="utf-8")
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    assert ".field textarea.recipe-lines { min-height: 12rem; }" in css
    for box in ("edit", "review", "manual"):
        for part in ("ingredients", "steps"):
            assert re.search(r'el\("textarea", \{ id: "%s-%s", class: "recipe-lines"' % (box, part), js), (box, part)
    assert "min-height:9rem" not in js


@pytest.mark.skipif(not (PICKER_DIR / "picker.js").exists(), reason="frontend not present")
def test_picker_never_hands_null_to_replace_children():
    """replaceChildren() and append() write a null argument out as the text
    "null": a recipe without a photo showed "null" above its title in the
    picker's recipe view (0082). A call that can get a "cond ? node : null"
    must filter it first."""
    js = (PICKER_DIR / "picker.js").read_text(encoding="utf-8")
    for m in re.finditer(r"\.(?:replaceChildren|append|prepend)\(", js):
        depth, i = 1, m.end()
        while depth:
            depth += {"(": 1, ")": -1}.get(js[i], 0)
            i += 1
        call = js[m.end():i - 1]
        if ": null" in call:
            assert call.rstrip().endswith(".filter(Boolean)"), call[:120]


@pytest.mark.skipif(not (PICKER_DIR / "picker.js").exists(), reason="frontend not present")
def test_picker_recipe_view_prints_the_recipe_only():
    """A Print button in the picker's recipe view (and nowhere else): the
    recipe alone, on white, notes and the edit link left off, the "View
    original" address written out; only while a recipe is open."""
    html = (PICKER_DIR / "index.html").read_text(encoding="utf-8")
    js = (PICKER_DIR / "picker.js").read_text(encoding="utf-8")
    css = (PICKER_DIR / "picker.css").read_text(encoding="utf-8")
    bar = html[html.index('class="reader-bar"'):html.index('id="reader-body"')]
    assert 'id="reader-print"' in bar and html.count("reader-print") == 2   # id and class, once
    assert 'printButton.addEventListener("click", () => window.print());' in js
    assert js.count("window.print(") == 1
    printing = css[css.index("@media print {"):]
    for hidden in (".picker-header", ".picker-main", ".reader-bar", ".reader-edit", ".reader-notes"):
        assert f"html.reader-open {hidden}" in printing, hidden
    assert ".reader-source::after { content: attr(href)" in printing
    assert "--color-bg: #FFFFFF;" in printing and "--color-ink: #000000;" in printing
