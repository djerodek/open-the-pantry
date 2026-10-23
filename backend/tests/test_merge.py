"""Merge two recipes (0110): POST /api/recipes/merge."""
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
