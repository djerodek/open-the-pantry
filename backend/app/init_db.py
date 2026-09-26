import sqlite3
from sqlalchemy import text
from .database import engine, SessionLocal, Base, DB_PATH

# Seed tag data: (name, category, subgroup)
SEED_TAGS = [
    # meal_type
    ("Breakfast", "meal_type", None),
    ("Brunch", "meal_type", None),
    ("Lunch", "meal_type", None),
    ("Dinner", "meal_type", None),
    ("Snack", "meal_type", None),
    ("Cocktails", "meal_type", None),
    # cooking_style - food prep
    ("Oven", "cooking_style", None),
    ("Stovetop", "cooking_style", None),
    ("Grill", "cooking_style", None),
    ("Barbecue/Smoker", "cooking_style", None),
    ("Deep Fryer", "cooking_style", None),
    ("Pressure Cooker", "cooking_style", None),
    ("Griddle", "cooking_style", None),
    # cooking_style - cocktail prep (subgroup so UI can render as a separate subtab)
    ("Shaken", "cooking_style", "cocktail"),
    ("Stirred", "cooking_style", "cocktail"),
    ("Built", "cooking_style", "cocktail"),
    ("Blended", "cooking_style", "cocktail"),
    # main_ingredient
    ("Chicken", "main_ingredient", None),
    ("Pork", "main_ingredient", None),
    ("Beef", "main_ingredient", None),
    ("Fish", "main_ingredient", None),
    ("Vegetarian", "main_ingredient", None),
    ("Game", "main_ingredient", None),
]

NEW_RECIPE_COLUMNS = {
    "tags_text": "TEXT DEFAULT ''",
    "content_text": "TEXT DEFAULT ''",
    "favorite": "INTEGER NOT NULL DEFAULT 0",
    "tastiness_rating": "INTEGER",
    "cook_time_rating": "TEXT",
    "difficulty_rating": "TEXT",
    "actual_cook_time_minutes": "INTEGER",
    "notes": "TEXT",
}

FTS_COLUMNS = ("title", "raw_text", "tags_text", "content_text", "notes")
_COLS = ", ".join(FTS_COLUMNS)
_NEW = ", ".join("new." + c for c in FTS_COLUMNS)
_OLD = ", ".join("old." + c for c in FTS_COLUMNS)
FTS_CREATE_SQL = f"CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5({_COLS}, content='recipes', content_rowid='id')"

FTS_TRIGGER_SQL = [
    f"""
    CREATE TRIGGER IF NOT EXISTS recipes_ai AFTER INSERT ON recipes BEGIN
        INSERT INTO recipes_fts(rowid, {_COLS}) VALUES (new.id, {_NEW});
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS recipes_ad AFTER DELETE ON recipes BEGIN
        INSERT INTO recipes_fts(recipes_fts, rowid, {_COLS}) VALUES ('delete', old.id, {_OLD});
    END;
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS recipes_au AFTER UPDATE ON recipes BEGIN
        INSERT INTO recipes_fts(recipes_fts, rowid, {_COLS}) VALUES ('delete', old.id, {_OLD});
        INSERT INTO recipes_fts(rowid, {_COLS}) VALUES (new.id, {_NEW});
    END;
    """,
]

# recipes.content_text = all ingredient lines and step texts, in order.
_CONTENT_SQL = """
    COALESCE((SELECT group_concat(raw_line, ' ') FROM (SELECT raw_line FROM ingredients
              WHERE recipe_id = {rid} ORDER BY position)), '')
    || ' ' ||
    COALESCE((SELECT group_concat(text, ' ') FROM (SELECT text FROM steps
              WHERE recipe_id = {rid} ORDER BY position)), '')
"""
CONTENT_TRIGGER_SQL = []
for _table in ("ingredients", "steps"):
    for _event, _ref in (("INSERT", "new"), ("UPDATE", "new"), ("DELETE", "old")):
        CONTENT_TRIGGER_SQL.append(f"""
        CREATE TRIGGER IF NOT EXISTS {_table}_content_{_event.lower()} AFTER {_event} ON {_table} BEGIN
            UPDATE recipes SET content_text = {_CONTENT_SQL.format(rid=_ref + ".recipe_id")}
            WHERE id = {_ref}.recipe_id;
        END;
        """)


def _migrate_and_setup_schema():
    """
    Runs entirely on a single, isolated sqlite3 connection (not the
    SQLAlchemy pooled engine, and not concurrent with any ORM Session).
    Dropping/recreating an FTS5 virtual table from one connection while
    another connection holds an open session against the same file is what
    corrupts the SQLite file in practice -- this keeps all DDL + backfill
    in one connection, fully committed and closed, before the app's normal
    pooled engine or any ORM session touches the database at all.
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()

        existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(recipes)")}
        table_exists = bool(existing_cols)

        email_cols = {row[1] for row in cur.execute("PRAGMA table_info(email_ingest_settings)")}
        if email_cols and "allowed_senders" not in email_cols:
            cur.execute("ALTER TABLE email_ingest_settings ADD COLUMN allowed_senders TEXT NOT NULL DEFAULT ''")

        if table_exists:
            for col, decl in NEW_RECIPE_COLUMNS.items():
                if col not in existing_cols:
                    cur.execute(f"ALTER TABLE recipes ADD COLUMN {col} {decl}")

            fts_cols = set()
            try:
                fts_cols = {row[1] for row in cur.execute("PRAGMA table_info(recipes_fts)")}
            except sqlite3.OperationalError:
                pass

            if fts_cols and set(FTS_COLUMNS) - fts_cols:
                # The index is missing a column (older version). Order
                # matters: drop the index and its triggers FIRST, then
                # backfill, then rebuild. Backfilling with the triggers in
                # place makes each UPDATE ask the index to delete entries it
                # never contained, which corrupts an external-content FTS5
                # table ("database disk image is malformed") -- caught by
                # migrating a real old-format database before release.
                cur.execute("DROP TRIGGER IF EXISTS recipes_ai")
                cur.execute("DROP TRIGGER IF EXISTS recipes_ad")
                cur.execute("DROP TRIGGER IF EXISTS recipes_au")
                cur.execute("DROP TABLE IF EXISTS recipes_fts")
                if "tags_text" not in fts_cols:
                    cur.execute("""
                        UPDATE recipes
                        SET tags_text = COALESCE((
                            SELECT group_concat(t.name, ' ')
                            FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id
                            WHERE rt.recipe_id = recipes.id
                        ), '')
                    """)
                cur.execute(f"UPDATE recipes SET content_text = {_CONTENT_SQL.format(rid='recipes.id')}")
                cur.execute(FTS_CREATE_SQL)
                cur.execute("INSERT INTO recipes_fts(recipes_fts) VALUES('rebuild')")
                for stmt in FTS_TRIGGER_SQL:
                    cur.execute(stmt)
            elif not fts_cols:
                # recipes table exists but recipes_fts doesn't (shouldn't
                # normally happen, but handle it defensively)
                cur.execute(FTS_CREATE_SQL)
                for stmt in FTS_TRIGGER_SQL:
                    cur.execute(stmt)
                cur.execute("INSERT INTO recipes_fts(recipes_fts) VALUES('rebuild')")

            for stmt in CONTENT_TRIGGER_SQL:
                cur.execute(stmt)

        conn.commit()
    finally:
        conn.close()


def init_db():
    # 1. Create any missing tables (fresh install) via the ORM.
    Base.metadata.create_all(bind=engine)
    engine.dispose()  # ensure no pooled connection is held open before raw migration

    # 2. Column/FTS migration for existing databases, in total isolation.
    _migrate_and_setup_schema()

    # 3. Fresh-install case: recipes_fts + triggers won't exist yet after
    # step 1 (create_all doesn't know about virtual tables). Create them now
    # if still missing -- harmless no-op if the migration step above already
    # handled it.
    with engine.connect() as conn:
        conn.execute(text(FTS_CREATE_SQL))
        for stmt in FTS_TRIGGER_SQL + CONTENT_TRIGGER_SQL:
            conn.execute(text(stmt))
        conn.commit()

    # 4. Seed tag data.
    from . import models
    db = SessionLocal()
    try:
        for name, category, subgroup in SEED_TAGS:
            existing = db.query(models.Tag).filter_by(name=name, category=category).first()
            if not existing:
                db.add(models.Tag(name=name, category=category, subgroup=subgroup))
        db.commit()
    finally:
        db.close()
