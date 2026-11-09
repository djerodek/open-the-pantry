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
from datetime import datetime, timedelta, timezone

import pytest

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


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


def _zip(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return out.getvalue()


def _zip_stored(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return out.getvalue()


def test_notes_and_dates_survive_a_json_round_trip(client):
    rid = client.post("/api/recipes", headers=H, json={
        "title": f"Round trip {uuid.uuid4().hex[:6]}", "source_type": "manual",
        "ingredients": [{"raw_line": "1 egg"}], "steps": ["Boil."]}).json()["id"]
    assert client.patch(f"/api/recipes/{rid}/notes", headers=H, json={"notes": "n" * 30_000}).status_code == 200
    from app import models
    from app.database import SessionLocal
    with SessionLocal() as db:
        db.get(models.Recipe, rid).created_at = datetime(2021, 5, 4, 12, 0, tzinfo=timezone.utc)
        db.commit()
    data = client.get("/api/backup/recipes-json.zip").content
    client.delete(f"/api/recipes/{rid}", headers=H)
    client.delete(f"/api/recently-deleted/{rid}", headers=H)
    r = client.post("/api/import/recipes", headers=H, files={"file": ("r.zip", data, "application/zip")}).json()
    new = next(s["recipe_id"] for s in r["succeeded"] if s["title"].startswith("Round trip"))
    others = [s["recipe_id"] for s in r["succeeded"] if s["recipe_id"] != new]
    try:
        got = client.get(f"/api/recipes/{new}").json()
        assert len(got["notes"]) == 30_000
        with SessionLocal() as db:
            assert db.get(models.Recipe, new).created_at.date().isoformat() == "2021-05-04"
    finally:
        for i in [new] + others:
            client.delete(f"/api/recipes/{i}", headers=H)
            client.delete(f"/api/recently-deleted/{i}", headers=H)


def test_dates_read_on_import():
    from app.interchange import _date
    assert _date("2024-03-01 12:00:00").year == 2024              # Paprika's form
    assert _date("2024-03-01T12:00:00+02:00").utcoffset() == timedelta(hours=2)
    for bad in (None, "", "yesterday", "1850-01-01", (datetime.now(timezone.utc) + timedelta(days=9)).isoformat(), 5):
        assert _date(bad) is None, bad


def test_one_photo_named_by_many_recipes_is_read_within_the_budget(tmp_path, monkeypatch):
    from app import interchange
    big = TINY_PNG + b"\0" * 400_000
    recipes = [{"@type": "Recipe", "name": f"R{i}", "image": "images/p.png",
                "recipeIngredient": ["1 egg"], "recipeInstructions": ["Boil."]} for i in range(30)]
    path = tmp_path / "many.zip"
    path.write_bytes(_zip({"recipes/all.json": json.dumps(recipes), "images/p.png": big}))
    monkeypatch.setattr(interchange, "UNPACK_BUDGET", 5 * len(big))
    with interchange.opened(str(path), "many.zip") as (found, problems):
        assert len(found) == 30 and all(r.image_bytes is None for r in found)   # nothing read yet
        photos = [r.photo() for r in found]
    assert 1 <= sum(1 for p in photos if p) < 5
    assert any("of unpacking" in p["error"] for p in problems)


def test_paprika_gzip_bombs_stop_at_the_budget(tmp_path, monkeypatch):
    from app import interchange
    bomb = gzip.compress(json.dumps({"name": "X", "ingredients": "1 egg", "directions": "Boil.",
                                     "notes": "a" * 2_000_000}).encode())
    assert len(bomb) < 20_000
    path = tmp_path / "bombs.paprikarecipes"
    path.write_bytes(_zip({f"r{i}.paprikarecipe": bomb for i in range(50)}))
    monkeypatch.setattr(interchange, "UNPACK_BUDGET", 10 * 2_000_000)
    with interchange.opened(str(path), "bombs.paprikarecipes") as (found, problems):
        assert len(found) < 12 and any("of unpacking" in p["error"] for p in problems)


def test_paprika_photos_are_read_when_saved(client):
    data = _zip({"p.paprikarecipe": gzip.compress(json.dumps({
        "name": "R10 Paprika photo", "ingredients": "1 egg", "directions": "Boil.",
        "photo_data": base64.b64encode(TINY_PNG).decode()}).encode())})
    body = client.post("/api/import/recipes", headers=H,
                       files={"file": ("x.paprikarecipes", data, "application/zip")}).json()
    rid = body["succeeded"][0]["recipe_id"]
    try:
        assert client.get(f"/uploads/{client.get(f'/api/recipes/{rid}').json()['image_path']}").status_code == 200
    finally:
        client.delete(f"/api/recipes/{rid}", headers=H)


def test_a_big_json_file_is_read_only_up_to_the_limit(tmp_path, monkeypatch):
    from app import interchange
    monkeypatch.setattr(interchange, "MAX_RECIPES", 3)
    calls = []
    real = interchange.from_jsonld
    monkeypatch.setattr(interchange, "from_jsonld", lambda *a, **k: calls.append(1) or real(*a, **k))
    many = [{"@type": "Recipe", "name": f"R{i}", "recipeIngredient": ["1 egg"], "recipeInstructions": ["Boil."]}
            for i in range(50)]
    path = tmp_path / "big.zip"
    path.write_bytes(_zip_stored({"recipes/a.json": json.dumps(many)}))
    recipes, problems = interchange.read_file(str(path), "big.zip")
    assert len(recipes) == 3 and len(calls) == 3
    assert any("More than 3 recipes" in p["error"] for p in problems)
    assert len(interchange._recipe_items(many, 4)) == 4
    # A plain .json file the same way.
    path = tmp_path / "big.json"
    path.write_text(json.dumps(many))
    calls.clear()
    recipes, problems = interchange.read_file(str(path), "big.json")
    assert len(recipes) == 3 and len(calls) == 3 and problems


def test_malformed_import_files_are_refused_not_500(client):
    import gzip
    deep = "[" * 10_000 + "]" * 10_000
    r = client.post("/api/import/recipes", headers=H, files={"file": ("deep.json", deep.encode(), "application/json")})
    assert r.status_code == 422
    paprika = _zip_stored({"x.paprikarecipe": gzip.compress(json.dumps({
        "name": "GPT difficulty number", "ingredients": "1 egg", "directions": "Boil.", "difficulty": 3,
        "categories": "not a list", "rating": "five"}).encode())})
    body = client.post("/api/import/recipes", headers=H,
                       files={"file": ("p.paprikarecipes", paprika, "application/zip")}).json()
    try:
        assert len(body["succeeded"]) == 1 and body["failed"] == []
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [s["recipe_id"] for s in body["succeeded"]]})


def test_one_bad_record_is_that_records_failure(tmp_path, monkeypatch):
    from app import interchange
    real = interchange.from_jsonld

    def picky(item, *a, **k):
        if item.get("name") == "bad":
            raise TypeError("unexpected field")
        return real(item, *a, **k)
    monkeypatch.setattr(interchange, "from_jsonld", picky)
    path = tmp_path / "two.json"
    path.write_text(json.dumps([{"@type": "Recipe", "name": "bad"}, {"@type": "Recipe", "name": "good"}]))
    recipes, problems = interchange.read_file(str(path), "two.json")
    assert [r.title for r in recipes] == ["good"] and len(problems) == 1
