"""What the pages do, in headless Chromium (see conftest.py). Each test
names the patch whose bug it would have caught."""
import io
import re
import uuid
import zipfile

from .conftest import add_recipe, wait_until


def _open_settings(p, url):
    p.page.goto(url)
    p.page.wait_for_selector(".recipe-card, .empty-state, #recipe-list", timeout=10000)
    p.page.click("#settings-toggle")
    p.page.wait_for_selector("#settings-overlay:not([hidden])")


def _password_if_asked(p, password="browser-test-password"):
    pg = p.page
    try:
        pg.wait_for_selector("#admin-overlay:not([hidden])", timeout=3000)
    except Exception:
        return
    if pg.locator("#admin-new").count():
        pg.fill("#admin-new", password)
        pg.fill("#admin-confirm", password)
    else:
        pg.fill("#admin-current", password)
    pg.click("#admin-body button[type=submit]")
    pg.wait_for_selector("#admin-overlay", state="hidden")


def test_list_and_a_recipe_without_a_photo(server, api, page):
    """0082: "null" showed above a recipe without a photo (picker); the
    main recipe view must not either. 0064: no sideways scroll on a phone."""
    r = add_recipe(api, f"Browser soup {uuid.uuid4().hex[:6]}")
    p = page()
    p.page.goto(server["url"])
    p.page.wait_for_selector(f"text={r['title']}")
    assert p.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    p.page.click(f"text={r['title']}")
    p.page.wait_for_selector("#recipe-detail-heading")
    body = p.page.inner_text("#recipe-detail-body")
    assert "null" not in body and "undefined" not in body
    # Scaling (0087): 2x doubles the leading amounts.
    p.page.click(".scale-button[aria-label='Double the quantities']")
    lines = p.page.inner_text(".recipe-ingredients .ingredients-list, .recipe-ingredients ul")
    assert re.search(r"^2 cups flour", lines, re.M) and re.search(r"^4 eggs", lines, re.M)


def test_largest_text_has_no_sideways_scroll(server, api, page):
    """0064: at the largest text size the list scrolled sideways."""
    add_recipe(api, "A rather long recipe title that wraps on a narrow phone screen")
    p = page(width=320)
    p.page.goto(server["url"])
    p.page.wait_for_selector(".recipe-card")
    p.page.evaluate("localStorage.setItem('recipe-app-text-size', '5')")
    p.page.reload()
    p.page.wait_for_selector(".recipe-card")
    assert p.page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")


def test_picker_reader_without_a_photo(server, api, page):
    """0082: the What to eat? reader showed "null" above such a recipe."""
    r = add_recipe(api, f"Picker plain {uuid.uuid4().hex[:6]}", tags=[{"name": "Dinner", "category": "meal_type"}])
    assert api.put("/api/picker/settings", json={"enabled": True}).status_code == 200
    try:
        p = page()
        p.page.goto(f"{server['picker_url']}/#recipe-{r['id']}")
        p.page.wait_for_selector("#reader-overlay:not([hidden]) #reader-body h2, #reader-overlay:not([hidden]) #reader-title")
        text = p.page.inner_text("#reader")
        assert r["title"] in text and "null" not in text and "undefined" not in text
    finally:
        api.put("/api/picker/settings", json={"enabled": False})


def test_settings_groups_and_hidden_buttons(server, page):
    """0098: "Lock now" / "Change password" showed before any settings
    password existed (a class's display beat the hidden attribute)."""
    p = page()
    _open_settings(p, server["url"])
    titles = p.page.locator(".settings-group-title").all_inner_texts()
    assert titles == ["Display", "Recipes", "Server"]
    if "Not set yet" in p.page.inner_text("#admin-lock-status"):
        assert not p.page.is_visible("#admin-change-toggle") and not p.page.is_visible("#admin-lock-now")
    hidden_but_shown = p.page.evaluate(
        "[...document.querySelectorAll('[hidden]')].filter(e => getComputedStyle(e).display !== 'none').map(e => e.id || e.className)")
    assert hidden_but_shown == []


def test_french(server, page):
    p = page(locale="fr")
    _open_settings(p, server["url"])
    assert p.page.locator(".settings-group-title").all_inner_texts() == ["Affichage", "Recettes", "Serveur"]
    assert p.page.inner_text("#settings-heading") == "Paramètres"


def test_duplicate_warning_checks_a_title_changed_during_the_check(server, api, page):
    """0094: a title edited while Save's duplicate check ran was saved
    without being checked."""
    existing = add_recipe(api, f"Taken title {uuid.uuid4().hex[:6]}")
    p = page()
    pg = p.page
    pg.goto(server["url"])
    pg.wait_for_selector(".recipe-card")
    pg.click("#add-recipe-fab")
    pg.locator("#add-recipe-body button", has_text="Manual").click()
    pg.fill("#manual-title", f"Unique {uuid.uuid4().hex[:6]}")
    pg.fill("#manual-ingredients", "1 egg")
    pg.fill("#manual-steps", "Boil.")
    pg.locator("#manual-steps").blur()
    pg.wait_for_timeout(500)

    # Each duplicate lookup answers 1.2 s late (done in the page: a route
    # handler that sleeps would block this test too).
    pg.evaluate("""() => { const real = window.fetch; window.fetch = (u, o) => String(u).includes('/api/recipes/duplicates')
        ? new Promise((ok) => setTimeout(() => ok(real(u, o)), 1200)) : real(u, o); }""")
    before = len(api.get("/api/recipes").json())
    pg.locator("#add-recipe-body form button[type=submit]").click()
    pg.wait_for_timeout(300)
    pg.evaluate(f"document.querySelector('#manual-title').value = {existing['title']!r}")
    pg.wait_for_selector(".dup-warning:not([hidden])", timeout=10000)
    assert len(api.get("/api/recipes").json()) == before                       # not saved unchecked
    pg.locator("#add-recipe-body form button[type=submit]").click()            # warned once: a second Save adds it
    pg.wait_for_timeout(3000)
    assert len(api.get("/api/recipes").json()) == before + 1


def test_shared_link_fills_in_but_doesnt_import(server, page):
    """0088: /?share_url= used to submit Add from URL by itself."""
    p = page()
    imports = []
    p.page.on("request", lambda r: imports.append(r.url) if "/api/ingest/url" in r.url else None)
    p.page.goto(server["url"] + "/?share_url=" + "https%3A%2F%2Fexample.com%2Frecipe")
    p.page.wait_for_selector("#url-input")
    assert p.page.input_value("#url-input") == "https://example.com/recipe"
    p.page.wait_for_timeout(800)
    assert imports == []


def test_review_banner_and_looks_right(server, api, page):
    """0092: a recipe saved without review shows the note; Looks right clears it."""
    r = add_recipe(api, f"Unreviewed {uuid.uuid4().hex[:6]}")
    import sqlite3, os
    con = sqlite3.connect(os.path.join(server["data"], "recipes.db"))
    con.execute("UPDATE recipes SET needs_review = 1 WHERE id = ?", (r["id"],))
    con.commit()
    con.close()
    p = page()
    p.page.goto(server["url"] + f"/#recipe-{r['id']}")
    p.page.wait_for_selector(".review-banner")
    p.page.locator(".review-banner button", has_text="Looks right").click()
    p.page.wait_for_selector(".review-banner", state="detached")
    assert api.get(f"/api/recipes/{r['id']}").json()["needs_review"] is False


def test_duplicate_opens_the_copy_in_edit(server, api, page):
    """0095."""
    r = add_recipe(api, f"Original {uuid.uuid4().hex[:6]}")
    p = page()
    p.page.goto(server["url"] + f"/#recipe-{r['id']}")
    p.page.wait_for_selector(".recipe-record-actions")
    p.page.locator(".recipe-record-actions button", has_text="Duplicate").click()
    p.page.wait_for_selector("#edit-title")
    assert p.page.input_value("#edit-title") == f"{r['title']} (copy)"


def test_restore_asks_for_the_password_at_the_check(server, api, page):
    """0099: checking a full backup needs the settings password; an import file doesn't."""
    add_recipe(api, f"Before restore {uuid.uuid4().hex[:6]}")
    backup = api.get("/api/backup/database.zip").content
    p = page()
    pg = p.page
    _open_settings(p, server["url"])
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("recipes/a.json", '{"@type": "Recipe", "name": "Imported in a browser test", '
                                       '"recipeIngredient": ["1 egg"], "recipeInstructions": ["Boil."]}')
    pg.set_input_files("#import-file", files=[{"name": "r.zip", "mimeType": "application/zip", "buffer": out.getvalue()}])
    pg.click("#import-btn")
    pg.wait_for_selector("#import-results li")
    assert pg.is_hidden("#admin-overlay")
    pg.set_input_files("#import-file", files=[{"name": "b.zip", "mimeType": "application/zip", "buffer": backup}])
    pg.click("#import-btn")
    _password_if_asked(p)
    pg.wait_for_selector("#restore-confirm:not([hidden])")
    assert "full backup" in pg.inner_text("#restore-summary")
    pg.click("#restore-cancel")


def test_automatic_backups_switch(server, page):
    """0101: switching on asks for the password, Back up now makes one."""
    p = page()
    pg = p.page
    _open_settings(p, server["url"])
    pg.locator("#auto-backup-box").scroll_into_view_if_needed()
    if not pg.is_checked("#auto-backup-enabled"):
        pg.click("label[for=auto-backup-enabled]")
        _password_if_asked(p)
    wait_until(pg, "document.querySelector('#auto-backup-enabled').checked")
    pg.click("#auto-backup-run")
    wait_until(pg, "document.querySelector('#auto-backup-status').textContent.includes('Last:')", timeout=20)
