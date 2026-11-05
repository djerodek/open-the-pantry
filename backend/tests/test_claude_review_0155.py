"""Claude review of 0155: the JSON-LD fallback (used when recipe-scrapers
can't read a page) passed field values through as they were: a list as the
name gave a 500, an empty or numeric ingredient crashed the parser, HTML
was kept, a list as a section name or a number as the image crashed. (The
restore's email row: test_backup_settings_0143.py; Weights and Volumes:
test_weights.py, test_volumes.py.)"""
import json

import pytest


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _page(recipe):
    return f'<script type="application/ld+json">{json.dumps(recipe)}</script>'


@pytest.mark.parametrize("recipe,title,ingredients,steps", [
    ({"name": ["Soup", "Soupe"], "recipeIngredient": ["1 cup rice"], "recipeInstructions": "Cook."},
     "Soup", ["1 cup rice"], ["Cook."]),
    ({"name": "S", "recipeIngredient": ["", "   ", 3, None, {"a": 1}, ["x"], "1 <b>cup</b> rice &amp; beans"],
      "recipeInstructions": "Cook."},
     "S", ["1 cup rice & beans"], ["Cook."]),
    ({"name": "<i>Soupe</i> d&#39;&eacute;t&eacute;", "recipeIngredient": ["1&nbsp;egg"],
      "recipeInstructions": [{"@type": "HowToSection", "name": ["Pâte"], "itemListElement": [{"text": "<p>Boil.</p><p>Drain.</p>"}]}]},
     "Soupe d'été", ["1 egg"], ["Pâte:", "Boil. Drain."]),
    ({"name": 5, "recipeIngredient": "1 egg", "recipeInstructions": "Cook.\nServe.", "image": [5], "recipeYield": {"a": 1},
      "prepTime": ["PT5M"], "cookTime": 10},
     "5", ["1 egg"], ["Cook.", "Serve."]),
    ({"name": None, "recipeIngredient": {"not": "a list"}, "recipeInstructions": [{"text": 7}, {"name": "Stir."}]},
     "Untitled Recipe", [], ["Stir."]),
])
def test_the_fallback_reads_text_only(recipe, title, ingredients, steps):
    from app.ingestion.ingredient_parser import parse_ingredient_block
    from app.ingestion.url_ingest import _try_json_ld
    got = _try_json_ld(_page({"@type": "Recipe", **recipe}))
    assert (got.title, got.ingredients, got.steps) == (title, ingredients, steps)
    assert all(isinstance(x, str) for x in [got.title, *got.ingredients, *got.steps])
    for field in (got.servings, got.prep_time, got.cook_time, got.total_time, got.image_url):
        assert field is None or isinstance(field, str)
    parse_ingredient_block(got.ingredients)          # used to raise on "" or 3


def test_image_servings_and_times_shapes():
    from app.ingestion.url_ingest import _try_json_ld
    base = {"@type": "Recipe", "name": "R", "recipeIngredient": ["1 egg"], "recipeInstructions": "Cook."}
    for image, want in (([{"url": "https://example.com/a.jpg"}], "https://example.com/a.jpg"), ([None], None),
                        ({"url": 3}, None), ("  ", None), ("https://example.com/b.jpg", "https://example.com/b.jpg")):
        assert _try_json_ld(_page({**base, "image": image})).image_url == want, image
    got = _try_json_ld(_page({**base, "recipeYield": [4, "4 servings"], "prepTime": "PT10M"}))
    assert (got.servings, got.prep_time) == ("4", "PT10M")


def test_add_from_url_with_a_list_as_the_name(client, monkeypatch):
    """The whole path, recipe-scrapers or not: no 500."""
    from app.ingestion import url_ingest
    html = _page({"@type": "Recipe", "name": ["Lentil soup"], "recipeIngredient": ["1 cup lentils", ""],
                  "recipeInstructions": [{"@type": "HowToStep", "text": "Simmer <b>30</b> minutes."}]})
    got = url_ingest.parse_html(html, "https://example.com/soup")
    assert got.title and isinstance(got.title, str)
    assert all(isinstance(x, str) and x for x in got.ingredients)
