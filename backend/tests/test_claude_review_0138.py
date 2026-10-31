"""Claude review of 0138 (the least recently changed areas): no index on
ingredients/steps.recipe_id or recipe_tags.tag_id, so every save rebuilt
content_text by reading whole tables; a write lock held over 5 s (emptying
Recently deleted) failed other changes with a bare 500; the PDF let long
words run off the page; the source line showed stored codes ("import");
the HTML export's language followed the interface, not the recipe.
Print (the list printed first) is in tests_browser/test_pages.py."""
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


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
    from app.database import engine, BUSY_TIMEOUT_SECONDS
    assert BUSY_TIMEOUT_SECONDS >= 15
    with engine.connect() as conn:
        busy = conn.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert busy == BUSY_TIMEOUT_SECONDS * 1000


def test_a_locked_database_is_a_503_that_says_try_again(client, monkeypatch):
    import sqlalchemy.exc
    from app import main

    def locked(db):
        raise sqlalchemy.exc.OperationalError("SELECT 1", {}, sqlite3.OperationalError("database is locked"))
    monkeypatch.setattr(main, "_binned", locked)
    r = client.get("/api/recently-deleted", headers=H)
    assert r.status_code == 503 and r.headers["Retry-After"] == "5"
    assert r.json()["detail"] == "The app is busy with another change. Try again in a moment."
    r = client.get("/api/recently-deleted", headers={**H, "X-App-Lang": "fr"})
    assert r.json()["detail"].startswith("L'application est occupée")


def test_another_database_error_is_still_a_500(monkeypatch):
    import sqlalchemy.exc
    from fastapi.testclient import TestClient
    from app import main

    def broken(db):
        raise sqlalchemy.exc.OperationalError("SELECT 1", {}, sqlite3.OperationalError("disk I/O error"))
    monkeypatch.setattr(main, "_binned", broken)
    c = TestClient(main.app, raise_server_exceptions=False)      # no lifespan: start-up sweeps too
    assert c.get("/api/recently-deleted", headers=H).status_code == 500


def test_emptying_recently_deleted_commits_in_chunks(client, monkeypatch):
    from sqlalchemy.orm import Session
    from app import main
    monkeypatch.setattr(main, "DELETE_CHUNK", 2)
    ids = []
    for n in range(5):
        r = client.post("/api/recipes", headers=H, json={"title": f"Chunk {n}", "source_type": "manual",
                                                         "ingredients": [{"raw_line": "1 egg"}], "steps": ["Boil."]})
        ids.append(r.json()["id"])
        client.delete(f"/api/recipes/{ids[-1]}", headers=H)
    commits = []
    real = Session.commit
    monkeypatch.setattr(Session, "commit", lambda self: commits.append(1) or real(self))
    before = client.get("/api/recently-deleted").json()["recipes"]
    r = client.delete("/api/recently-deleted", headers=H)
    assert r.json()["deleted"] == len(before) >= 5
    assert len(commits) == -(-len(before) // 2)
    assert client.get("/api/recently-deleted").json()["recipes"] == []


def _export(client, rid, lang="en"):
    r = client.get(f"/api/recipes/{rid}/export.html", headers={**H, "X-App-Lang": lang})
    assert r.status_code == 200, r.text
    return r.text


def test_the_export_source_line_and_language(client):
    body = {"ingredients": [{"raw_line": "1 tasse de farine"}],
            "steps": ["Mélanger la farine avec le beurre dans un bol."]}
    r = client.post("/api/recipes", headers=H, json={"title": "Crêpes", "source_type": "import",
                                                     "language": "fr", **body}).json()
    try:
        html = _export(client, r["id"])
        assert "Source: Imported" in html and "Source: import" not in html
        assert '<html lang="fr-CA">' in html                       # the recipe's language
        assert "Ingredients" in html                               # the interface's labels
        assert "Source : Importée" in _export(client, r["id"], "fr")
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{r['id']}", headers=H)


def test_long_unbroken_text_wraps_in_the_pdf():
    template = (ROOT / "backend/app/templates/recipe_export.html").read_text(encoding="utf-8")
    body = template[template.index("  body {"):template.index("}", template.index("  body {"))]
    assert "overflow-wrap: anywhere" in body
