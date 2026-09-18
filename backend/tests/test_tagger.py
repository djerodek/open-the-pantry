import os
import sys

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
