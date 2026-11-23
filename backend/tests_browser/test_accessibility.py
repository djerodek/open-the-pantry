"""Accessibility scan (0112): axe-core (bundled in axe-playwright-python,
pinned in requirements-browser.txt) on every screen of both pages, in
English/light and French/dark. A test fails on any "serious" or
"critical" finding; "moderate" and "minor" ones are printed (pytest -s)
but don't fail it.

Rules: WCAG 2.0/2.1/2.2 A and AA, plus axe's best practices. One rule is
off on purpose: meta-viewport (pinch-zoom on phones is disabled, the
owner's decision in 0064; Settings -> Text size is the alternative).

What this can't see: whether the order of things makes sense to a
screen-reader user, or whether the wording is clear. Automated checks
find roughly a third of accessibility problems."""
import pathlib
import uuid

import axe_playwright_python
import pytest

from .conftest import add_recipe, wait_until
from .test_pages import _open_settings

AXE_SOURCE = (pathlib.Path(axe_playwright_python.__file__).parent / "axe.min.js").read_text(encoding="utf-8")
FAILING = ("serious", "critical")
RUN = """async (within) => {
  const r = await axe.run(within ? document.querySelector(within) : document, {
    resultTypes: ["violations"],
    runOnly: {type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]},
    rules: {"meta-viewport": {enabled: false}},
  });
  return r.violations.map((v) => ({id: v.id, impact: v.impact, help: v.help,
    nodes: v.nodes.slice(0, 5).map((n) => n.target.join(" ") + ": " + (n.failureSummary || "").split("\\n").slice(1, 2).join(""))}));
}"""
VARIANTS = [("en", "light"), ("fr", "dark")]


class Scanner:
    def __init__(self, pg, label):
        self.pg, self.label, self.failures = pg, label, []

    def scan(self, screen, within=None):
        """`within`: a selector to scan only that part. For an open menu:
        whatever it covers counts as an obscured target (target-size), as
        any menu drawn over a page would."""
        # A sheet fading in has blended colours: contrast is measured once
        # every animation and transition has finished.
        wait_until(self.pg, "document.getAnimations().every((a) => a.playState !== 'running')")
        # Through evaluate, which the page's Content-Security-Policy doesn't
        # apply to; an injected <script> would be blocked (and reported).
        if not self.pg.evaluate("typeof window.axe !== 'undefined'"):
            self.pg.evaluate(AXE_SOURCE)
        for v in self.pg.evaluate(RUN, within):
            where = f"{self.label} / {screen}: [{v['impact']}] {v['id']} ({v['help']})\n      " + "\n      ".join(v["nodes"])
            if v["impact"] in FAILING:
                self.failures.append(where)
            else:
                print("accessibility (not failing):", where)

    def check(self):
        assert not self.failures, "Accessibility findings:\n" + "\n".join(self.failures)


@pytest.fixture(scope="module")
def library(server):
    import httpx
    from .conftest import H
    with httpx.Client(base_url=server["url"], headers=H, timeout=30) as api:
        sfx = uuid.uuid4().hex[:6]
        soup = add_recipe(api, f"A11y soup {sfx}", servings="4", source_url="https://example.com/soup",
                          tags=[{"name": "Dinner", "category": "meal_type"}])
        stew = add_recipe(api, f"A11y stew {sfx}")
        gone = add_recipe(api, f"A11y gone {sfx}")
        api.delete(f"/api/recipes/{gone['id']}")
        yield {"soup": soup, "stew": stew}


@pytest.mark.parametrize("lang,scheme", VARIANTS)
def test_main_page(server, page, library, lang, scheme):
    p = page(locale=lang, color_scheme=scheme)
    pg = p.page
    s = Scanner(pg, f"{lang}/{scheme}")
    soup, stew = library["soup"], library["stew"]
    pg.goto(server["url"])
    pg.wait_for_selector(f"text={soup['title']}")
    s.scan("recipe list")
    pg.fill("#search-input", "flourr")                      # a typo, matched in the ingredients
    pg.press("#search-input", "Enter")
    wait_until(pg, "!document.querySelector('#search-note').hidden && !!document.querySelector('.card-snippet mark')")
    s.scan("search with a typo")
    pg.click("#search-clear")
    wait_until(pg, "document.querySelector('#search-note').hidden")
    pg.click("#sidebar-toggle")
    pg.wait_for_selector(".review-filter[aria-controls=bin-overlay]")
    s.scan("side menu")
    pg.click(".review-filter[aria-controls=bin-overlay]")
    pg.wait_for_selector("#bin-overlay:not([hidden])")
    s.scan("Recently deleted")
    pg.click("#bin-close")
    pg.click("#sidebar-toggle")

    pg.click(f"text={soup['title']}")
    pg.wait_for_selector("#recipe-detail-heading")
    s.scan("recipe")
    pg.click(".recipe-aids .ref-toggle")
    s.scan("quick reference menu", within=".recipe-aids .ref-wrap")
    pg.click(".recipe-aids .ref-menu button[data-chart=meat]")
    pg.wait_for_selector("#reference-overlay:not([hidden]) .ref-sources")
    s.scan("meat cooking times")
    pg.click(".ref-switch button[data-chart=baking]")
    pg.wait_for_selector("#reference-body .ref-subs")
    s.scan("baking substitutions")
    pg.click("#reference-close")
    pg.click(".share-toggle")
    pg.click("#share-menu button >> nth=-2")                   # Image (JPG)
    wait_until(pg, "document.querySelector('#image-sheet-preview img')?.naturalWidth > 0")
    s.scan("share as image")
    pg.click("#image-sheet-close")
    pg.click("#recipe-detail-body button:has-text('✎')")
    pg.wait_for_selector("#edit-title")
    s.scan("edit recipe")
    pg.click("#recipe-detail-close")
    pg.wait_for_selector("#recipe-detail-overlay", state="hidden")

    pg.click("#add-recipe-fab")
    pg.wait_for_selector("#add-recipe-overlay:not([hidden]) .source-type-picker")
    s.scan("add: choose a source")
    pg.click(".source-type-picker button >> nth=3")
    pg.wait_for_selector("#manual-title")
    s.scan("add manually")
    pg.click("#add-recipe-close")
    pg.wait_for_selector("#add-recipe-overlay", state="hidden")

    pg.click("#select-mode-toggle")
    pg.click(f".recipe-card:has-text('{soup['title']}')")
    pg.click(f".recipe-card:has-text('{stew['title']}')")
    pg.wait_for_selector("#batch-bar:not([hidden])")
    s.scan("select mode")
    pg.click("#batch-tags-btn")
    pg.wait_for_selector("#batch-tags-overlay:not([hidden])")
    s.scan("tag the selected")
    pg.click("#batch-tags-close")
    pg.click("#batch-merge-btn")
    pg.wait_for_selector("#merge-overlay:not([hidden])")
    s.scan("merge")
    pg.click("#merge-close")
    pg.click("#batch-cancel-btn")

    _open_settings(p, server["url"])
    pg.evaluate("document.querySelectorAll('#settings-overlay details').forEach((d) => { d.open = true; })")
    pg.click("#library-check-btn")
    wait_until(pg, "document.querySelectorAll('#library-check-result details').length > 0")
    pg.evaluate("document.querySelectorAll('#library-check-result details').forEach((d) => { d.open = true; })")
    s.scan("settings")
    s.check()


@pytest.mark.parametrize("lang,scheme", VARIANTS)
def test_what_to_eat_page(server, api, page, library, lang, scheme):
    assert api.put("/api/picker/settings", json={"enabled": True}).status_code == 200
    try:
        p = page(locale=lang, color_scheme=scheme)
        pg = p.page
        s = Scanner(pg, f"{lang}/{scheme} picker")
        pg.goto(server["picker_url"])
        pg.wait_for_selector("#pick-button")
        s.scan("stage")
        pg.goto(f"{server['picker_url']}/#recipe-{library['soup']['id']}")
        pg.wait_for_selector("#reader-overlay:not([hidden]) #reader-title")
        s.scan("recipe")
        # The main page's top bar on a phone: What to eat? in the charts' menu (0233).
        pg.goto(server["url"])
        wait_until(pg, "document.querySelector('#topbar-ref')?.classList.contains('is-merged')")
        pg.click("#topbar-ref-toggle")
        s.scan("main page: What to eat? and quick reference menu", within="#topbar-ref")
        s.check()
    finally:
        api.put("/api/picker/settings", json={"enabled": False})
