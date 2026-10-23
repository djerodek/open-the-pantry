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
    # The largest step (26px). This set 'recipe-app-text-size', a key the
    # app never reads, so it ran at the default size (fixed in 0112).
    p.page.evaluate("localStorage.setItem('recipe-app-font-size-idx', '6')")
    p.page.reload()
    p.page.wait_for_selector(".recipe-card")
    assert p.page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--base-font-size').trim()") == "26px"
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


def test_tag_the_selected_recipes(server, api, page):
    """0108: Select mode -> Tags adds one tag to every selected recipe and
    removes it again; the batch bar fits a 320 px phone."""
    suffix = uuid.uuid4().hex[:6]
    tagged = add_recipe(api, f"Batch tagged {suffix}", tags=[{"name": f"Weeknight{suffix}", "category": "custom"}])
    plain = add_recipe(api, f"Batch plain {suffix}")
    p = page(width=320)
    pg = p.page
    pg.goto(server["url"])
    pg.wait_for_selector(f"text={plain['title']}")
    pg.click("#select-mode-toggle")
    pg.click(f".recipe-card:has-text('{tagged['title']}')")
    pg.click(f".recipe-card:has-text('{plain['title']}')")
    pg.wait_for_selector("#batch-bar:not([hidden])")
    assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    outside = pg.evaluate("""[...document.querySelectorAll('#batch-bar > *')].filter((n) => {
      const r = n.getBoundingClientRect(); return r.left < 0 || r.right > innerWidth; }).map((n) => n.id)""")
    assert outside == []
    pg.click("#batch-tags-btn")
    pg.wait_for_selector("#batch-tags-overlay:not([hidden])")
    assert pg.inner_text("#batch-tags-count") == "2 recipes selected"
    pg.select_option("#batch-tags-select", label=f"Weeknight{suffix}")
    pg.click("#batch-tags-add")
    wait_until(pg, "document.querySelector('#batch-tags-status').textContent.includes('added to 1 recipe')")
    assert "1 recipe already had it." in pg.inner_text("#batch-tags-status")
    assert [t["name"] for t in api.get(f"/api/recipes/{plain['id']}").json()["tags"]] == [f"Weeknight{suffix}"]
    pg.click("#batch-tags-remove")
    wait_until(pg, "document.querySelector('#batch-tags-status').textContent.includes('removed from 2 recipes')")
    assert api.get(f"/api/recipes/{tagged['id']}").json()["tags"] == []


def test_delete_and_put_back(server, api, page):
    """0109: a deleted recipe waits in Recently deleted (side menu) and can
    be put back from there."""
    r = add_recipe(api, f"Bin browser {uuid.uuid4().hex[:6]}")
    p = page()
    pg = p.page
    pg.goto(server["url"])
    pg.click(f"text={r['title']}")
    pg.wait_for_selector("#recipe-detail-heading")
    pg.click("#recipe-detail-body button.btn-danger:has-text('Delete recipe')")
    pg.wait_for_selector("#recipe-detail-overlay", state="hidden")
    wait_until(pg, f"!document.body.innerText.includes({r['title']!r}) || !!document.querySelector('.review-filter[aria-controls=bin-overlay]')")
    assert api.get(f"/api/recipes/{r['id']}").status_code == 404
    pg.click("#sidebar-toggle")
    pg.click(".review-filter[aria-controls=bin-overlay]")
    pg.wait_for_selector("#bin-overlay:not([hidden])")
    item = pg.locator(".bin-item", has_text=r["title"])
    assert "gone in 30 days" in item.inner_text()
    item.locator("button", has_text="Put back").click()
    wait_until(pg, "document.querySelector('#bin-status').textContent.startsWith('Put back:')")
    assert api.get(f"/api/recipes/{r['id']}").status_code == 200
    pg.click("#bin-close")
    pg.wait_for_selector(f".recipe-card:has-text('{r['title']}')")


def test_merge_two_recipes(server, api, page):
    """0110: Merge… shows only with two selected; the parts chosen end up
    in one recipe and the other goes to Recently deleted."""
    sfx = uuid.uuid4().hex[:6]
    small = add_recipe(api, f"Merge small {sfx}", servings="2")
    big = add_recipe(api, f"Merge big {sfx}", ingredients=[{"raw_line": "1 cup rice"}, {"raw_line": "2 cups water"},
                                                          {"raw_line": "1 pinch salt"}])
    p = page()
    pg = p.page
    pg.goto(server["url"])
    pg.wait_for_selector(f"text={big['title']}")
    pg.click("#select-mode-toggle")
    pg.click(f".recipe-card:has-text('{small['title']}')")
    assert pg.is_hidden("#batch-merge-btn")
    pg.click(f".recipe-card:has-text('{big['title']}')")
    pg.click("#batch-merge-btn")
    pg.wait_for_selector("#merge-overlay:not([hidden])")
    # The bigger recipe's ingredients are the default; take the small one's title.
    assert pg.is_checked("input[name=merge-content][value='2']")
    pg.check("input[name=merge-title][value='1']")
    pg.check("input[name=merge-details][value='1']")
    pg.click("#merge-submit")
    pg.wait_for_selector("#merge-overlay", state="hidden")
    merged = api.get(f"/api/recipes/{big['id']}").json()
    assert merged["title"] == small["title"] and merged["servings"] == "2" and len(merged["ingredients"]) == 3
    assert api.get(f"/api/recipes/{small['id']}").status_code == 404
    assert pg.is_hidden("#batch-bar")


def test_library_check(server, api, page):
    """0111: Settings -> Library check lists recipes missing something and
    look-alikes; a title opens the recipe."""
    sfx = uuid.uuid4().hex[:6]
    bare = add_recipe(api, f"Check bare {sfx}", ingredients=[], steps=[])
    add_recipe(api, f"Check twin {sfx}")
    add_recipe(api, f"CHECK TWIN {sfx}")
    p = page()
    pg = p.page
    _open_settings(p, server["url"])
    pg.click("#library-check-btn")
    wait_until(pg, "document.querySelector('#library-check-result').textContent.includes('recipes checked')")
    result = pg.locator("#library-check-result")
    group = result.locator("details", has_text="Without ingredients")
    group.locator("summary").click()
    group.locator("button", has_text=bare["title"]).click()
    pg.wait_for_selector("#settings-overlay", state="hidden")
    wait_until(pg, f"document.querySelector('#recipe-detail-heading')?.textContent === {bare['title']!r}")
    pg.click("#recipe-detail-close")
    pg.click("#settings-toggle")
    dup = pg.locator("#library-check-result details", has_text="Look like the same recipe")
    dup.locator("summary").click()
    assert f"Check twin {sfx}" in dup.inner_text() and f"CHECK TWIN {sfx}" in dup.inner_text()


def test_share_as_image(server, api, page, tmp_path):
    """0113: Share -> Image (JPG) draws the recipe in the page; Download
    saves a 1080 px wide JPEG."""
    from PIL import Image
    r = add_recipe(api, f"Image soup {uuid.uuid4().hex[:6]}", servings="4",
                   steps=["Mix everything in a large bowl until smooth and glossy, about two minutes.", "Bake 20 minutes."])
    p = page()
    pg = p.page
    pg.goto(server["url"])
    pg.click(f"text={r['title']}")
    pg.wait_for_selector("#recipe-detail-heading")
    pg.click(".share-toggle")
    pg.click("#share-menu button:has-text('Image (JPG)')")
    pg.wait_for_selector("#image-sheet-preview img")
    wait_until(pg, "document.querySelector('#image-sheet-preview img').naturalWidth > 0")
    with pg.expect_download() as dl:
        pg.click("#image-sheet-download")
    path = tmp_path / dl.value.suggested_filename
    dl.value.save_as(path)
    assert path.name.endswith(".jpg")
    with Image.open(path) as im:
        assert im.format == "JPEG" and im.width == 1080 and 600 < im.height < 3000
    # From a card in the list too (its recipe has no ingredients loaded).
    pg.click("#image-sheet-close")
    pg.click("#recipe-detail-close")
    pg.click(f".recipe-card:has-text('{r['title']}') .card-share-btn")
    pg.click("#share-sheet-actions button:has-text('Image (JPG)')")
    wait_until(pg, "document.querySelector('#image-sheet-preview img')?.naturalWidth === 1080")


def test_share_as_image_with_notes(server, api, page, tmp_path):
    """0114: "Include my notes" (off by default) adds the notes to the
    image; it shows only for a recipe that has notes."""
    from PIL import Image
    plain = add_recipe(api, f"Image plain {uuid.uuid4().hex[:6]}")
    r = add_recipe(api, f"Image notes {uuid.uuid4().hex[:6]}")
    api.patch(f"/api/recipes/{r['id']}/notes", json={"notes": "Double the garlic.\nFreezes well for a month."})
    p = page()
    pg = p.page
    pg.goto(server["url"])

    def open_image(title):
        pg.click(f".recipe-card:has-text('{title}') .card-share-btn")
        pg.click("#share-sheet-actions button:has-text('Image (JPG)')")
        wait_until(pg, "document.querySelector('#image-sheet-preview img')?.naturalWidth > 0")
        return pg.evaluate("document.querySelector('#image-sheet-preview img').naturalHeight")

    open_image(plain["title"])
    assert pg.is_hidden(".image-sheet-notes")
    pg.click("#image-sheet-close")
    without = open_image(r["title"])
    assert pg.is_visible(".image-sheet-notes") and not pg.is_checked("#image-sheet-notes")
    pg.check("#image-sheet-notes")
    wait_until(pg, f"document.querySelector('#image-sheet-preview img')?.naturalHeight > {without}")
    with pg.expect_download() as dl:
        pg.click("#image-sheet-download")
    path = tmp_path / "with-notes.jpg"
    dl.value.save_as(path)
    with Image.open(path) as im:
        assert im.height > without
