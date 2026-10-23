"""Select mode -> Tags: POST /api/recipes/batch-tags (0108)."""


def _recipe(client, title, tags=()):
    body = {"title": title, "source_type": "manual", "ingredients": [{"raw_line": "1 cup flour"}],
            "steps": ["Mix."], "tags": [{"name": n, "category": c} for n, c in tags]}
    r = client.post("/api/recipes", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["id"]


def _tag_id(client, name, category):
    return next(t["id"] for t in client.get("/api/tags").json() if t["name"] == name and t["category"] == category)


def _tags(client, rid):
    return sorted(t["name"] for t in client.get(f"/api/recipes/{rid}").json()["tags"])


def test_add_and_remove_a_tag_on_several_recipes(client):
    a = _recipe(client, "BatchTag A", [("BatchTagSoup", "custom")])
    b = _recipe(client, "BatchTag B")
    c = _recipe(client, "BatchTag C", [("BatchTagOther", "custom")])
    tag = _tag_id(client, "BatchTagSoup", "custom")
    try:
        r = client.post("/api/recipes/batch-tags", json={"ids": [a, b, c, b, 999_999_999], "tag_id": tag, "action": "add"})
        assert r.status_code == 200
        assert r.json() == {"changed": [b, c], "unchanged": [a], "full": [], "missing": [999_999_999]}
        assert _tags(client, b) == ["BatchTagSoup"] and _tags(client, c) == ["BatchTagOther", "BatchTagSoup"]
        # Search finds them by the new tag (tags_text kept in step).
        found = {x["id"] for x in client.get("/api/recipes", params={"q": "BatchTagSoup"}).json()}
        assert {a, b, c} <= found

        r = client.post("/api/recipes/batch-tags", json={"ids": [a, b], "tag_id": tag, "action": "remove"})
        assert r.json()["changed"] == [a, b]
        assert _tags(client, a) == [] and _tags(client, b) == [] and _tags(client, c) == ["BatchTagOther", "BatchTagSoup"]
        r = client.post("/api/recipes/batch-tags", json={"ids": [a], "tag_id": tag, "action": "remove"})
        assert r.json()["unchanged"] == [a]
        found = {x["id"] for x in client.get("/api/recipes", params={"q": "BatchTagSoup"}).json()}
        assert a not in found and c in found
    finally:
        for rid in (a, b, c):
            client.delete(f"/api/recipes/{rid}")


def test_a_recipe_with_50_tags_is_left_alone(client):
    rid = _recipe(client, "BatchTag Full", [(f"BatchFull{i}", "custom") for i in range(50)])
    other = _recipe(client, "BatchTag Has Room", [("BatchTagExtra", "custom")])
    tag = _tag_id(client, "BatchTagExtra", "custom")
    try:
        r = client.post("/api/recipes/batch-tags", json={"ids": [rid, other], "tag_id": tag, "action": "add"})
        assert r.json()["full"] == [rid] and r.json()["unchanged"] == [other]
        assert len(_tags(client, rid)) == 50
    finally:
        client.delete(f"/api/recipes/{rid}")
        client.delete(f"/api/recipes/{other}")


def test_bad_requests(client):
    rid = _recipe(client, "BatchTag Bad")
    try:
        assert client.post("/api/recipes/batch-tags", json={"ids": [rid], "tag_id": 999_999_999, "action": "add"}).status_code == 404
        assert client.post("/api/recipes/batch-tags", json={"ids": [], "tag_id": 1, "action": "add"}).status_code == 422
        assert client.post("/api/recipes/batch-tags", json={"ids": [rid], "tag_id": 1, "action": "toggle"}).status_code == 422
        assert client.post("/api/recipes/batch-tags", json={"ids": [2**70], "tag_id": 1, "action": "add"}).status_code == 422
    finally:
        client.delete(f"/api/recipes/{rid}")
