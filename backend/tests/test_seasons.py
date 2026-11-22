"""In season (owner, 2026-11-18): Quebec produce in high season by month,
from Mangez Québec's 2026 calendar (app/seasons.py); not greenhouse
produce, potatoes or the everyday aromatics."""
import uuid

import pytest

from app import seasons
from app.seasons import line_months

M = "JFMAMJJASOND"


def months(line):
    m = line_months(line)
    return "".join(M[i] if m >> i & 1 else "." for i in range(12))


@pytest.mark.parametrize("line, expected", [
    ("2 cups strawberries, hulled", ".....JJASO.."),
    ("2 cups (500 ml) fraises", ".....JJASO.."),
    ("3 tomatoes, diced", "........S..."),
    ("1/2 chou", ".......AS..."),
    ("1 chou-fleur", "......JAS..."),             # the longest name, not "chou"
    ("1 lb Brussels sprouts", "........SO.."),
    ("3 échalotes, hachées", "......JA...."),      # Quebec: green onions
    ("2 patates douces", "..........ND"),
    ("1 butternut squash", ".........ON."),
    ("2 courgettes", "......JA...."),              # not "courge"
    ("3 pommes, pelées", "........SO.."),
    ("1 tasse de mûres", ".......AS..."),
    ("1 red bell pepper", "........S..."),
    ("1 jalapeño, seeded", "........S..."),
    ("4 stalks rhubarb", "....MJJAS..."),        # the calendar's available months (review of 0216)
    ("1 bunch asparagus", "....MJ......"),
    # Claude review of 0225: a preserved form only as an alternative.
    ("6 tomatoes (or 1 can)", "........S..."),
    ("6 tomatoes, or 1 can", "........S..."),
    ("2 cups blueberries, fresh or frozen", "......JA...."),
    ("2 cups blueberries, frozen or fresh", "......JA...."),
])
def test_produce_in_its_months(line, expected):
    assert months(line) == expected


@pytest.mark.parametrize("line", [
    "4 pommes de terre", "2 lbs potatoes",                      # high season all year
    "1 English cucumber", "1 cup sliced mushrooms", "1 laitue Boston", "2 endives",   # greenhouse, indoors
    "1 onion, chopped", "2 cloves garlic", "2 échalotes françaises", "1/4 cup parsley",  # aromatics
    "1 cup frozen blueberries", "1 cup blueberries, frozen", "1 can (796 ml) diced tomatoes",
    "2 tbsp tomato paste", "1 cup applesauce", "1 tsp red pepper flakes", "2 tbsp cornstarch",
    "1 c. à thé de piment de la Jamaïque", "2 bananes mûres", "1 pineapple", "Sauce:", "",
    # Review of 0216: canned without the word "canned", and choux pastry.
    "1 (796 ml) can tomatoes", "1 boîte (796 ml) de tomates", "2 tins chopped tomatoes", "1 jar roasted red peppers",
    "1 recette de pâte à choux",
    # Claude review of 0228: every alternative preserved, or a bracketed note.
    "4 cups strawberries (frozen)", "2 cups frozen or canned corn",
])
def test_not_counted(line):
    assert line_months(line) == 0


def test_table_names_are_folded_and_months_valid():
    for en, fr, months_ in seasons.PRODUCE:
        for name in en + fr:
            assert name == seasons._fold(name).strip(), name
        assert months_ is None or (months_ and all(1 <= m <= 12 for m in months_)), (en, fr)


def _recipe(client, title, lines):
    body = {"title": title, "source_type": "manual", "ingredients": [{"raw_line": x} for x in lines],
            "steps": ["Cook."], "tags": []}
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_the_list_keeps_or_leaves_out_recipes_in_season(client):
    sfx = uuid.uuid4().hex[:8]
    ids = [_recipe(client, f"Pie {sfx}", ["2 cups flour", "6 cups sliced apples"]),
           _recipe(client, f"Salad {sfx}", ["1 head romaine", "1 cucumber"]),
           _recipe(client, f"Soup {sfx}", ["4 potatoes", "1 onion", "1 cup sliced mushrooms"])]
    try:
        def titles(**params):
            r = client.get("/api/recipes", params={"q": sfx, **params})
            assert r.status_code == 200, r.text
            return {x["title"].split()[0] for x in r.json()}
        assert titles(in_season="true", month=10) == {"Pie"}
        assert titles(in_season="true", month=7) == {"Salad"}
        assert titles(in_season="false", month=7) == {"Pie", "Soup"}
        assert titles(in_season="true", month=1) == set()
        assert titles() == {"Pie", "Salad", "Soup"}
        assert client.get("/api/recipes", params={"in_season": "true", "month": 13}).status_code == 422
    finally:
        client.post("/api/recipes/batch-delete", headers={"X-Requested-With": "XMLHttpRequest"}, json={"ids": ids})
