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
    from .conftest import TINY_PNG
    from app.database import UPLOADS_DIR

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
