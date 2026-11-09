"""Page behaviour checked in the JavaScript source, where tests_browser/ has no
test for it. Prefer a browser test when adding one."""
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


JS = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")


PICKER_JS = ROOT / "frontend-picker" / "picker.js"


FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")


def test_the_list_shows_only_the_newest_answer():
    js = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    body = js[js.index("  async function loadRecipes()"):js.index("// Recently deleted (0109)")]
    assert "const generation = ++recipesGeneration;" in body
    assert body.index("if (stale()) return;") < body.index("currentRecipes = recipes;")


def test_busy_state_stays_in_its_own_dialog():
    body = JS[JS.index("function setBusy("):JS.index("function setBusy(") + 500]
    assert "topOverlay() || document" in body and "busyDisabled" in body


def test_startup_survives_a_failed_tag_or_time_load():
    for name in ("async function loadTags()", "async function loadTimeBuckets()"):
        body = JS[JS.index(name):JS.index(name) + 700]
        assert "try {" in body and "catch" in body


def test_rating_and_notes_saves_never_reject_and_update_the_list():
    for name in ("async function patchRating(", "async function patchNotes("):
        body = JS[JS.index(name):JS.index(name) + 500]
        assert "catch {" in body and "syncListEntry(" in body
    close = JS[JS.index("function closeRecipeDetail()"):JS.index("function closeRecipeDetail()") + 400]
    assert "renderRecipeList(currentRecipes)" in close
    assert "if (openOverlay === detailOverlay) { closeRecipeDetail(); return; }" in JS


def test_busy_state_covers_text_boxes_and_lists():
    from pathlib import Path
    js = (Path(__file__).parents[2] / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert '".modal button, .modal input, .modal textarea, .modal select"' in js


def test_no_page_sets_html_from_data():
    """Audit of 0143: a future innerHTML of recipe text would run (the CSP
    allows <img onerror>). innerHTML is only ever emptied, and el()'s
    trustedStaticHtml only gets the fixed SVG constants."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for f in [root / "frontend/js/app.js", *sorted((root / "frontend-picker").glob("*.js"))]:
        js = f.read_text(encoding="utf-8")
        assert "insertAdjacentHTML" not in js and "outerHTML" not in js and "document.write" not in js, f.name
        for m in re.finditer(r"\.innerHTML\s*=\s*([^;\n]+)", js):
            assert m.group(1).strip() in ('""', "v"), (f.name, m.group(0))
        for m in re.finditer(r"trustedStaticHtml:\s*([^,}\n]+)", js):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*_SVG", m.group(1).strip()), (f.name, m.group(0))
        # The one `= v` is el()'s trustedStaticHtml branch.
        assert len(re.findall(r"\.innerHTML\s*=\s*v\b", js)) <= 1


@pytest.mark.skipif(not PICKER_JS.exists(), reason="frontend not present")
def test_out_of_date_pick_is_dropped():
    js = PICKER_JS.read_text(encoding="utf-8")
    toggle = js[js.index("function toggleChip("):js.index("// Picking")]
    clear = js[js.index("function clearAll("):js.index('pickButton.addEventListener("click"')]
    pick = js[js.index("async function pick("):js.index("function clearAll(")]
    assert "state.generation += 1" in toggle
    assert "state.generation += 1" in clear and "state.cleared = true" in clear
    assert "const generation = state.generation;" in pick
    # Checked after every wait: each request, the shuffle, and a failure.
    waits = [i for i in range(len(pick)) if pick.startswith("await ", i)]
    assert len(waits) == 3
    for i in waits:
        assert "if (stale())" in pick[i:i + 200], pick[i:i + 80]
    assert "catch (err) {\n      if (stale()) return;" in pick
    # Nothing about the round or the recipe on screen changes until the pick
    # has survived the shuffle.
    after_shuffle = pick.index("await shuffle(")
    assert pick.index("state.current = recipe;") > after_shuffle
    assert pick.index("state.shown = shown.filter(") > after_shuffle
    # A chip change puts back what the shuffle replaced; Clear's empty area stays.
    assert "if (!state.cleared) restore();" in pick


def test_service_worker_never_caches_logs_or_https_status():
    sw = open(os.path.join(FRONTEND, "service-worker.js"), encoding="utf-8").read()
    block = re.search(r"const NEVER_CACHE = \[(.*?)\];", sw, re.S).group(1)
    assert r"/^\/api\/logs/" in block and r"/^\/api\/https/" in block


def test_app_reads_storage_only_through_guarded_calls():
    """localStorage throws when storage is blocked; one unguarded read at
    startup stops the whole app."""
    js = open(os.path.join(FRONTEND, "js", "app.js"), encoding="utf-8").read()
    for m in re.finditer(r"localStorage\.(?:get|set)Item", js):
        line_start = js.rfind("\n", 0, m.start()) + 1
        context = js[max(0, line_start - 200):js.find("\n", m.end())]
        assert "try" in context, js[line_start:js.find("\n", m.end())]
