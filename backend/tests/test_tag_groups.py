"""Settings -> Tag groups: adding groups and tags, keywords the tagger
uses, and deleting what was added.

app.* is imported inside tests only; see the note in test_backup.py.
"""


import pytest


@pytest.fixture(autouse=True)
def _remove_what_each_test_added(client):
    """The test database lasts the whole session, and keywords added here
    feed the tagger for every later test. Delete the groups and tags each
    test created, so results don't depend on test order (DeepSeek review)."""
    groups_before = {g["key"] for g in client.get("/api/tag-groups").json()}
    tags_before = {t["id"]: t["keywords"] for t in client.get("/api/tags").json()}
    yield
    for g in client.get("/api/tag-groups").json():
        if g["key"] not in groups_before:
            client.delete(f"/api/tag-groups/{g['key']}")
    for t in client.get("/api/tags").json():
        if t["id"] not in tags_before and t["user_defined"]:
            client.delete(f"/api/tags/{t['id']}")
        elif t["id"] in tags_before and t["keywords"] != tags_before[t["id"]]:
            client.put(f"/api/tags/{t['id']}", json={"keywords": tags_before[t["id"]]})


def _recipe(client, title, ingredients, tags=()):
    r = client.post("/api/recipes", json={
        "title": title, "source_type": "manual", "steps": ["Cook it."],
        "ingredients": [{"raw_line": i} for i in ingredients],
        "tags": [{"name": n, "category": c} for n, c in tags]})
    assert r.status_code == 200
    return r.json()["id"]


def test_builtin_groups_are_listed_in_order(client):
    keys = [g["key"] for g in client.get("/api/tag-groups").json()]
    assert keys[:3] == ["meal_type", "cooking_style", "main_ingredient"]
    assert keys[-1] == "custom"


def test_new_group_with_a_keyword_tag_is_suggested_and_scanned(client):
    g = client.post("/api/tag-groups", json={"label": "Cuisine TG"}).json()
    assert g["key"].startswith("g-") and g["builtin"] is False
    assert client.get("/api/tag-groups").json()[-1]["key"] == "custom"          # custom stays last
    assert client.post("/api/tag-groups", json={"label": "cuisine tg"}).status_code == 409

    t = client.post("/api/tags", json={"name": "Thai", "category": g["key"],
                                       "keywords": "Lemongrass, fish sauce; galangal"}).json()
    assert t["keywords"] == "lemongrass, fish sauce, galangal" and t["user_defined"] is True

    got = client.post("/api/tags/suggest", json={"title": "Green curry", "ingredients": ["2 stalks lemongrass"]}).json()
    assert {"name": "Thai", "category": g["key"], "subgroup": None} in got

    rid = _recipe(client, "TG Tom Yum", ["1 stalk lemongrass", "shrimp"])
    ch = client.post("/api/tags/auto-apply?dry_run=true").json()["changes"]
    assert any(c["id"] == rid and "Thai" in c["added"] for c in ch)


def test_keywords_on_a_builtin_tag(client):
    beef = next(t for t in client.get("/api/tags").json() if t["name"] == "Beef" and t["category"] == "main_ingredient")
    client.put(f"/api/tags/{beef['id']}", json={"keywords": "bavette"})
    names = {t["name"] for t in client.post("/api/tags/suggest", json={"title": "Grilled bavette", "ingredients": ["1 bavette"]}).json()}
    assert "Beef" in names and "Vegetarian" not in names
    client.put(f"/api/tags/{beef['id']}", json={"keywords": ""})
    assert client.delete(f"/api/tags/{beef['id']}").status_code == 400          # built-in: not deletable


def test_new_meat_tag_rules_out_vegetarian(client):
    client.post("/api/tags", json={"name": "Lamb TG", "category": "main_ingredient",
                                   "keywords": "lamb shoulder", "rules_out_vegetarian": True})
    names = {t["name"] for t in client.post("/api/tags/suggest", json={
        "title": "Slow roast", "ingredients": ["1 lamb shoulder", "rosemary"]}).json()}
    assert "Lamb TG" in names and "Vegetarian" not in names


def test_deleting_a_group_removes_its_tags_from_recipes(client):
    g = client.post("/api/tag-groups", json={"label": "Occasion TG"}).json()
    client.post("/api/tags", json={"name": "Holiday TG", "category": g["key"], "keywords": "holiday"})
    rid = _recipe(client, "TG Holiday Ham", ["1 ham"], tags=[("Holiday TG", g["key"])])
    assert rid in {h["id"] for h in client.get("/api/recipes?q=Holiday").json()}

    r = client.delete(f"/api/tag-groups/{g['key']}").json()
    assert r == {"deleted_tags": 1, "recipes_changed": 1}
    assert not any(t["name"] == "Holiday TG" for t in client.get(f"/api/recipes/{rid}").json()["tags"])
    suggested = client.post("/api/tags/suggest", json={"title": "holiday roast"}).json()
    assert not any(t["name"] == "Holiday TG" for t in suggested)          # its keywords are gone too
    assert client.delete("/api/tag-groups/meal_type").status_code == 400


def test_old_database_gets_the_new_columns_and_groups(tmp_path):
    import sqlite3
    from unittest.mock import patch
    from app import init_db
    db = tmp_path / "recipes.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, subgroup TEXT)")
    with patch.object(init_db, "DB_PATH", str(db)):
        init_db._migrate_and_setup_schema()
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(tags)")}
    assert {"keywords", "rules_out_vegetarian", "user_defined"} <= cols


def test_tag_in_a_missing_group_moves_to_custom(client):
    """A group row deleted outside the app left its tags in no group."""
    from app.database import SessionLocal
    from app import models
    from app.init_db import init_db
    db = SessionLocal()
    t = models.Tag(name="Orphan TG", category="g-vanished", user_defined=True)
    db.add(t); db.commit(); db.close()
    init_db()
    cats = {x["name"]: x["category"] for x in client.get("/api/tags").json()}
    assert cats["Orphan TG"] == "custom"
    tid = next(x["id"] for x in client.get("/api/tags").json() if x["name"] == "Orphan TG")
    client.delete(f"/api/tags/{tid}")


def test_zz_nothing_left_behind(client):
    """Last in the file: the cleanup fixture removed what the tests added,
    from the database and from the tagger."""
    from app.ingestion import tagger
    assert not any(n in ("Thai", "Lamb TG", "Holiday TG") for n, *_ in tagger._user_keywords)
    assert not any(g["key"].startswith("g-") for g in client.get("/api/tag-groups").json())
