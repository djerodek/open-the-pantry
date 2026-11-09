"""Merge two recipes (0110): POST /api/recipes/merge."""
import os
import uuid

from .conftest import TINY_PNG


def _photo(client):
    return client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]


def _recipe(client, title, **extra):
    body = {"title": title, "source_type": "manual", "ingredients": [{"raw_line": "1 cup rice"}],
            "steps": ["Cook."], "tags": [], **extra}
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(client, *ids):
    for rid in ids:
        client.delete(f"/api/recipes/{rid}")
        client.delete(f"/api/recently-deleted/{rid}")


def test_merge_takes_each_part_from_the_chosen_recipe(client):
    sfx = uuid.uuid4().hex[:6]
    a = _recipe(client, f"Merge chili {sfx}", servings="4", source_url="https://example.com/a",
                image_path=_photo(client), tags=[{"name": f"MergeA{sfx}", "category": "custom"}],
                notes="Use dried beans.", tastiness_rating=3, raw_text="A original")
    b = _recipe(client, f"Merge chili con carne {sfx}", servings="6", source_url="https://example.com/b",
                image_path=_photo(client), ingredients=[{"raw_line": "500 g beef"}, {"raw_line": "1 onion"}],
                steps=["Brown.", "Simmer."], tags=[{"name": f"MergeB{sfx}", "category": "custom"}],
                notes="Better the next day.", tastiness_rating=5, favorite=True, raw_text="B original")
    client.patch(f"/api/recipes/{a['id']}/notes", json={"notes": "Use dried beans."})
    client.patch(f"/api/recipes/{b['id']}/notes", json={"notes": "Better the next day."})
    try:
        r = client.post("/api/recipes/merge", json={"keep_id": b["id"], "other_id": a["id"], "take": {
            "title": "other", "photo": "keep", "content": "keep", "details": "other", "source": "other",
            "ratings": "keep", "notes": "both"}})
        assert r.status_code == 200, r.text
        m = r.json()
        assert m["id"] == b["id"] and m["title"] == a["title"]
        assert [i["raw_line"] for i in m["ingredients"]] == ["500 g beef", "1 onion"]
        assert [s["text"] for s in m["steps"]] == ["Brown.", "Simmer."] and m["raw_text"] == "B original"
        assert m["servings"] == "4" and m["source_url"] == "https://example.com/a"
        assert m["tastiness_rating"] == 5 and m["favorite"] is True
        assert m["notes"] == "Better the next day.\n\nUse dried beans."
        assert sorted(t["name"] for t in m["tags"]) == sorted([f"MergeA{sfx}", f"MergeB{sfx}"])
        assert m["image_path"] == b["image_path"]
        # The other one waits in Recently deleted, unchanged, photo and all.
        assert client.get(f"/api/recipes/{a['id']}").status_code == 404
        assert a["id"] in [x["id"] for x in client.get("/api/recently-deleted").json()["recipes"]]
        assert client.get(f"/uploads/{a['image_path']}").status_code == 200
        client.post(f"/api/recently-deleted/{a['id']}/restore")
        back = client.get(f"/api/recipes/{a['id']}").json()
        assert back["title"] == a["title"] and back["servings"] == "4" and back["image_path"] == a["image_path"]
    finally:
        _cleanup(client, a["id"], b["id"])


def test_photo_from_the_other_recipe_is_copied(client):
    a = _recipe(client, f"Merge photo A {uuid.uuid4().hex[:6]}", image_path=_photo(client))
    b = _recipe(client, f"Merge photo B {uuid.uuid4().hex[:6]}", image_path=_photo(client))
    try:
        m = client.post("/api/recipes/merge", json={"keep_id": a["id"], "other_id": b["id"],
                                                     "take": {"photo": "other"}}).json()
        assert m["image_path"].startswith("merge-") and m["image_path"] != b["image_path"]
        assert client.get(f"/uploads/{m['image_path']}").status_code == 200
        assert client.get(f"/uploads/{b['image_path']}").status_code == 200       # the other's own, kept
        assert client.get(f"/uploads/{a['image_path']}").status_code == 404       # replaced
        assert m["notes"] is None
    finally:
        _cleanup(client, a["id"], b["id"])


def test_no_photo_and_content_from_the_other(client):
    a = _recipe(client, f"Merge none A {uuid.uuid4().hex[:6]}", image_path=_photo(client))
    b = _recipe(client, f"Merge none B {uuid.uuid4().hex[:6]}", ingredients=[{"raw_line": "2 eggs"}], steps=["Whisk."])
    try:
        m = client.post("/api/recipes/merge", json={"keep_id": a["id"], "other_id": b["id"],
                                                     "take": {"photo": "none", "content": "other"}}).json()
        assert m["image_path"] is None and [i["raw_line"] for i in m["ingredients"]] == ["2 eggs"]
        assert [x["id"] for x in client.get("/api/recipes", params={"q": "Whisk"}).json()].count(a["id"]) == 1
    finally:
        _cleanup(client, a["id"], b["id"])


def test_bad_merges(client):
    a = _recipe(client, f"Merge bad {uuid.uuid4().hex[:6]}")
    try:
        assert client.post("/api/recipes/merge", json={"keep_id": a["id"], "other_id": a["id"]}).status_code == 400
        assert client.post("/api/recipes/merge", json={"keep_id": a["id"], "other_id": 999_999_999}).status_code == 404
        assert client.post("/api/recipes/merge", json={"keep_id": a["id"], "other_id": 1,
                                                        "take": {"title": "both"}}).status_code == 422
    finally:
        _cleanup(client, a["id"])


def _rice_recipe(client, title, tags=(), photo=False):
    image = None
    if photo:
        image = client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual", "image_path": image,
                                          "ingredients": [{"raw_line": "1 cup rice"}], "steps": ["Cook."],
                                          "tags": [{"name": t, "category": "custom"} for t in tags]})
    assert r.status_code == 200, r.text
    return r.json()


H = {"X-Requested-With": "OpenThePantry"}


def test_a_merge_that_would_cut_notes_or_tags_is_refused(client):
    from app.routes.recipes import NOTES_MAX
    sfx = uuid.uuid4().hex[:6]
    a, b = _rice_recipe(client, f"Merge long A {sfx}"), _rice_recipe(client, f"Merge long B {sfx}")
    try:
        client.patch(f"/api/recipes/{a['id']}/notes", json={"notes": "a" * NOTES_MAX})
        client.patch(f"/api/recipes/{b['id']}/notes", json={"notes": "Keep me."})
        both = {"keep_id": a["id"], "other_id": b["id"], "take": {"notes": "both"}}
        r = client.post("/api/recipes/merge", json=both)
        assert r.status_code == 400 and "50,000 characters" in r.json()["detail"]
        fr = client.post("/api/recipes/merge", json=both, headers={"X-App-Lang": "fr"}).json()["detail"]
        assert fr.startswith("Les notes des deux recettes dépassent ensemble 50 000 caractères.")
        # Nothing changed, and the other recipe is still in the library.
        assert client.get(f"/api/recipes/{b['id']}").json()["notes"] == "Keep me."
        assert len(client.get(f"/api/recipes/{a['id']}").json()["notes"]) == NOTES_MAX
        # One recipe's notes: fine.
        assert client.post("/api/recipes/merge", json={**both, "take": {"notes": "keep"}}).status_code == 200
    finally:
        _cleanup(client, a["id"], b["id"])

    c = _rice_recipe(client, f"Merge tags C {sfx}", tags=[f"MT{sfx}-{i}" for i in range(30)])
    d = _rice_recipe(client, f"Merge tags D {sfx}", tags=[f"MT{sfx}-{i}" for i in range(20, 55)])
    g = _rice_recipe(client, f"Merge tags G {sfx}", tags=[f"MT{sfx}-{i}" for i in range(60, 80)])
    try:
        # 30 + 35, 10 shared: 55, over the 50 a recipe holds.
        r = client.post("/api/recipes/merge", json={"keep_id": c["id"], "other_id": d["id"]})
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "Together these recipes have 55 tags; a recipe can have at most 50. Remove some tags first."
        assert client.get(f"/api/recipes/{d['id']}").status_code == 200
        assert len(client.get(f"/api/recipes/{c['id']}").json()["tags"]) == 30
        # Exactly 50: merged, every tag kept.
        r = client.post("/api/recipes/merge", json={"keep_id": c["id"], "other_id": g["id"]})
        assert r.status_code == 200 and len(r.json()["tags"]) == 50
    finally:
        _cleanup(client, c["id"], d["id"], g["id"])


def test_a_refused_merge_leaves_no_photo_behind(client):
    from app.main import UPLOADS_DIR
    from app.routes.recipes import NOTES_MAX
    sfx = uuid.uuid4().hex[:6]
    keep = _rice_recipe(client, f"Leak keep {sfx}", tags=[f"LK{sfx}-{i}" for i in range(30)])
    other = _rice_recipe(client, f"Leak other {sfx}", tags=[f"LO{sfx}-{i}" for i in range(30)], photo=True)
    plain = _rice_recipe(client, f"Leak plain {sfx}", photo=True)
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


def test_a_failed_merge_photo_copy_leaves_nothing(client, monkeypatch):
    from app import main, routes
    photo = client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]
    a = client.post("/api/recipes", headers=H, json={"title": "Keep", "source_type": "manual"}).json()
    b = client.post("/api/recipes", headers=H, json={"title": "Other", "source_type": "manual",
                                                     "image_path": photo}).json()
    try:
        before = set(os.listdir(main.UPLOADS_DIR))

        def half_copy(src, dst):
            with open(dst, "wb") as f:
                f.write(b"partial")
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(routes.common.shutil, "copyfile", half_copy)
        r = client.post("/api/recipes/merge", headers=H, json={"keep_id": a["id"], "other_id": b["id"],
                                                              "take": {"photo": "other"}})
        assert r.status_code == 500 and "Nothing was changed" in r.json()["detail"]
        assert set(os.listdir(main.UPLOADS_DIR)) == before
        assert client.get(f"/api/recipes/{b['id']}").status_code == 200      # not merged
    finally:
        for rid in (a["id"], b["id"]):
            client.delete(f"/api/recipes/{rid}", headers=H)
            client.delete(f"/api/recently-deleted/{rid}", headers=H)
