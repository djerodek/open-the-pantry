"""A recipe's language (0125): guessed from its text, correctable in Edit
recipe, carried by duplicate, merge and schema.org export/import, and the
Language filter in What to eat? (app/language.py, app/picker.py)."""
import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

H = {"X-Requested-With": "OpenThePantry"}

FR_STEPS = ["Faire fondre le beurre dans une poêle.", "Ajouter les oignons et cuire 10 minutes environ."]
FR_INGS = ["1 tasse de farine", "2 cuillères à soupe de beurre", "1 pincée de sel"]
EN_STEPS = ["Melt the butter in a pan.", "Add the onions and stir until soft."]
EN_INGS = ["1 cup of flour", "2 tablespoons butter", "1 pinch of salt"]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _ings(lines):
    return [{"raw_line": line} for line in lines]


def _make(client, title, ings, steps, tags=(), **extra):
    r = client.post("/api/recipes", headers=H, json={"title": title, "source_type": "manual",
                                                      "ingredients": _ings(ings), "steps": steps,
                                                      "tags": list(tags), **extra})
    assert r.status_code == 200, r.text
    return r.json()


def _put(client, recipe, **changes):
    body = {k: recipe[k] for k in ("title", "source_type", "source_url", "servings", "prep_time", "cook_time",
                                   "total_time", "image_path", "raw_text", "favorite", "tastiness_rating",
                                   "cook_time_rating", "difficulty_rating")}
    body["ingredients"] = [{"raw_line": i["raw_line"]} for i in recipe["ingredients"]]
    body["steps"] = [s["text"] for s in recipe["steps"]]
    body["tags"] = [{"name": t["name"], "category": t["category"]} for t in recipe["tags"]]
    body.update(changes)
    return client.put(f"/api/recipes/{recipe['id']}", headers=H, json=body)


# ---------------------------------------------------------------- the guess

def test_guess_language():
    from app.language import guess_language, recipe_language
    assert guess_language(" ".join(FR_INGS + FR_STEPS)) == "fr"
    assert guess_language(" ".join(EN_INGS + EN_STEPS)) == "en"
    assert guess_language("") == "en"          # nothing to go on: English
    assert guess_language(None) == "en"
    # The hand-set value wins; anything else is ignored.
    assert recipe_language("en", "Crêpes", " ".join(FR_STEPS)) == "en"
    assert recipe_language("fr", "Pancakes", " ".join(EN_STEPS)) == "fr"
    assert recipe_language("de", "Crêpes", " ".join(FR_STEPS)) == "fr"
    assert recipe_language(None, "Crêpes", " ".join(FR_STEPS)) == "fr"


def test_corpus_uses_the_shared_guess():
    from app import corpus, language
    assert corpus.guess_language is language.guess_language


def test_column_exists(client):
    from app.database import engine
    with engine.connect() as conn:
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(recipes)")}
    assert "language" in cols


# ---------------------------------------------------------------- the field

def test_new_recipe_is_guessed_and_can_be_set(client):
    fr = _make(client, "Crêpes", FR_INGS, FR_STEPS)
    en = _make(client, "Pancakes", EN_INGS, EN_STEPS)
    try:
        assert (fr["language"], fr["detected_language"]) == (None, "fr")
        assert (en["language"], en["detected_language"]) == (None, "en")
        set_on_create = _make(client, "Pancakes", EN_INGS, EN_STEPS, language="fr")
        assert (set_on_create["language"], set_on_create["detected_language"]) == ("fr", "en")
        client.delete(f"/api/recipes/{set_on_create['id']}", headers=H)
    finally:
        for r in (fr, en):
            client.delete(f"/api/recipes/{r['id']}", headers=H)


def test_put_sets_keeps_and_clears(client):
    r = _make(client, "Pancakes", EN_INGS, EN_STEPS)
    try:
        res = _put(client, r, language="fr")
        assert res.status_code == 200, res.text
        assert res.json()["language"] == "fr"
        # A client that doesn't send the field doesn't clear a correction.
        res = _put(client, res.json(), title="Pancakes again")
        assert res.status_code == 200, res.text
        assert res.json()["language"] == "fr"
        # null: back to the guess.
        res = _put(client, res.json(), language=None)
        assert res.json()["language"] is None
        assert res.json()["detected_language"] == "en"
        # Only the two languages.
        assert _put(client, res.json(), language="de").status_code == 422
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)


def test_detected_language_follows_edited_text(client):
    r = _make(client, "Soup", EN_INGS, EN_STEPS)
    try:
        res = _put(client, r, ingredients=_ings(FR_INGS), steps=FR_STEPS)
        assert res.status_code == 200, res.text
        assert res.json()["detected_language"] == "fr"
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)


def test_duplicate_copies_language(client):
    r = _make(client, "Pancakes", EN_INGS, EN_STEPS, language="fr")
    res = client.post(f"/api/recipes/{r['id']}/duplicate", headers=H)
    assert res.status_code == 200, res.text
    try:
        assert res.json()["language"] == "fr"
    finally:
        for rid in (r["id"], res.json()["id"]):
            client.delete(f"/api/recipes/{rid}", headers=H)


@pytest.mark.parametrize("content, expected", [("keep", None), ("other", "fr")])
def test_merge_language_goes_with_the_text(client, content, expected):
    keep = _make(client, "Pancakes", EN_INGS, EN_STEPS)
    other = _make(client, "Crêpes", FR_INGS, FR_STEPS, language="fr")
    try:
        res = client.post("/api/recipes/merge", headers=H, json={
            "keep_id": keep["id"], "other_id": other["id"], "take": {"content": content}})
        assert res.status_code == 200, res.text
        assert res.json()["language"] == expected
    finally:
        for rid in (keep["id"], other["id"]):
            client.delete(f"/api/recipes/{rid}", headers=H)


def test_schema_org_export_and_import_carry_language(client):
    r = _make(client, "Pancakes", EN_INGS, EN_STEPS, language="fr")
    plain = _make(client, "Crêpes", FR_INGS, FR_STEPS)
    new_ids = []
    try:
        doc = client.get(f"/api/recipes/{r['id']}/export.json").json()
        ext = next(v for k, v in doc.items() if isinstance(v, dict) and "difficulty_rating" in v)
        assert ext["language"] == "fr"
        plain_doc = client.get(f"/api/recipes/{plain['id']}/export.json").json()
        assert next(v for v in plain_doc.values() if isinstance(v, dict) and "difficulty_rating" in v)["language"] is None

        res = client.post("/api/import/recipes", headers=H,
                          files={"file": ("r.json", json.dumps(doc).encode(), "application/json")})
        assert res.status_code == 200, res.text
        new_ids = [s["recipe_id"] for s in res.json()["succeeded"]]
        assert len(new_ids) == 1
        assert client.get(f"/api/recipes/{new_ids[0]}").json()["language"] == "fr"

        # Anything else in the file is ignored.
        ext["language"] = "klingon"
        res = client.post("/api/import/recipes", headers=H,
                          files={"file": ("r.json", json.dumps(doc).encode(), "application/json")})
        more = [s["recipe_id"] for s in res.json()["succeeded"]]
        new_ids += more
        assert client.get(f"/api/recipes/{more[0]}").json()["language"] is None
    finally:
        for rid in [r["id"], plain["id"], *new_ids]:
            client.delete(f"/api/recipes/{rid}", headers=H)


# ---------------------------------------------------------------- What to eat?

@pytest.fixture()
def lang_library(client):
    sfx = uuid.uuid4().hex[:6]
    tag = {"name": f"PkLang{sfx}", "category": "main_ingredient"}
    made = {
        "fr": _make(client, f"Crêpes {sfx}", FR_INGS, FR_STEPS, tags=[tag]),
        "en": _make(client, f"Pancakes {sfx}", EN_INGS, EN_STEPS, tags=[tag]),
        "en_set_fr": _make(client, f"Fish {sfx}", EN_INGS, EN_STEPS, tags=[tag], language="fr"),
        "drink": _make(client, f"Kir {sfx}", FR_INGS, FR_STEPS,
                       tags=[tag, {"name": "Cocktails", "category": "meal_type"}]),
    }
    tag_id = next(t["id"] for t in client.get("/api/tags").json() if t["name"] == tag["name"])
    yield {k: v["id"] for k, v in made.items()}, tag_id
    for r in made.values():
        client.delete(f"/api/recipes/{r['id']}", headers=H)


def _matches(c, params):
    seen = []
    for _ in range(50):
        body = c.get("/api/picker/random", params={**params, "exclude": seen}).json()
        if body["recipe"] is None:
            break
        seen.append(body["recipe"]["id"])
    return set(seen)


def test_picker_language_filter(client, lang_library):
    ids, tag_id = lang_library
    from app.main import picker_app
    for c in (client, TestClient(picker_app)):
        base = {"main_ingredient": [tag_id]}
        assert _matches(c, base) == {ids["fr"], ids["en"], ids["en_set_fr"]}
        assert _matches(c, {**base, "language": ["fr"]}) == {ids["fr"], ids["en_set_fr"]}
        assert _matches(c, {**base, "language": ["en"]}) == {ids["en"]}
        # OR within the group.
        assert _matches(c, {**base, "language": ["en", "fr"]}) == {ids["fr"], ids["en"], ids["en_set_fr"]}
        assert c.get("/api/picker/random", params={"language": "de"}).status_code == 422


def test_picker_options_count_languages(client, lang_library):
    before = {x["value"]: x["count"] for x in client.get("/api/picker/options").json()["language"]}
    ids, _ = lang_library
    # The drink isn't counted; the hand-set one counts as French.
    assert before.get("fr", 0) >= 2 and before.get("en", 0) >= 1
    client.delete(f"/api/recipes/{ids['en_set_fr']}", headers=H)
    after = {x["value"]: x["count"] for x in client.get("/api/picker/options").json()["language"]}
    assert after.get("fr", 0) == before["fr"] - 1
    assert after.get("en", 0) == before["en"]
    # Only languages with recipes, in a fixed order.
    o = client.get("/api/picker/options").json()["language"]
    assert [x["value"] for x in o] == [v for v in ("en", "fr") if v in {x["value"] for x in o}]
    assert all(x["count"] > 0 for x in o)
