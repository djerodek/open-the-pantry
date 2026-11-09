"""The database: indexes, busy and locked handling, triggers and columns added to older databases."""
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from app import search

H = {"X-Requested-With": "OpenThePantry"}


ROOT = Path(__file__).resolve().parents[2]


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


def test_the_lookup_columns_are_indexed(client):
    from app.database import DB_PATH
    with sqlite3.connect(DB_PATH) as conn:
        def indexed(table, column):
            for _, name, *_ in conn.execute(f"PRAGMA index_list({table})"):
                cols = [c[2] for c in conn.execute(f"PRAGMA index_info({name})")]
                if cols and cols[0] == column:
                    return True
            return False
        assert indexed("ingredients", "recipe_id") and indexed("steps", "recipe_id")
        assert indexed("recipe_tags", "tag_id") and indexed("recipe_tags", "recipe_id")
        # The content triggers' lookups use them.
        plan = " ".join(str(r) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT raw_line FROM ingredients WHERE recipe_id = 1 ORDER BY position"))
        assert "ix_ingredients_recipe_id" in plan


def test_a_write_waits_longer_than_five_seconds():
    from app.database import BUSY_TIMEOUT_SECONDS, engine
    assert BUSY_TIMEOUT_SECONDS >= 15
    with engine.connect() as conn:
        busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert busy == BUSY_TIMEOUT_SECONDS * 1000


def test_a_locked_database_is_a_503_that_says_try_again(client, monkeypatch):
    import sqlalchemy.exc

    from app import main, routes

    def locked(db):
        raise sqlalchemy.exc.OperationalError("SELECT 1", {}, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(routes.recipes, "_binned", locked)
    r = client.get("/api/recently-deleted", headers=H)
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert r.json()["detail"] == "The app is busy with another change. Try again in a moment."
    r = client.get("/api/recently-deleted", headers={**H, "X-App-Lang": "fr"})
    assert r.json()["detail"].startswith("L'application est occupée")


def test_another_database_error_is_still_a_500(monkeypatch):
    import sqlalchemy.exc
    from fastapi.testclient import TestClient

    from app import main, routes

    def broken(db):
        raise sqlalchemy.exc.OperationalError("SELECT " + "x" * 5000, {}, sqlite3.OperationalError("disk I/O error"))
    monkeypatch.setattr(routes.recipes, "_binned", broken)
    records = []

    class Keep(logging.Handler):
        def emit(self, record):
            records.append(record)
    handler = Keep()
    main.log.addHandler(handler)
    try:
        c = TestClient(main.app, raise_server_exceptions=False)      # no lifespan: start-up sweeps too
        assert c.get("/api/recently-deleted", headers=H).status_code == 500
    finally:
        main.log.removeHandler(handler)
    # Logged briefly (review of 0158): the error and the start of the
    # statement, no traceback, which holds the whole SQL.
    logged = [r for r in records if "Database error" in r.getMessage()]
    assert len(logged) == 1 and logged[0].exc_info is None
    assert "disk I/O error" in logged[0].getMessage() and len(logged[0].getMessage()) < 600


@pytest.mark.parametrize("message,code,busy", [
    ("database is locked", None, True), ("database table is locked", None, True),
    ("database schema is locked", None, True), ("disk I/O error", None, False),
    ("anything", "SQLITE_BUSY", True), ("anything", "SQLITE_LOCKED_SHAREDCACHE", True),
    ("anything", "SQLITE_BUSY_SNAPSHOT", True), ("no such table: x", "SQLITE_ERROR", False),
])
def test_both_sqlite_busy_results_are_a_503(message, code, busy):
    """GPT review of 0140: only "database is locked" was recognised;
    SQLITE_LOCKED ("database table is locked") gave a 500."""
    import sqlalchemy.exc

    from app import main
    orig = sqlite3.OperationalError(message)
    if code:
        orig.sqlite_errorname = code
    assert main._database_busy(sqlalchemy.exc.OperationalError("SELECT 1", {}, orig)) is busy


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


def test_problem_columns_are_added_to_an_existing_database(tmp_path):
    import sqlite3
    from unittest.mock import patch

    from app import init_db
    db = tmp_path / "recipes.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE email_ingest_settings (id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT 0, "
                  "allowed_senders TEXT NOT NULL DEFAULT '')")
        c.execute("INSERT INTO email_ingest_settings (id, enabled) VALUES (1, 0)")
    with patch.object(init_db, "DB_PATH", str(db)):
        init_db._migrate_and_setup_schema()
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(email_ingest_settings)")}
    assert {"last_problem", "last_problem_at"} <= cols
