"""Search and parsing findings from the Opus 5 review (group D), each
reproduced against the previous code first.

app.* is imported inside tests only; see the note in test_backup.py.
"""
import pytest


def _make(client, title, ingredients, steps, **extra):
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual", "tags": [],
        "ingredients": [{"raw_line": i, "name": i} for i in ingredients], "steps": steps, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _titles(client, q):
    return [r["title"] for r in client.get("/api/recipes", params={"q": q}).json()]


def test_search_finds_ingredients_of_manual_recipes(client):
    """Reproduced: a manual recipe listing saffron didn't come up for
    "saffron" -- only title and raw_text were indexed, and manual recipes
    have no raw_text."""
    rid = _make(client, "Golden Rice Zq", ["1 pinch saffron"], ["Steep it"])
    try:
        assert "Golden Rice Zq" in _titles(client, "saffron")
        assert "Golden Rice Zq" in _titles(client, "steep")
    finally:
        client.delete(f"/api/recipes/{rid}")


def test_search_follows_edits(client):
    rid = _make(client, "Edited Soup Zq", ["1 onion"], ["Cook"])
    try:
        client.put(f"/api/recipes/{rid}", json={"title": "Edited Soup Zq", "source_type": "manual", "tags": [],
                   "ingredients": [{"raw_line": "2 parsnips", "name": "2 parsnips"}], "steps": ["Cook"]})
        assert "Edited Soup Zq" in _titles(client, "parsnips")
        assert "Edited Soup Zq" not in _titles(client, "onion")
    finally:
        client.delete(f"/api/recipes/{rid}")


def test_search_matches_word_beginnings_in_any_order(client):
    """"chick" didn't find "Chicken soup": the query was one exact phrase."""
    rid = _make(client, "Chicken Noodle Zq", ["1 chicken"], ["Simmer gently"])
    try:
        assert "Chicken Noodle Zq" in _titles(client, "chick")
        assert "Chicken Noodle Zq" in _titles(client, "noodle chicken")
        assert "Chicken Noodle Zq" not in _titles(client, "chicken beef")
    finally:
        client.delete(f"/api/recipes/{rid}")


def test_search_includes_notes(client):
    rid = _make(client, "Notes Stew Zq", ["1 carrot"], ["Stew"])
    try:
        client.patch(f"/api/recipes/{rid}/notes", json={"notes": "Grandpa's favourite"})
        assert "Notes Stew Zq" in _titles(client, "grandpa")
    finally:
        client.delete(f"/api/recipes/{rid}")


@pytest.mark.parametrize("q", ['"', "a:b", "{title}", "-x", "chick*", "NEAR(a b)", "((("])
def test_search_syntax_characters_are_harmless(client, q):
    assert client.get("/api/recipes", params={"q": q}).status_code == 200


def test_old_database_is_migrated_without_corruption(tmp_path):
    """The search index gained two columns. The migration must rebuild it
    from an old-format database without corrupting it; an earlier draft of
    this change filled the new column with the triggers already in place,
    which corrupted the index ("database disk image is malformed")."""
    import sqlite3
    db = tmp_path / "recipes.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, source_url TEXT, source_type TEXT NOT NULL,
            servings TEXT, prep_time TEXT, cook_time TEXT, total_time TEXT, image_path TEXT, raw_text TEXT,
            notes TEXT, tags_text TEXT DEFAULT '', ocr_confidence REAL, favorite INTEGER NOT NULL DEFAULT 0,
            tastiness_rating INTEGER, cook_time_rating TEXT, difficulty_rating TEXT,
            actual_cook_time_minutes INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE ingredients (id INTEGER PRIMARY KEY, recipe_id INTEGER, position INTEGER, raw_line TEXT NOT NULL,
            quantity TEXT, unit TEXT, name TEXT);
        CREATE TABLE steps (id INTEGER PRIMARY KEY, recipe_id INTEGER, position INTEGER, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE recipes_fts USING fts5(title, raw_text, tags_text, content='recipes', content_rowid='id');
        CREATE TRIGGER recipes_ai AFTER INSERT ON recipes BEGIN
            INSERT INTO recipes_fts(rowid, title, raw_text, tags_text) VALUES (new.id, new.title, new.raw_text, new.tags_text); END;
        CREATE TRIGGER recipes_ad AFTER DELETE ON recipes BEGIN
            INSERT INTO recipes_fts(recipes_fts, rowid, title, raw_text, tags_text) VALUES ('delete', old.id, old.title, old.raw_text, old.tags_text); END;
        CREATE TRIGGER recipes_au AFTER UPDATE ON recipes BEGIN
            INSERT INTO recipes_fts(recipes_fts, rowid, title, raw_text, tags_text) VALUES ('delete', old.id, old.title, old.raw_text, old.tags_text);
            INSERT INTO recipes_fts(rowid, title, raw_text, tags_text) VALUES (new.id, new.title, new.raw_text, new.tags_text); END;
        INSERT INTO recipes (id, title, source_type, raw_text, tags_text) VALUES (1, 'Saffron Rice', 'manual', NULL, '');
        INSERT INTO ingredients (recipe_id, position, raw_line, name) VALUES (1, 0, '1 pinch saffron', 'saffron');
        INSERT INTO steps (recipe_id, position, text) VALUES (1, 0, 'Steep');
    """)
    c.commit(); c.close()

    from unittest.mock import patch
    from app import init_db
    with patch.object(init_db, "DB_PATH", str(db)):
        init_db._migrate_and_setup_schema()

    c = sqlite3.connect(db)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    c.execute("INSERT INTO recipes_fts(recipes_fts) VALUES('integrity-check')")
    assert c.execute("SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'saffron'").fetchall() == [(1,)]
    c.execute("INSERT INTO ingredients (recipe_id, position, raw_line) VALUES (1, 1, '2 parsnips')")
    assert c.execute("SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH 'parsnips'").fetchall() == [(1,)]
    c.execute("INSERT INTO recipes_fts(recipes_fts) VALUES('integrity-check')")


def test_json_ld_sections_keep_their_steps():
    """HowToSection used to be reduced to its heading: ['For the dough']."""
    import json
    from app.ingestion.url_ingest import _try_json_ld
    data = {"@context": "https://schema.org", "@type": "Recipe", "name": "Pie",
            "recipeIngredient": ["2 cups flour"],
            "recipeInstructions": [
                {"@type": "HowToSection", "name": "For the dough", "itemListElement": [
                    {"@type": "HowToStep", "text": "Mix the flour."}, {"@type": "HowToStep", "text": "Chill."}]},
                {"@type": "HowToSection", "name": "For the filling", "itemListElement": [
                    {"@type": "HowToStep", "text": "Slice apples."}]}]}
    result = _try_json_ld(f'<script type="application/ld+json">{json.dumps(data)}</script>')
    assert result.steps == ["For the dough:", "Mix the flour.", "Chill.", "For the filling:", "Slice apples."]


@pytest.mark.parametrize("line,qty,unit,name", [
    ("1½ cups flour", "1½", "cup", "flour"),
    ("1 ½ cups flour", "1 ½", "cup", "flour"),
    ("½ cup milk", "½", "cup", "milk"),
    ("3–4 heads bok choy", "3–4", "head", "bok choy"),
])
def test_ingredient_parser_handles_unicode_fractions(line, qty, unit, name):
    from app.ingestion.ingredient_parser import parse_ingredient_line
    d = parse_ingredient_line(line)
    assert (d["quantity"], d["unit"], d["name"]) == (qty, unit, name)


def test_lines_saved_from_the_ui_are_parsed_and_kept_verbatim(client):
    """The review/edit/manual screens send {raw_line: line, name: line};
    quantity and unit were lost for everything saved from the UI."""
    rid = _make(client, "Parsed Zq", ["3 Tablespoons maple syrup", "Salt to taste"], ["Mix"])
    try:
        ings = client.get(f"/api/recipes/{rid}").json()["ingredients"]
        assert (ings[0]["quantity"], ings[0]["unit"], ings[0]["name"]) == ("3", "tbsp", "maple syrup")
        assert ings[0]["raw_line"] == "3 Tablespoons maple syrup"
        assert ings[1]["quantity"] is None and ings[1]["name"] == "Salt to taste"
    finally:
        client.delete(f"/api/recipes/{rid}")
