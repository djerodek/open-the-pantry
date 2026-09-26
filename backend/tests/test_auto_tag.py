"""Settings -> "Add suggested tags to all recipes" (POST /api/tags/auto-apply).

app.* is imported inside tests only; see the note in test_backup.py.
"""


def _make(client, title, ingredients, tags=()):
    r = client.post("/api/recipes", json={
        "title": title, "source_type": "manual", "steps": ["Simmer gently."],
        "ingredients": [{"raw_line": i} for i in ingredients],
        "tags": [{"name": n, "category": c} for n, c in tags],
    })
    assert r.status_code == 200
    return r.json()["id"]


def _tags(client, rid):
    return {(t["name"], t["category"]) for t in client.get(f"/api/recipes/{rid}").json()["tags"]}


def test_adds_missing_tags_without_duplicates_and_keeps_existing(client):
    rid = _make(client, "Auto Clam Linguine", ["1 lb clams", "linguine", "garlic"],
                tags=[("Vegetarian", "main_ingredient"), ("Weeknight", "custom")])
    before = _tags(client, rid)
    r = client.post("/api/tags/auto-apply").json()
    after = _tags(client, rid)
    assert ("Fish", "main_ingredient") in after and ("Stovetop", "cooking_style") in after
    assert before <= after                       # nothing removed, custom tag kept
    assert any(c["id"] == rid for c in r["changes"])

    again = client.post("/api/tags/auto-apply").json()
    assert all(c["id"] != rid for c in again["changes"])     # second run adds nothing here
    assert _tags(client, rid) == after
    names = [t["name"] for t in client.get(f"/api/recipes/{rid}").json()["tags"]]
    assert len(names) == len(set(names))


def test_dry_run_changes_nothing(client):
    rid = _make(client, "Auto Dry Salmon", ["2 salmon fillets", "lemon"])
    before = _tags(client, rid)
    r = client.post("/api/tags/auto-apply?dry_run=true").json()
    assert r["dry_run"] is True and any(c["id"] == rid and "Fish" in c["added"] for c in r["changes"])
    assert _tags(client, rid) == before


def test_vegetarian_not_added_next_to_a_meat_tag(client):
    """A hand-added Beef tag on a recipe whose text names no cut: the tagger
    alone would call it Vegetarian."""
    rid = _make(client, "Auto Sunday Dinner", ["potatoes", "carrots", "1 packet soup mix"],
                tags=[("Beef", "main_ingredient")])
    client.post("/api/tags/auto-apply")
    assert ("Vegetarian", "main_ingredient") not in _tags(client, rid)


def test_added_tags_are_searchable(client):
    """Search covers tag names through recipes.tags_text, which has to be
    updated along with the tags."""
    rid = _make(client, "Auto Search Prawns", ["12 prawns", "chili"])
    assert rid not in {h["id"] for h in client.get("/api/recipes?q=fish").json()}
    client.post("/api/tags/auto-apply")
    assert rid in {h["id"] for h in client.get("/api/recipes?q=fish").json()}


def test_needs_the_app_header(client):
    from fastapi.testclient import TestClient
    from app.main import app
    bare = TestClient(app)
    assert bare.post("/api/tags/auto-apply?dry_run=true").status_code == 403
