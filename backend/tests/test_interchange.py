"""Recipes in standard formats (backlog item 2, app/interchange.py):
schema.org JSON-LD export (library zip, one recipe) and import of schema.org
JSON, zips of it and Paprika .paprikarecipes."""
import base64
import gzip
import io
import json
import os
import uuid
import zipfile

import pytest

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _upload_photo(client):
    r = client.post("/api/upload-image", headers=H, files={"file": ("p.png", TINY_PNG, "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["stored_file"]


def _import(client, name, data):
    return client.post("/api/import/recipes", headers=H, files={"file": (name, data, "application/octet-stream")})


def _ids(body):
    return [s["recipe_id"] for s in body.get("succeeded", [])]


def test_iso_durations():
    from app.interchange import iso_duration
    cases = {"PT20M": "PT20M", "pt1h": "PT1H", "45 minutes": "PT45M", "1 hr 20 min": "PT1H20M",
             "1 h 30": "PT1H30M", "2 heures": "PT2H", "90": "PT90M", "1 day": "PT24H",
             "1,5 h": "PT1H30M", "overnight": None, "": None, None: None, "P": None}
    for text, want in cases.items():
        assert iso_duration(text) == want, text


def test_one_recipe_as_schema_org(client):
    sfx = uuid.uuid4().hex[:6]
    r = client.post("/api/recipes", headers=H, json={
        "title": f"JSON Stew {sfx}", "source_type": "url", "source_url": f"https://example.com/json-stew-{sfx}",
        "servings": "4", "prep_time": "15 min", "cook_time": "PT1H", "total_time": "overnight",
        "ingredients": [{"raw_line": "500 g beef"}, {"raw_line": "Sauce:"}, {"raw_line": "2 tbsp flour"}],
        "steps": ["Brown the beef.", "Sauce:", "Whisk.", "Simmer."],
        "tags": [{"name": "Dinner", "category": "meal_type"}, {"name": "Beef", "category": "main_ingredient"}],
        "tastiness_rating": 4, "favorite": True, "actual_cook_time": "00:01:30"})
    rid = r.json()["id"]
    try:
        client.patch(f"/api/recipes/{rid}/notes", headers=H, json={"notes": "Less salt."})
        res = client.get(f"/api/recipes/{rid}/export.json")
        assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
        assert res.headers["content-type"].startswith("application/ld+json")
        doc = res.json()
        assert doc["@context"] == "https://schema.org" and doc["@type"] == "Recipe"
        assert doc["name"] == f"JSON Stew {sfx}" and doc["recipeYield"] == "4"
        assert doc["recipeIngredient"] == ["500 g beef", "Sauce:", "2 tbsp flour"]
        assert doc["recipeInstructions"] == [
            {"@type": "HowToStep", "text": "Brown the beef."},
            {"@type": "HowToSection", "name": "Sauce", "itemListElement": [
                {"@type": "HowToStep", "text": "Whisk."}, {"@type": "HowToStep", "text": "Simmer."}]}]
        assert doc["prepTime"] == "PT15M" and doc["cookTime"] == "PT1H" and "totalTime" not in doc
        assert doc["recipeCategory"] == ["Dinner"] and doc["url"].endswith(sfx)
        assert doc["description"] == "Less salt."
        ext = doc["x-open-the-pantry"]
        assert ext["tastiness_rating"] == 4 and ext["favorite"] is True and ext["actual_cook_time"] == "00:01:30"
        assert ext["total_time"] == "overnight"
        assert {"name": "Beef", "category": "main_ingredient", "subgroup": None} in ext["tags"]
        assert client.get("/api/recipes/999999999/export.json").status_code == 404
    finally:
        client.delete(f"/api/recipes/{rid}", headers=H)


def test_library_export_round_trips(client):
    sfx = uuid.uuid4().hex[:6]
    photo = _upload_photo(client)
    r = client.post("/api/recipes", headers=H, json={
        "title": f"Round Trip Pie {sfx}", "source_type": "pdf", "image_path": photo, "servings": "8",
        "ingredients": [{"raw_line": "1 crust"}], "steps": ["Bake."],
        "tags": [{"name": f"RT{sfx}", "category": "custom"}], "tastiness_rating": 5,
        "difficulty_rating": "hard", "cook_time_rating": "long"})
    rid = r.json()["id"]
    client.patch(f"/api/recipes/{rid}/notes", headers=H, json={"notes": "Family favourite."})
    res = client.get("/api/backup/recipes-json.zip")
    assert res.status_code == 200
    data = res.content
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        assert "README.txt" in names and f"images/{photo}" in names
        mine = [n for n in names if n.startswith(f"recipes/{rid}-")]
        assert len(mine) == 1
        assert json.loads(zf.read(mine[0]))["image"] == f"images/{photo}"
    client.delete(f"/api/recipes/{rid}", headers=H)
    # Import only this recipe back (the rest of the shared test library is
    # still there and would come back as duplicates).
    with zipfile.ZipFile(io.BytesIO(data)) as zf, io.BytesIO() as out:
        with zipfile.ZipFile(out, "w") as small:
            small.writestr(mine[0], zf.read(mine[0]))
            small.writestr(f"images/{photo}", zf.read(f"images/{photo}"))
        small_zip = out.getvalue()
    body = _import(client, "export.zip", small_zip).json()
    try:
        assert body["failed"] == [] and len(body["succeeded"]) == 1
        new = client.get(f"/api/recipes/{_ids(body)[0]}").json()
        assert new["title"] == f"Round Trip Pie {sfx}" and new["servings"] == "8" and new["source_type"] == "pdf"
        assert new["notes"] == "Family favourite." and new["tastiness_rating"] == 5
        assert new["difficulty_rating"] == "hard" and new["cook_time_rating"] == "long"
        assert [t["name"] for t in new["tags"]] == [f"RT{sfx}"]
        assert new["image_path"] and new["image_path"] != photo
        assert client.get(f"/uploads/{new['image_path']}").status_code == 200
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": _ids(body)})


def _paprika(recipes):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for r in recipes:
            zf.writestr(f"{r['name']}.paprikarecipe", gzip.compress(json.dumps(r).encode()))
    return out.getvalue()


def test_paprika_import(client):
    sfx = uuid.uuid4().hex[:6]
    data = _paprika([{
        "name": f"Paprika Curry {sfx}", "ingredients": "1 onion\n2 tbsp curry paste\n\n400 ml coconut milk",
        "directions": "1. Fry the onion.\n2. Add the paste.\n\nSimmer 20 minutes.", "servings": "4 servings",
        "prep_time": "10 min", "cook_time": "25 min", "total_time": "", "rating": 4, "on_favorites": 1,
        "difficulty": "Easy", "categories": [f"Weeknight{sfx}", "dinner"], "notes": "Add lime.",
        "source": "Example", "source_url": f"https://example.com/paprika-curry-{sfx}",
        "photo_data": base64.b64encode(TINY_PNG).decode(), "uid": "ABC", "hash": "x"},
        {"name": f"Paprika Broken {sfx}", "ingredients": "x" * 3000, "directions": "Cook."}])
    body = _import(client, "My Recipes.paprikarecipes", data).json()
    try:
        assert len(body["succeeded"]) == 1
        assert [f["title"] for f in body["failed"]] == [f"Paprika Broken {sfx}"]   # 3000-character line: refused, not a 500
        r = client.get(f"/api/recipes/{_ids(body)[0]}").json()
        assert [i["raw_line"] for i in r["ingredients"]] == ["1 onion", "2 tbsp curry paste", "400 ml coconut milk"]
        assert [s["text"] for s in r["steps"]] == ["Fry the onion.", "Add the paste.", "Simmer 20 minutes."]
        assert (r["servings"], r["prep_time"], r["cook_time"], r["total_time"]) == ("4 servings", "10 min", "25 min", None)
        assert r["tastiness_rating"] == 4 and r["favorite"] is True and r["difficulty_rating"] == "easy"
        assert r["notes"] == "Add lime." and r["source_type"] == "import" and r["image_path"]
        names = {(t["name"], t["category"]) for t in r["tags"]}
        assert (f"Weeknight{sfx}", "custom") in names       # the person's own category, kept
        assert ("Dinner", "meal_type") in names              # "dinner" matched to the built-in tag
        # The same file again: skipped by its link.
        again = _import(client, "My Recipes.paprikarecipes", data).json()
        assert [k["recipe_id"] for k in again["skipped"]] == _ids(body)
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": _ids(body)})


def test_foreign_schema_org_keywords_only_match_known_tags(client):
    sfx = uuid.uuid4().hex[:6]
    doc = {"@context": "https://schema.org", "@graph": [{"@type": "WebPage"}, {
        "@type": "Recipe", "name": f"Site Soup {sfx}", "recipeIngredient": ["1 l stock", "2 carrots"],
        "recipeInstructions": "Simmer.\nBlend.", "recipeYield": ["4", "4 bowls"],
        "keywords": f"easy, weeknight, SEO-spam-{sfx}, Dinner", "aggregateRating": {"ratingValue": 4.8, "ratingCount": 900},
        "image": "https://example.com/soup.jpg"}]}
    body = _import(client, "soup.json", json.dumps(doc).encode()).json()
    try:
        r = client.get(f"/api/recipes/{_ids(body)[0]}").json()
        tags = {t["name"] for t in r["tags"]}
        assert "Dinner" in tags and f"SEO-spam-{sfx}" not in tags
        assert r["tastiness_rating"] is None            # a site's average isn't your rating
        assert r["image_path"] is None                  # web photos aren't fetched on a file import
        assert [s["text"] for s in r["steps"]] == ["Simmer.", "Blend."] and r["servings"] == "4"
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": _ids(body)})


def test_bad_files(client, monkeypatch):
    from app import interchange
    assert _import(client, "x.txt", b"hello").status_code == 422
    assert _import(client, "x.json", b'{"@type": "Person"}').status_code == 422
    assert _import(client, "x.zip", b"PK\x03\x04 not really").status_code == 422
    assert _import(client, "x.json", b"").status_code == 400
    # A zip with a bad entry and a traversal-shaped image reference.
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("recipes/bad.json", b"{not json")
        zf.writestr("recipes/ok.json", json.dumps({"@type": "Recipe", "name": "Zip Ok " + uuid.uuid4().hex[:6],
                                                    "recipeIngredient": ["1 egg"], "recipeInstructions": ["Boil."],
                                                    "image": "../../../etc/passwd"}))
        zf.writestr("../evil.json", b"{}")
    body = _import(client, "mixed.zip", out.getvalue()).json()
    try:
        assert len(body["succeeded"]) == 1
        assert {f["origin"] for f in body["failed"]} == {"bad.json", "evil.json"}
        assert client.get(f"/api/recipes/{_ids(body)[0]}").json()["image_path"] is None
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": _ids(body)})
    monkeypatch.setattr(interchange, "MAX_ENTRIES", 2)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for i in range(3):
            zf.writestr(f"{i}.json", b"{}")
    res = _import(client, "many.zip", out.getvalue())
    assert res.status_code == 422 and "limit is 2" in res.json()["detail"]
