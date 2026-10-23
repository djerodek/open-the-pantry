"""Settings -> Library check (0111): GET /api/library-check."""
import os
import uuid

from .conftest import TINY_PNG


def _recipe(client, title, **extra):
    body = {"title": title, "source_type": "manual", "ingredients": [{"raw_line": "1 cup rice"}],
            "steps": ["Cook."], "tags": [{"name": "Dinner", "category": "meal_type"}], **extra}
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(check, key):
    return {x["id"] for x in check["checks"][key]}


def test_library_check_lists_what_is_missing_and_lookalikes(client):
    from app.database import UPLOADS_DIR
    sfx = uuid.uuid4().hex[:6]
    photo = client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]
    full = _recipe(client, f"Check full {sfx}", image_path=photo)
    bare = _recipe(client, f"Check bare {sfx}", ingredients=[], steps=[], tags=[])
    lost = _recipe(client, f"Check lost {sfx}", image_path=client.post(
        "/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"])
    os.remove(os.path.join(UPLOADS_DIR, lost["image_path"]))
    twin_a = _recipe(client, f"Check Twin {sfx}", source_url=f"https://example.com/{sfx}")
    twin_b = _recipe(client, f"check twin! {sfx}")                                   # same title, ignoring case and punctuation
    twin_c = _recipe(client, f"Check other {sfx}", source_url=f"https://www.example.com/{sfx}/?utm_source=x")  # same link
    gone = _recipe(client, f"Check bare {sfx}", ingredients=[], steps=[])
    client.delete(f"/api/recipes/{gone['id']}")                                      # Recently deleted: not counted
    try:
        c = client.get("/api/library-check").json()
        assert bare["id"] in _ids(c, "no_ingredients") and bare["id"] in _ids(c, "no_steps")
        assert bare["id"] in _ids(c, "no_tags") and bare["id"] in _ids(c, "no_photo")
        assert full["id"] not in _ids(c, "no_ingredients") | _ids(c, "no_steps") | _ids(c, "no_tags") | _ids(c, "no_photo")
        assert lost["id"] in _ids(c, "photo_missing") and lost["id"] not in _ids(c, "no_photo")
        assert gone["id"] not in _ids(c, "no_ingredients")
        sets = [{x["id"] for x in s} for s in c["duplicates"]]
        assert {twin_a["id"], twin_b["id"], twin_c["id"]} in sets
        assert not any(gone["id"] in s or bare["id"] in s for s in sets)
        assert c["total"] == len(client.get("/api/recipes").json())
    finally:
        for r in (full, bare, lost, twin_a, twin_b, twin_c, gone):
            client.delete(f"/api/recipes/{r['id']}")
            client.delete(f"/api/recently-deleted/{r['id']}")
