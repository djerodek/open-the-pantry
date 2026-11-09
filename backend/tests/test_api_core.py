import re
import uuid

import pytest


def test_create_and_get_recipe(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    assert r.status_code == 200
    recipe_id = r.json()["id"]

    r = client.get(f"/api/recipes/{recipe_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == sample_recipe_payload["title"]
    assert len(body["ingredients"]) == 2
    assert len(body["steps"]) == 2


def test_get_nonexistent_recipe_404(client):
    r = client.get("/api/recipes/999999999")
    assert r.status_code == 404


def test_delete_recipe(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]

    r = client.delete(f"/api/recipes/{recipe_id}")
    assert r.status_code == 200

    r = client.get(f"/api/recipes/{recipe_id}")
    assert r.status_code == 404


def test_tag_and_filter_requires_all_selected_tags(client):
    r1 = client.post("/api/recipes", json={
        "title": "AND Filter Test Dinner Chicken",
        "source_type": "manual", "ingredients": [], "steps": [],
        "tags": [{"name": "Dinner", "category": "meal_type", "subgroup": None},
                 {"name": "Chicken", "category": "main_ingredient", "subgroup": None}],
    })
    r2 = client.post("/api/recipes", json={
        "title": "AND Filter Test Cocktail Only",
        "source_type": "manual", "ingredients": [], "steps": [],
        "tags": [{"name": "Cocktails", "category": "meal_type", "subgroup": None}],
    })
    assert r1.status_code == 200 and r2.status_code == 200
    id1, id2 = r1.json()["id"], r2.json()["id"]

    try:
        r = client.get("/api/recipes", params={"tags": ["Dinner", "Chicken"]})
        titles = [x["title"] for x in r.json()]
        assert "AND Filter Test Dinner Chicken" in titles
        assert "AND Filter Test Cocktail Only" not in titles

        r = client.get("/api/recipes", params={"tags": ["Dinner", "Cocktails"]})
        titles = [x["title"] for x in r.json()]
        assert titles == [], "no recipe has both tags -- AND logic should exclude both"
    finally:
        client.post("/api/recipes/batch-delete", json={"ids": [id1, id2]})


def test_search_matched_via_distinguishes_text_and_tag(client):
    r1 = client.post("/api/recipes", json={
        "title": "Unique Zzyzx Text Match Recipe",
        "source_type": "manual", "ingredients": [], "steps": [],
        "tags": [],
    })
    r2 = client.post("/api/recipes", json={
        "title": "Unrelated Title For Tag Match",
        "source_type": "manual", "ingredients": [], "steps": [],
        "tags": [{"name": "Zzyzx Custom Tag", "category": "custom", "subgroup": None}],
    })
    id1, id2 = r1.json()["id"], r2.json()["id"]

    try:
        r = client.get("/api/recipes", params={"q": "Zzyzx"})
        by_id = {x["id"]: x for x in r.json()}
        assert "text" in by_id[id1]["matched_via"]
        assert "tag" in by_id[id2]["matched_via"]
    finally:
        client.post("/api/recipes/batch-delete", json={"ids": [id1, id2]})


def test_search_with_special_characters_does_not_500(client):
    for q in ['chicken " soup', "-chicken", "tags_text:Dinner", '"unterminated']:
        r = client.get("/api/recipes", params={"q": q})
        assert r.status_code == 200


def test_time_filter(client):
    r1 = client.post("/api/recipes", json={
        "title": "Quick Time Filter Test",
        "source_type": "manual", "actual_cook_time": "00:00:15",
        "ingredients": [], "steps": [], "tags": [],
    })
    r2 = client.post("/api/recipes", json={
        "title": "Slow Time Filter Test",
        "source_type": "manual", "actual_cook_time": "00:03:00",
        "ingredients": [], "steps": [], "tags": [],
    })
    id1, id2 = r1.json()["id"], r2.json()["id"]

    try:
        r = client.get("/api/recipes", params={"max_minutes": 20})
        titles = [x["title"] for x in r.json()]
        assert "Quick Time Filter Test" in titles
        assert "Slow Time Filter Test" not in titles
    finally:
        client.post("/api/recipes/batch-delete", json={"ids": [id1, id2]})


def test_batch_delete_mixed_valid_and_invalid_ids(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    valid_id = r.json()["id"]

    r = client.post("/api/recipes/batch-delete", json={"ids": [valid_id, 999999999]})
    body = r.json()
    assert valid_id in body["deleted"]
    assert 999999999 in body["missing"]


def test_rating_fields_are_constrained(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        r = client.patch(f"/api/recipes/{recipe_id}/rating", json={"tastiness_rating": 99})
        assert r.status_code == 422  # out of the 1-5 range

        r = client.patch(f"/api/recipes/{recipe_id}/rating", json={"cook_time_rating": "instant"})
        assert r.status_code == 422  # not one of quick/moderate/long

        r = client.patch(f"/api/recipes/{recipe_id}/rating", json={"favorite": True})
        assert r.status_code == 200
        assert r.json()["favorite"] is True
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_ratings_can_be_cleared_with_explicit_null(client, sample_recipe_payload):
    """An explicit null clears a rating; omitting the key leaves it alone.

    These two cases are easy to collapse -- `if payload.x is not None` treats
    them identically, which makes a "clear rating" control return 200 and do
    nothing. The card rating sheet depends on the difference.
    """
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        r = client.patch(f"/api/recipes/{recipe_id}/rating", json={
            "tastiness_rating": 4, "cook_time_rating": "quick",
            "difficulty_rating": "hard", "favorite": True,
        })
        assert r.status_code == 200

        # Omitting a key must not disturb it.
        r = client.patch(f"/api/recipes/{recipe_id}/rating", json={"favorite": True})
        assert r.status_code == 200
        body = r.json()
        assert body["tastiness_rating"] == 4
        assert body["cook_time_rating"] == "quick"
        assert body["difficulty_rating"] == "hard"

        # An explicit null clears that one and only that one.
        r = client.patch(f"/api/recipes/{recipe_id}/rating", json={"tastiness_rating": None})
        assert r.status_code == 200
        body = r.json()
        assert body["tastiness_rating"] is None
        assert body["cook_time_rating"] == "quick"
        assert body["difficulty_rating"] == "hard"
        assert body["favorite"] is True

        for field in ("cook_time_rating", "difficulty_rating"):
            r = client.patch(f"/api/recipes/{recipe_id}/rating", json={field: None})
            assert r.status_code == 200
            assert r.json()[field] is None
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_tags_seeded_with_cocktail_subgroup(client):
    r = client.get("/api/tags")
    tags = r.json()
    cocktail_tags = {t["name"] for t in tags if t["subgroup"] == "cocktail"}
    assert cocktail_tags == {"Shaken", "Stirred", "Built", "Blended"}


def test_notes_patch_and_clear(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    assert r.json()["notes"] is None
    try:
        r = client.patch(f"/api/recipes/{recipe_id}/notes", json={"notes": "Worked great with less salt."})
        assert r.status_code == 200
        assert r.json()["notes"] == "Worked great with less salt."

        r = client.get(f"/api/recipes/{recipe_id}")
        assert r.json()["notes"] == "Worked great with less salt."

        r = client.patch(f"/api/recipes/{recipe_id}/notes", json={"notes": ""})
        assert r.json()["notes"] is None
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_export_only_includes_notes_when_requested_and_present(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        client.patch(f"/api/recipes/{recipe_id}/notes", json={"notes": "Secret ingredient: nutmeg."})

        r = client.get(f"/api/recipes/{recipe_id}/export.html")
        assert "Notes</h2>" not in r.text, "notes must not appear unless explicitly requested"

        r = client.get(f"/api/recipes/{recipe_id}/export.html", params={"include_notes": True})
        assert "Notes</h2>" in r.text
        assert "Secret ingredient: nutmeg." in r.text

        r = client.get(f"/api/recipes/{recipe_id}/export.pdf", params={"include_notes": True})
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_export_with_include_notes_but_no_notes_present(client, sample_recipe_payload):
    """include_notes=True on a recipe with no notes shouldn't render an
    empty Notes section."""
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        r = client.get(f"/api/recipes/{recipe_id}/export.html", params={"include_notes": True})
        assert "Notes</h2>" not in r.text
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_edit_round_trip_preserves_unedited_fields(client, sample_recipe_payload):
    """PUT /api/recipes/{id} is a full replace: the edit form must send
    back every field the handler touches, or ratings/favorite/image/raw
    text get silently wiped. This locks in that a realistic edit payload
    (title + ingredients changed, everything else round-tripped) leaves
    the untouched fields intact."""
    import os

    from app.database import UPLOADS_DIR

    from .conftest import TINY_PNG

    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]

    try:
        # Give it state the edit form doesn't expose as editable fields.
        up = client.post("/api/upload-image", files={"file": ("a.png", TINY_PNG, "image/png")})
        temp_name = up.json()["stored_file"]
        client.patch(f"/api/recipes/{recipe_id}/image", json={"image_path": temp_name})
        client.patch(f"/api/recipes/{recipe_id}/rating", json={
            "favorite": True, "tastiness_rating": 5,
            "cook_time_rating": "quick", "difficulty_rating": "hard",
            "actual_cook_time": "00:01:30",
        })
        client.patch(f"/api/recipes/{recipe_id}/notes", json={"notes": "keep me"})

        before = client.get(f"/api/recipes/{recipe_id}").json()
        assert before["favorite"] is True and before["image_path"] is not None

        # What the edit form sends: two fields changed, the rest round-tripped.
        payload = {
            "title": "Corrected Title",
            "source_type": before["source_type"],
            "source_url": before["source_url"],
            "servings": before["servings"],
            "prep_time": before["prep_time"],
            "cook_time": before["cook_time"],
            "total_time": before["total_time"],
            "image_path": before["image_path"],
            "raw_text": before["raw_text"],
            "ocr_confidence": before["ocr_confidence"],
            "favorite": before["favorite"],
            "tastiness_rating": before["tastiness_rating"],
            "cook_time_rating": before["cook_time_rating"],
            "difficulty_rating": before["difficulty_rating"],
            "actual_cook_time": before["actual_cook_time"],
            "ingredients": [{"raw_line": "2 cups corrected flour", "name": "corrected flour"}],
            "steps": ["Fixed step one", "Fixed step two"],
            "tags": [{"name": "Dinner", "category": "meal_type", "subgroup": None}],
        }
        r = client.put(f"/api/recipes/{recipe_id}", json=payload)
        assert r.status_code == 200
        after = r.json()

        # edits applied
        assert after["title"] == "Corrected Title"
        assert [i["raw_line"] for i in after["ingredients"]] == ["2 cups corrected flour"]
        assert [s["text"] for s in after["steps"]] == ["Fixed step one", "Fixed step two"]

        # everything else survived
        assert after["favorite"] is True, "favorite must not be wiped by an edit"
        assert after["tastiness_rating"] == 5
        assert after["cook_time_rating"] == "quick"
        assert after["difficulty_rating"] == "hard"
        assert after["actual_cook_time"] == "00:01:30"
        assert after["image_path"] == before["image_path"], "showcase image must survive an edit"
        assert os.path.isfile(os.path.join(UPLOADS_DIR, after["image_path"])), \
            "the image FILE must still exist -- a mismatched image_path would have deleted it"
        assert after["notes"] == "keep me", "notes are not part of PUT and must be untouched"
        assert after["source_type"] == before["source_type"]
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_incomplete_put_does_wipe_fields(client, sample_recipe_payload):
    """Documents the hazard the previous test guards against: PUT really
    is a full replace, so a naive edit form that omits fields clears
    them. If this ever stops being true the round-tripping in the
    frontend edit form could be simplified -- until then, don't."""
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        client.patch(f"/api/recipes/{recipe_id}/rating", json={"favorite": True, "tastiness_rating": 4})
        assert client.get(f"/api/recipes/{recipe_id}").json()["favorite"] is True

        # Minimal PUT -- omits favorite/ratings entirely.
        r = client.put(f"/api/recipes/{recipe_id}", json={
            "title": "Minimal", "source_type": "manual",
            "ingredients": [], "steps": [], "tags": [],
        })
        assert r.status_code == 200
        assert r.json()["favorite"] is False, "confirms PUT clears omitted fields"
        assert r.json()["tastiness_rating"] is None
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_sort_orders_and_always_puts_blanks_last(client, sample_recipe_payload):
    """Sorting, and the deliberate asymmetry in how blanks are handled.

    Unrated/untimed recipes sort last in BOTH directions. Most recipes have
    no rating, so an ascending sort that honoured nulls naturally would open
    with a wall of blanks. The trade is that asc and desc are not strict
    mirrors, which is easy to "fix" by accident later -- hence this test.
    """
    made = []
    try:
        for title, rating, diff in [
            ("ZZ sort probe alpha", 5, "hard"),
            ("AA sort probe beta", 2, "easy"),
            ("MM sort probe gamma", None, None),
        ]:
            r = client.post("/api/recipes", json=dict(sample_recipe_payload, title=title))
            rid = r.json()["id"]
            made.append(rid)
            if rating is not None:
                assert client.patch(f"/api/recipes/{rid}/rating", json={
                    "tastiness_rating": rating, "difficulty_rating": diff,
                }).status_code == 200

        def probe(sort, direction, key):
            rows = client.get(f"/api/recipes?sort={sort}&direction={direction}").json()
            rows = [r for r in rows if r["id"] in made]
            return [r[key] for r in rows]

        # Ratings descend and ascend correctly among the rated ones.
        desc = probe("rating", "desc", "tastiness_rating")
        assert [v for v in desc if v is not None] == [5, 2]
        asc = probe("rating", "asc", "tastiness_rating")
        assert [v for v in asc if v is not None] == [2, 5]

        # ...and the unrated one is last either way.
        for direction in ("asc", "desc"):
            vals = probe("rating", direction, "tastiness_rating")
            assert vals[-1] is None, f"blank must sort last when {direction}"

        # Difficulty is stored as text; it must order easy < medium < hard,
        # not alphabetically (which would put "hard" before "medium").
        assert [v for v in probe("difficulty", "asc", "difficulty_rating") if v] == ["easy", "hard"]
        assert [v for v in probe("difficulty", "desc", "difficulty_rating") if v] == ["hard", "easy"]

        # Title sorting is case-insensitive.
        titles = probe("title", "asc", "title")
        assert titles == sorted(titles, key=str.lower)

        # An unknown sort field is rejected rather than silently ignored.
        assert client.get("/api/recipes?sort=nonsense").status_code == 422
        assert client.get("/api/recipes?sort=title&direction=sideways").status_code == 422
    finally:
        for rid in made:
            client.delete(f"/api/recipes/{rid}")


BIG = "99999999999999999999"


H = {"X-Requested-With": "OpenThePantry"}


def _routes_with_int_path(app):
    from tests.conftest import app_routes
    for route in app_routes(app):
        path = getattr(route, "path", "")
        if re.search(r"\{\w+_id\}", path):
            for method in sorted(getattr(route, "methods", None) or ()):
                if method != "HEAD":
                    yield method, re.sub(r"\{\w+_id\}", BIG, path)


def _recipe(client, title, notes=None, **extra):
    body = {"title": title, "source_type": "manual",
            "ingredients": [{"raw_line": "2 cups flour"}], "steps": ["Mix."]}
    body.update(extra)
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    if notes is not None:
        assert client.patch(f"/api/recipes/{rid}/notes", json={"notes": notes}).status_code == 200
    return rid


def test_fit_recipe():
    from app import schemas
    fields, ings, steps = schemas.fit_recipe(
        {"title": "t" * 900, "source_url": "https://example.com/" + "a" * 3000, "servings": "4" * 999, "prep_time": None},
        [{"raw_line": "r" * 3000, "quantity": "1" * 200, "unit": None, "name": "n"}] * 600, ["s" * 30_000] * 700)
    assert len(fields["title"]) == 500 and fields["source_url"] is None and len(fields["servings"]) == schemas.SERVINGS_MAX
    assert len(ings) == 500 and len(ings[0]["raw_line"]) == 2000 and len(ings[0]["quantity"]) == 100
    assert len(steps) == 500 and len(steps[0]) == 20_000


def test_every_id_in_a_path_is_checked_on_the_main_port(client):
    found = list(_routes_with_int_path(client.app))
    assert len(found) >= 12
    for method, path in found:
        r = client.request(method, path, headers=H, json={})
        assert r.status_code == 422, (method, path, r.status_code)
    # In range, nothing changes: an unknown id is still a 404.
    assert client.get("/api/recipes/0").status_code == 404
    assert client.get("/api/recipes/-5").status_code == 404
    assert client.get(f"/api/recipes/{2**63 - 1}").status_code == 404


def test_query_and_body_integers_are_checked(client):
    assert client.get(f"/api/recipes?max_minutes={BIG}").status_code == 422
    assert client.get(f"/api/recipes?max_minutes={2**63 - 1}").status_code == 200
    r = client.post("/api/recipes/batch-delete", headers=H, json={"ids": [int(BIG)]})
    assert r.status_code == 422


@pytest.mark.parametrize("field,value", [
    ("title", "x" * 501),
    ("raw_text", "x" * 1_000_001),
    ("ingredients", [{"raw_line": "x" * 2001}]),
    ("steps", ["x" * 20_001]),
    ("steps", ["Mix."] * 501),
], ids=["title-501", "raw_text-1000001", "ingredient-2001", "step-20001", "steps-501"])
# Short ids: pytest -v prints each test's id, and the values themselves
# made one line a megabyte long, which stalled GitHub's live log view.
def test_recipe_fields_are_capped(client, sample_recipe_payload, field, value):
    r = client.post("/api/recipes", json={**sample_recipe_payload, field: value})
    assert r.status_code == 422


def test_json_bodies_are_capped(client):
    from app import main, routes
    big = b'{"ids": [' + b"1," * (main.MAX_JSON_BODY_BYTES // 2) + b'1]}'
    r = client.post("/api/recipes/batch-delete", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    # Without a Content-Length (chunked), counted as it arrives.
    def chunks():
        yield b'{"ids": ['
        for _ in range(40):
            yield b"1," * (1024 * 1024)
        yield b"1]}"
    r = client.post("/api/recipes/batch-delete", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    # Uploads aren't affected (they stream to disk with their own limits).
    assert client.post("/api/recipes/batch-delete", json={"ids": []}).status_code == 200


@pytest.mark.parametrize("path,body", [
    ("/api/recipes/batch-delete", {"ids": list(range(10_001))}),
    ("/api/ingest/url/batch", {"urls": ["https://example.com/" + "a" * 2000]}),
    ("/api/tags/suggest", {"title": "x", "ingredients": ["a" * 2001]}),
    ("/api/tags/auto-apply", {"selections": [{"id": 1, "tags": ["t" * 101]}]}),
])
def test_lists_and_their_items_are_bounded(client, path, body):
    assert client.post(path, json=body).status_code == 422


def test_an_invalid_suggested_tag_is_left_off_not_the_recipe(client, monkeypatch):
    from app import main, models, routes
    warned = []
    monkeypatch.setattr(main.log, "warning", lambda msg, *a, **k: warned.append(msg))
    from app.database import SessionLocal
    result = {"title": "Moules emailed", "ingredients": [{"raw_line": "1 kg mussels"}],
              "steps": ["Steam the mussels."], "raw_text": "1 kg mussels\nSteam the mussels.",
              "tags": [{"name": "Fish", "category": "Diet"}, {"name": "x" * 150, "category": "Diet"}]}
    with SessionLocal() as db:
        rid = routes.email._save_email_recipe(db, result, "[RECIPE]")
    try:
        with SessionLocal() as db:
            assert [t.name for t in db.get(models.Recipe, rid).tags] == ["Fish"]
        assert any("left off" in m for m in warned)
    finally:
        client.delete(f"/api/recipes/{rid}")
        client.delete(f"/api/recently-deleted/{rid}")


def test_list_says_which_recipes_have_notes(client):
    with_notes = _recipe(client, "Has Notes Card", notes="Use less salt.")
    blank_notes = _recipe(client, "Blank Notes Card", notes="   ")
    rows = {r["id"]: r for r in client.get("/api/recipes?q=Notes Card").json()}
    assert rows[with_notes]["has_notes"] is True
    assert rows[blank_notes]["has_notes"] is False
    assert "notes" not in rows[with_notes]  # the text itself stays out of the list


def test_tag_in_an_unknown_group_is_saved_under_custom(client):
    r = client.post("/api/recipes", json={
        "title": "Unknown Group Tag", "source_type": "manual", "steps": ["x"],
        "tags": [{"name": "Mystery", "category": "nonexistent-group", "subgroup": "x"}]})
    assert r.status_code == 200
    tag = next(t for t in r.json()["tags"] if t["name"] == "Mystery")
    assert tag["category"] == "custom" and tag["subgroup"] is None


def test_null_clears_the_cook_time_and_absent_leaves_it(client):
    rid = _recipe(client, "Cook Time Null")
    assert client.patch(f"/api/recipes/{rid}/rating", json={"actual_cook_time": "00:01:30"}).status_code == 200
    client.patch(f"/api/recipes/{rid}/rating", json={"favorite": True})
    assert client.get(f"/api/recipes/{rid}").json()["actual_cook_time"] == "00:01:30"
    client.patch(f"/api/recipes/{rid}/rating", json={"actual_cook_time": None})
    assert client.get(f"/api/recipes/{rid}").json()["actual_cook_time"] is None


def test_recipe_list_loads_tags_in_one_query(client):
    """list_recipes read each recipe's tags lazily: one extra SELECT per
    recipe in the list."""
    from sqlalchemy import event

    from app.database import engine

    for i in range(6):
        client.post("/api/recipes", json={"title": f"Tagged {i}", "source_type": "manual",
                                          "tags": [{"name": "Dinner", "category": "meal_type"}], "steps": ["x"]})
    statements = []
    listener = lambda conn, cursor, stmt, *a: statements.append(stmt)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        r = client.get("/api/recipes?sort=created&direction=desc")
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert r.status_code == 200 and len(r.json()) >= 6
    tag_selects = [s for s in statements if "FROM tags" in s or "JOIN tags" in s]
    assert len(tag_selects) <= 1, f"{len(tag_selects)} tag queries for one list"


def test_rate_limit_ignores_photos_and_static_files(client, monkeypatch):
    """Scrolling ~100 photo recipes used the whole 120/min budget on
    thumbnails, then /api/recipes got 429 and the list read "No recipes
    match"."""
    import app.main as m
    monkeypatch.setattr(m, "RATE_LIMIT_MAX_REQUESTS", 5)
    m._request_log.clear()
    for _ in range(20):
        client.get("/uploads/img-" + uuid.uuid4().hex + ".png")
    assert client.get("/api/recipes").status_code == 200
    m._request_log.clear()


def test_rate_limit_still_applies_to_the_api(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "RATE_LIMIT_MAX_REQUESTS", 3)
    m._request_log.clear()
    codes = [client.get("/api/tags").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    m._request_log.clear()


@pytest.mark.parametrize("value", ["00:99:99", "01:24:00", "00:00:60", "20000:00:00", "61:00:00"])
def test_out_of_range_cook_times_are_rejected(client, value):
    r = client.post("/api/recipes", json={"title": "T", "source_type": "manual", "ingredients": [],
                                         "steps": [], "tags": [], "actual_cook_time": value})
    assert r.status_code == 422
