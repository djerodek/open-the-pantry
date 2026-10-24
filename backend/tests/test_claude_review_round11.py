"""Claude review round 11 (of 0117): control characters in a search, and
Recently deleted recipes out of the search index."""
import json
import sqlite3

from app import search


def _recipe(client, title, ingredient):
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual",
                                          "ingredients": [{"raw_line": ingredient}], "steps": ["Cook."]})
    assert r.status_code == 200, r.text
    return r.json()


def _search(client, q):
    search.forget_vocabulary()
    r = client.get("/api/recipes", params={"q": q})
    assert r.status_code == 200, r.text
    return [x["title"] for x in r.json()], json.loads(r.headers.get("X-Search-Corrected", "{}"))


def test_control_characters_in_a_search(client):
    """A NUL ended FTS5's quoted term early: "unterminated string", a 500."""
    for q in ("chicken\x00", "\x00", "chi\x00cken", "a\x01b\x7f", "rosemarry\x00chicken"):
        assert client.get("/api/recipes", params={"q": q}).status_code == 200, repr(q)
    assert search.parse("chi\x00cken\tpie") == (["chi", "cken", "pie"], [])


def test_a_binned_recipe_offers_no_corrections(client):
    r = _recipe(client, "Zanzibarquinoa salad", "1 cup zanzibarquinoa")
    try:
        assert _search(client, "zanzibarquinoe")[0] == ["Zanzibarquinoa salad"]
        client.delete(f"/api/recipes/{r['id']}")                   # to Recently deleted
        titles, corrected = _search(client, "zanzibarquinoe")
        assert titles == [] and corrected == {}
        assert _search(client, "zanzibarquinoa") == ([], {})
        # Put back: found again, typo and all.
        assert client.post(f"/api/recently-deleted/{r['id']}/restore").status_code == 200
        assert _search(client, "zanzibarquinoe")[0] == ["Zanzibarquinoa salad"]
    finally:
        client.delete(f"/api/recipes/{r['id']}")
        client.delete(f"/api/recently-deleted/{r['id']}")
    # Deleted for good from the bin: the index stays consistent.
    assert _search(client, "zanzibarquinoa") == ([], {})


def test_an_older_database_gets_the_new_triggers(client):
    """A database from before 0119: old triggers, a binned recipe still in
    the index. Starting up replaces the triggers and takes it out."""
    from app import init_db as mod
    FTS_COLUMNS = mod.FTS_COLUMNS
    from app.database import engine
    r = _recipe(client, "Quillowort stew", "2 cups quillowort")
    client.delete(f"/api/recipes/{r['id']}")
    cols = ", ".join(FTS_COLUMNS)
    engine.dispose()
    con = sqlite3.connect(mod.DB_PATH)
    try:
        for name in ("recipes_ai", "recipes_ad", "recipes_au"):
            con.execute(f"DROP TRIGGER {name}")
        con.execute(f"CREATE TRIGGER recipes_ai AFTER INSERT ON recipes BEGIN "
                    f"INSERT INTO recipes_fts(rowid, {cols}) VALUES (new.id, {', '.join('new.' + c for c in FTS_COLUMNS)}); END")
        con.execute(f"INSERT INTO recipes_fts(rowid, {cols}) SELECT id, {cols} FROM recipes WHERE id = ?", (r["id"],))
        con.commit()
        assert con.execute("SELECT count(*) FROM recipes_fts WHERE recipes_fts MATCH 'quillowort'").fetchone()[0] == 1
    finally:
        con.close()

    mod._migrate_and_setup_schema()

    con = sqlite3.connect(mod.DB_PATH)
    try:
        assert con.execute("SELECT count(*) FROM recipes_fts WHERE recipes_fts MATCH 'quillowort'").fetchone()[0] == 0
        sqls = [x[0] for x in con.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' "
                                          "AND name IN ('recipes_ai', 'recipes_ad', 'recipes_au')")]
        assert len(sqls) == 3 and all(mod._LIVE_MARK in x for x in sqls)
        # A restored backup has to pass this (restore.py) with recipes in the bin.
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        con.execute("INSERT INTO recipes_fts(recipes_fts) VALUES('integrity-check')")
    finally:
        con.close()
    assert client.post(f"/api/recently-deleted/{r['id']}/restore").status_code == 200
    assert _search(client, "quillowort")[0] == ["Quillowort stew"]
    client.delete(f"/api/recipes/{r['id']}")
    client.delete(f"/api/recently-deleted/{r['id']}")
