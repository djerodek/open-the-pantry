"""Search (0117): typo tolerance, words to leave out, best match first,
where it matched. app/search.py."""
import json
import uuid

import pytest

from app import search


def _recipe(client, title, ingredients=("1 cup rice",), steps=("Cook.",), tags=()):
    body = {"title": title, "source_type": "manual", "ingredients": [{"raw_line": i} for i in ingredients],
            "steps": list(steps), "tags": [{"name": t, "category": "custom"} for t in tags]}
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _search(client, q, **params):
    search.forget_vocabulary()
    r = client.get("/api/recipes", params={"q": q, **params})
    assert r.status_code == 200, r.text
    corrected = json.loads(r.headers.get("X-Search-Corrected", "{}"))
    return r.json(), corrected


@pytest.fixture
def library(client):
    made = [
        _recipe(client, "Quorkleberry pie", ingredients=["2 cups flour", "3 cups quorkleberries"]),
        _recipe(client, "Zelmonade", ingredients=["4 zelmons", "1 cup sugar"], steps=["Squeeze the zelmons."]),
        _recipe(client, "Brambish tart", ingredients=["1 cup brambish jam", "1 egg"], tags=["Brambish"]),
        _recipe(client, "Brambish eggplant bake", ingredients=["1 eggplant", "brambish oil"]),
        _recipe(client, "Brambish curry", ingredients=["2 eggs", "brambish paste"]),
    ]
    yield {r["title"]: r for r in made}
    for r in made:
        client.delete(f"/api/recipes/{r['id']}")
        client.delete(f"/api/recently-deleted/{r['id']}")


def _titles(results):
    return [r["title"] for r in results]


def test_a_misspelt_word_finds_the_close_one(client, library):
    found, corrected = _search(client, "quorkelbery")          # two edits, 11 letters
    assert "Quorkleberry pie" in _titles(found)
    assert "quorkleberry" in corrected["quorkelbery"]
    found, corrected = _search(client, "zelmonaed")             # two letters swapped: one edit
    assert _titles(found) == ["Zelmonade"] and "zelmonade" in corrected["zelmonaed"]
    found, corrected = _search(client, "zelmns")                 # a letter missing, short word
    assert "Zelmonade" in _titles(found)


def test_plurals_and_stems(client, library):
    """"berries" used to miss "berry" (and the reverse): matching is from
    the start of words."""
    found, corrected = _search(client, "quorkleberrys")
    assert "Quorkleberry pie" in _titles(found) and corrected["quorkleberrys"][0] in ("quorkleberry", "quorkleberries")
    assert search.stem("berries") == "berr" and search.stem("tomatoes") == "tomato" and search.stem("eggs") == "eggs"


def test_a_word_that_matches_isnt_corrected(client, library):
    found, corrected = _search(client, "zelm")                   # the start of zelmons
    assert "Zelmonade" in _titles(found) and corrected == {}
    found, corrected = _search(client, "xqz")                    # under four letters: never corrected
    assert found == [] and corrected == {}


def test_leave_words_out(client, library):
    found, _ = _search(client, "brambish -curry")
    assert set(_titles(found)) == {"Brambish tart", "Brambish eggplant bake"}
    # "-egg" leaves out egg and eggs, not eggplant.
    found, _ = _search(client, "brambish -egg")
    assert _titles(found) == ["Brambish eggplant bake"]
    # Only words to leave out: everything else.
    found, _ = _search(client, "-brambish")
    titles = _titles(found)
    assert "Zelmonade" in titles and not any(t.startswith("Brambish") for t in titles)


def test_best_match_first(client, library):
    found, _ = _search(client, "brambish", sort="relevance")
    titles = _titles(found)
    # The tagged one (title + tag + ingredients) above ones matching in title and ingredients only.
    assert titles[0] == "Brambish tart" and len(titles) == 3
    # Any other sort is kept as chosen.
    found, _ = _search(client, "brambish", sort="title", direction="asc")
    assert _titles(found) == sorted(_titles(found), key=str.lower)
    # Without words to find, "relevance" is the default order.
    assert client.get("/api/recipes", params={"sort": "relevance"}).status_code == 200


def test_where_it_matched(client, library):
    found, _ = _search(client, "sugar zelmonade")
    hit = next(r for r in found if r["title"] == "Zelmonade")
    assert hit["match_snippet"] is None                          # in the title: nothing to add
    found, _ = _search(client, "squeeze")
    hit = next(r for r in found if r["title"] == "Zelmonade")
    assert f"{search.MARK_START}Squeeze{search.MARK_END}" in hit["match_snippet"]


def test_query_syntax_stays_plain_text(client, library):
    for q in ['"', 'title:x', '{tags_text}', 'a OR b', 'NOT', '-', '*', '("x"']:
        assert client.get("/api/recipes", params={"q": q}).status_code == 200, q


@pytest.mark.parametrize("a,b,d", [
    ("chicken", "chiken", 1), ("tomato", "tomatoe", 1), ("recipe", "reicpe", 1),
    ("abc", "abc", 0), ("kitten", "sitting", 3),
])
def test_edit_distance(a, b, d):
    assert search.edit_distance(a, b, 3) == d


def test_accents_and_case(client):
    r = _recipe(client, f"Crème brûlée {uuid.uuid4().hex[:4]}")
    try:
        found, corrected = _search(client, "CREME brulee")
        assert r["id"] in [x["id"] for x in found] and corrected == {}
    finally:
        client.delete(f"/api/recipes/{r['id']}")
        client.delete(f"/api/recently-deleted/{r['id']}")
