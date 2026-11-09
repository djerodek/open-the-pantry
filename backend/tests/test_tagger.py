import os
import sys

import pytest

from app.ingestion.tagger import suggest_tags  # noqa: E402


def _names(suggestions):
    return {name for name, category, subgroup in suggestions}


def test_chicken_detected():
    suggestions = suggest_tags("Roast Chicken", ["1 whole chicken", "salt", "pepper"])
    assert "Chicken" in _names(suggestions)


def test_no_meat_suggests_vegetarian():
    suggestions = suggest_tags("Veggie Stir Fry", ["broccoli", "carrots", "soy sauce"])
    assert "Vegetarian" in _names(suggestions)


def test_meat_present_no_vegetarian():
    suggestions = suggest_tags("Beef Stew", ["beef chuck", "carrots", "potatoes"])
    names = _names(suggestions)
    assert "Beef" in names
    assert "Vegetarian" not in names


def test_word_boundary_avoids_false_positive():
    """'ham' as a substring of 'shamrock' should not trigger a Pork match --
    confirms the \\b word-boundary regex actually does its job."""
    suggestions = suggest_tags("Shamrock Shake", ["milk", "green food coloring", "ice"])
    assert "Pork" not in _names(suggestions)


def test_cocktail_cooking_style_has_subgroup():
    suggestions = suggest_tags(
        "Whiskey Sour", ["2oz whiskey", "3/4oz lemon juice"], "Shake well with ice and strain."
    )
    shaken = [s for s in suggestions if s[0] == "Shaken"]
    assert shaken, "Expected 'Shaken' to be suggested from 'shake well' in the steps text"
    assert shaken[0][2] == "cocktail"  # subgroup


def test_oven_cooking_style_detected():
    suggestions = suggest_tags(
        "Baked Salmon", ["salmon fillet", "lemon"], "Bake in the oven at 400F for 15 minutes."
    )
    assert "Oven" in _names(suggestions)


# --- input validation and case sensitivity ---

def test_suggest_tags_handles_empty_and_none_inputs():
    """Ingestion can legitimately produce an empty title or ingredient
    list (a failed parse); tag suggestion must not crash on it."""
    assert isinstance(suggest_tags("", []), list)
    assert isinstance(suggest_tags(None, []), list)
    assert isinstance(suggest_tags("Title", [], None), list)
    assert isinstance(suggest_tags("", [], ""), list)


def test_suggest_tags_is_case_insensitive():
    for variant in ["CHICKEN", "Chicken", "cHiCkEn"]:
        names = _names(suggest_tags("Dish", [f"1 lb {variant}"]))
        assert "Chicken" in names, f"{variant!r} should match"


def test_suggest_tags_deduplicates_repeated_triggers():
    """Several ingredients triggering the same tag should yield it once,
    not once per match."""
    suggestions = suggest_tags("Chicken Dish", ["2 lb chicken thighs", "1 cup chicken stock", "chicken fat"])
    names = [n for n, _, _ in suggestions]
    assert names.count("Chicken") == 1


def _make(client, title, ingredients):
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual", "steps": ["Simmer gently."],
                                          "ingredients": [{"raw_line": i} for i in ingredients]})
    return r.json()["id"]


@pytest.mark.parametrize("title", ["Moules à la marinière", "Moules à la crème", "Moules à l'ail",
                                   "Moules au curry"])
def test_mussels_are_fish(title):
    from app.ingestion.tagger import suggest_tags
    names = {t[0] for t in suggest_tags(title, [])}
    assert "Vegetarian" not in names, (title, names)


def test_a_baking_tin_is_still_not_mussels():
    from app.ingestion.tagger import _BAKING_TIN, _fold
    for text in map(_fold, ("moule à gâteau", "moule à tarte", "moules en silicone", "moule de 23 cm")):
        assert _BAKING_TIN.search(text), text
    for text in map(_fold, ("moules à la marinière", "moules à l'ail", "moules a les herbes")):
        assert not _BAKING_TIN.search(text), text


@pytest.mark.parametrize("title,ingredients,tag", [
    ("Braised Oxtail", ["2 lb oxtail", "1 onion"], "Beef"),
])
def test_tagger_oxtail_is_beef(title, ingredients, tag):
    from app.ingestion.tagger import suggest_tags
    names = [t[0] for t in suggest_tags(title, ingredients)]
    assert tag in names and "Vegetarian" not in names


def test_tagger_bone_marrow_is_not_vegetarian_but_vegetable_marrow_is():
    from app.ingestion.tagger import suggest_tags
    assert "Vegetarian" not in [t[0] for t in suggest_tags("Roasted Bone Marrow", ["4 bone marrow canoes", "parsley"])]
    assert "Vegetarian" in [t[0] for t in suggest_tags("Stuffed Marrow", ["1 large marrow", "rice", "tomatoes"])]


# Found by running the tagger over a real recipe library (not in the review).
@pytest.mark.parametrize("title,ingredients,expect,forbid", [
    ("Irish Stew", ["beef chuck", "bacon", "chicken stock/broth", "Guinness"], {"Beef", "Pork"}, {"Chicken", "Vegetarian"}),
    ("Pork Tenderloin with Egg Noodles", ["pork tenderloin", "egg noodles", "chicken stock"], {"Pork"}, {"Chicken"}),
    ("Garlic Pork Stir-Fry", ["ground pork", "soy sauce", "oyster sauce", "unsalted chicken broth"], {"Pork"}, {"Chicken", "Fish"}),
    ("Blade Roast", ["potatoes", "carrots", "1 blade roast", "onion soup mix"], {"Beef"}, {"Vegetarian"}),
    ("Pad See Ew with Tofu", ["tofu", "rice noodles", "fish sauce"], set(), {"Fish", "Vegetarian"}),
    ("Minestrone", ["beans", "pasta", "vegetable broth"], {"Vegetarian"}, set()),
])
def test_stock_and_sauce_are_not_the_main_ingredient(title, ingredients, expect, forbid):
    from app.ingestion.tagger import suggest_tags
    names = {t[0] for t in suggest_tags(title, ingredients)}
    assert expect <= names and not (forbid & names), names


@pytest.mark.parametrize("title", ["Moules de bouchot", "Moules de corde", "Moules en sauce", "Moules de Bretagne",
                                   "Moules à la marinière", "Moules à la crème", "Moules à l'ail", "Moules au beurre",
                                   "Moules au curry", "Moules frites"])
def test_mussels_are_fish_0134(title):
    from app.ingestion.tagger import suggest_tags
    names = {t[0] for t in suggest_tags(title, [])}
    assert "Fish" in names and "Vegetarian" not in names, (title, names)


@pytest.mark.parametrize("text", ["moule à gâteau", "moule à tarte", "moules à muffins", "moule en silicone",
                                  "moule de 23 cm", "moule à fond amovible", "moule à charnière", "moule à manqué",
                                  "moule beurré", "un moule rond", "moule à cake"])
def test_baking_tins(text):
    from app.ingestion.tagger import _BAKING_TIN, _fold
    assert _BAKING_TIN.search(_fold(text)), text


def test_a_tin_in_the_steps_is_not_fish():
    from app.ingestion.tagger import suggest_tags
    names = {t[0] for t in suggest_tags("Gâteau au chocolat", ["200 g de chocolat", "3 oeufs"],
                                        "Beurrer un moule à manqué de 22 cm. Verser dans le moule à gâteau.")}
    assert "Fish" not in names


def test_skipped_recipes_are_left_out_of_the_scan(client):
    rid = _make(client, "Skip Me Salmon", ["2 salmon fillets"])
    pre = client.post("/api/tags/auto-apply?dry_run=true").json()
    assert any(c["id"] == rid for c in pre["changes"])
    r = client.patch(f"/api/recipes/{rid}/autotag-ignore", json={"ignored": True})
    assert r.status_code == 200 and r.json()["autotag_ignored"] is True
    after = client.post("/api/tags/auto-apply?dry_run=true").json()
    assert all(c["id"] != rid for c in after["changes"]) and after["recipes_ignored"] >= 1
    client.post("/api/tags/auto-apply")
    assert not client.get(f"/api/recipes/{rid}").json()["tags"]

    # A normal edit (PUT) doesn't reset it; the edit screen's checkbox does.
    rec = client.get(f"/api/recipes/{rid}").json()
    client.put(f"/api/recipes/{rid}", json={"title": "Skip Me Salmon", "source_type": "manual",
                                            "ingredients": [{"raw_line": "2 salmon fillets"}], "steps": ["x"]})
    assert client.get(f"/api/recipes/{rid}").json()["autotag_ignored"] is True
    client.patch(f"/api/recipes/{rid}/autotag-ignore", json={"ignored": False})
    assert any(c["id"] == rid for c in client.post("/api/tags/auto-apply?dry_run=true").json()["changes"])


def test_suggest_for_the_edit_form(client):
    r = client.post("/api/tags/suggest", json={
        "title": "Weeknight Clam Pasta", "ingredients": ["1 lb clams", "linguine"], "steps": ["Simmer the sauce."],
        "current_tags": ["Dinner", "stovetop"]})
    got = {t["name"]: t["category"] for t in r.json()}
    assert got.get("Fish") == "main_ingredient"
    assert "Stovetop" not in got           # already listed (case-insensitive)
    assert "Vegetarian" not in got


def test_suggest_skips_vegetarian_next_to_a_listed_meat_tag(client):
    r = client.post("/api/tags/suggest", json={"title": "Sunday Dinner", "ingredients": ["potatoes", "soup mix"],
                                               "current_tags": ["Beef"]})
    assert "Vegetarian" not in {t["name"] for t in r.json()}


def test_tagger_does_not_call_lamb_or_turkey_vegetarian():
    from app.ingestion.tagger import suggest_tags
    for title, ings in [("Roast Leg of Lamb", ["lamb"]), ("Thanksgiving Turkey", ["turkey"]),
                        ("Moules marinières", ["mussels"]), ("Osso Buco", ["veal shanks"])]:
        tags = [n for n, c, _ in suggest_tags(title, ings, "")]
        assert "Vegetarian" not in tags, title
    assert "Vegetarian" in [n for n, c, _ in suggest_tags("Lentil Soup", ["lentils"], "")]
