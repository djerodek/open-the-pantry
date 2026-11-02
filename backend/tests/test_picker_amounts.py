"""0155: What to eat?'s reader has the main app's ½ / 1× / 2× and As
written / Weights / Volumes, from the same files (amounts.js, weights.js),
served on the picker port by name only; the recipe carries its language."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.fixture(scope="module")
def picker_client(client):
    from app.main import picker_app
    return TestClient(picker_app)


def test_the_shared_scripts_are_served_on_the_picker_port(picker_client):
    c = picker_client
    for name in ("i18n.js", "weights.js", "amounts.js"):
        r = c.get(f"/js/{name}")
        assert r.status_code == 200, name
        assert r.text == (ROOT / "frontend" / "js" / name).read_text(encoding="utf-8"), name
        assert r.headers["cache-control"] == "no-cache", name
    for name in ("app.js", "../js/app.js", "%2e%2e/index.html", "service-worker.js"):
        assert c.get(f"/js/{name}").status_code == 404, name
    page = c.get("/").text
    assert page.index("/js/i18n.js") < page.index("/js/weights.js") < page.index("/js/amounts.js") < page.index("picker.js")


@pytest.mark.parametrize("language,title,lines,want", [
    ("fr", "Explicit", ["1 cup flour"], "fr"),
    (None, "Gâteau du dimanche", ["250 g de farine", "2 oeufs", "Mélanger la farine et les oeufs dans un bol."], "fr"),
    (None, "Sunday cake", ["250 g flour", "2 eggs", "Mix the flour and the eggs in a bowl."], "en"),
])
def test_the_reader_gets_the_recipes_language(client, picker_client, language, title, lines, want):
    body = {"title": title, "source_type": "manual", "ingredients": [{"raw_line": t} for t in lines[:2]],
            "steps": lines[2:] or ["Bake."], "tags": [{"name": "Dinner", "category": "meal_type"}]}
    if language:
        body["language"] = language
    rid = client.post("/api/recipes", json=body).json()["id"]
    try:
        got = picker_client.get(f"/api/picker/recipes/{rid}").json()
        assert got["language"] == want and got["ingredients"][0] == lines[0]
    finally:
        client.delete(f"/api/recipes/{rid}")
        client.delete(f"/api/recently-deleted/{rid}")


def test_both_pages_use_the_one_copy():
    app = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    picker = (ROOT / "frontend-picker/picker.js").read_text(encoding="utf-8")
    for js in (app, picker):
        assert "ingredientControls(" in js
        for own in ("function isPartName(", "function scaleLineInfo(", "function weightLineInfo(",
                    "function volumeLineInfo(", "function scaleButtons("):
            assert own not in js, own
    css = (ROOT / "frontend-picker/picker.css").read_text(encoding="utf-8")
    assert ".units-disclaimer {" in css and "html.reader-open .units-disclaimer" in css
