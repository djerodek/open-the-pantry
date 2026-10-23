"""Duplicate a recipe (owner's request): a full copy with its own photo,
titled "(copy)", without the favourite or "To review" marks."""
import uuid

import pytest

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_a_copy_has_everything_and_its_own_photo(client):
    from app import models
    from app.database import SessionLocal
    sfx = uuid.uuid4().hex[:6]
    photo = client.post("/api/upload-image", headers=H, files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]
    src = client.post("/api/recipes", headers=H, json={
        "title": f"Dup Stew {sfx}", "source_type": "pdf", "image_path": photo, "servings": "4",
        "prep_time": "10 min", "cook_time": "1 h", "raw_text": "Dup Stew\n500 g beef",
        "ingredients": [{"raw_line": "500 g beef"}, {"raw_line": "Sauce:"}, {"raw_line": "2 tbsp flour"}],
        "steps": ["Brown.", "Simmer."], "tags": [{"name": "Beef", "category": "main_ingredient"},
                                               {"name": f"Mine{sfx}", "category": "custom"}],
        "favorite": True, "tastiness_rating": 4, "difficulty_rating": "hard", "actual_cook_time": "00:01:30"}).json()
    client.patch(f"/api/recipes/{src['id']}/notes", headers=H, json={"notes": "Less salt."})
    db = SessionLocal()
    db.get(models.Recipe, src["id"]).needs_review = True
    db.commit()
    db.close()
    r = client.post(f"/api/recipes/{src['id']}/duplicate", headers=H)
    assert r.status_code == 200
    copy = r.json()
    try:
        assert copy["id"] != src["id"] and copy["title"] == f"Dup Stew {sfx} (copy)"
        for k in ("source_type", "servings", "prep_time", "cook_time", "raw_text", "tastiness_rating",
                  "difficulty_rating", "actual_cook_time"):
            assert copy[k] == src[k], k
        assert copy["notes"] == "Less salt."
        assert [i["raw_line"] for i in copy["ingredients"]] == ["500 g beef", "Sauce:", "2 tbsp flour"]
        assert copy["ingredients"][0]["quantity"] == src["ingredients"][0]["quantity"]
        assert [s["text"] for s in copy["steps"]] == ["Brown.", "Simmer."]
        assert {(t["name"], t["category"]) for t in copy["tags"]} == {(t["name"], t["category"]) for t in src["tags"]}
        assert copy["favorite"] is False and copy["needs_review"] is False
        assert copy["image_path"] and copy["image_path"] != photo
        # Found by search like any recipe (tags and content indexed).
        assert copy["id"] in [x["id"] for x in client.get("/api/recipes", params={"q": f"Mine{sfx}"}).json()]
        # Deleting the original leaves the copy's photo.
        client.delete(f"/api/recipes/{src['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{src['id']}", headers=H)     # for good (0109)
        assert client.get(f"/uploads/{photo}").status_code == 404
        assert client.get(f"/uploads/{copy['image_path']}").status_code == 200
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [src["id"], copy["id"]]})


def test_copy_title_in_french_and_unknown_recipe(client):
    sfx = uuid.uuid4().hex[:6]
    src = client.post("/api/recipes", headers=H, json={"title": f"Tarte {sfx}", "source_type": "manual",
                                                        "ingredients": [{"raw_line": "1 oeuf"}], "steps": ["Cuire."],
                                                        "tags": []}).json()
    copy = client.post(f"/api/recipes/{src['id']}/duplicate", headers={**H, "X-App-Lang": "fr"}).json()
    try:
        assert copy["title"] == f"Tarte {sfx} (copie)" and copy["image_path"] is None
        assert client.post("/api/recipes/999999999/duplicate", headers=H).status_code == 404
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [src["id"], copy["id"]]})
