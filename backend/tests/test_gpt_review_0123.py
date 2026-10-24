"""GPT review of 0123: a refused merge left a copy of the other recipe's
photo in uploads/ (the copy was made before the notes and tag limits were
checked). The sweep removed it after a day; now it isn't made."""
import os
import uuid

from .conftest import TINY_PNG


def _recipe(client, title, tags=(), photo=False):
    image = None
    if photo:
        image = client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual", "image_path": image,
                                          "ingredients": [{"raw_line": "1 cup rice"}], "steps": ["Cook."],
                                          "tags": [{"name": t, "category": "custom"} for t in tags]})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_refused_merge_leaves_no_photo_behind(client):
    from app.main import NOTES_MAX, UPLOADS_DIR
    sfx = uuid.uuid4().hex[:6]
    keep = _recipe(client, f"Leak keep {sfx}", tags=[f"LK{sfx}-{i}" for i in range(30)])
    other = _recipe(client, f"Leak other {sfx}", tags=[f"LO{sfx}-{i}" for i in range(30)], photo=True)
    plain = _recipe(client, f"Leak plain {sfx}", photo=True)
    try:
        before = set(os.listdir(UPLOADS_DIR))
        # Over the tag limit, the other's photo chosen.
        r = client.post("/api/recipes/merge", json={"keep_id": keep["id"], "other_id": other["id"],
                                                    "take": {"photo": "other"}})
        assert r.status_code == 400 and "tags" in r.json()["detail"]
        assert set(os.listdir(UPLOADS_DIR)) == before
        # Over the notes limit, the same.
        client.patch(f"/api/recipes/{keep['id']}/notes", json={"notes": "a" * NOTES_MAX})
        client.patch(f"/api/recipes/{plain['id']}/notes", json={"notes": "b"})
        r = client.post("/api/recipes/merge", json={"keep_id": keep["id"], "other_id": plain["id"],
                                                    "take": {"photo": "other", "notes": "both"}})
        assert r.status_code == 400 and "notes" in r.json()["detail"]
        assert set(os.listdir(UPLOADS_DIR)) == before
        # Allowed: the copy is made, and is the kept recipe's photo.
        r = client.post("/api/recipes/merge", json={"keep_id": keep["id"], "other_id": plain["id"],
                                                    "take": {"photo": "other", "notes": "keep"}})
        assert r.status_code == 200, r.text
        assert set(os.listdir(UPLOADS_DIR)) - before == {r.json()["image_path"]}
    finally:
        for rid in (keep["id"], other["id"], plain["id"]):
            client.delete(f"/api/recipes/{rid}")
            client.delete(f"/api/recently-deleted/{rid}")
