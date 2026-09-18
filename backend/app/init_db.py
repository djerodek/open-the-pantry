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
    "favorite": "INTEGER NOT NULL DEFAULT 0",
    "tastiness_rating": "INTEGER",
    "cook_time_rating": "TEXT",
    "difficulty_rating": "TEXT",
    "actual_cook_time_minutes": "INTEGER",
    "notes": "TEXT",
}

FTS_TRIGGER_SQL = [
    """
    CREATE TRIGGER IF NOT EXISTS recipes_ai AFTER INSERT ON recipes BEGIN
        INSERT INTO recipes_fts(rowid, title, raw_text, tags_text)
        VALUES (new.id, new.title, new.raw_text, new.tags_text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS recipes_ad AFTER DELETE ON recipes BEGIN
        INSERT INTO recipes_fts(recipes_fts, rowid, title, raw_text, tags_text)
        VALUES ('delete', old.id, old.title, old.raw_text, old.tags_text);
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS recipes_au AFTER UPDATE ON recipes BEGIN
        INSERT INTO recipes_fts(recipes_fts, rowid, title, raw_text, tags_text)
        VALUES ('delete', old.id, old.title, old.raw_text, old.tags_text);
        INSERT INTO recipes_fts(rowid, title, raw_text, tags_text)
        VALUES (new.id, new.title, new.raw_text, new.tags_text);
    END;
    """,
]


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

        if table_exists:
            for col, decl in NEW_RECIPE_COLUMNS.items():
                if col not in existing_cols:
                    cur.execute(f"ALTER TABLE recipes ADD COLUMN {col} {decl}")

            fts_cols = set()
            try:
                fts_cols = {row[1] for row in cur.execute("PRAGMA table_info(recipes_fts)")}
            except sqlite3.OperationalError:
                pass

            if fts_cols and "tags_text" not in fts_cols:
                # Backfill tags_text from the existing recipe_tags/tags join
                # before rebuilding the FTS index against it.
                cur.execute("""
                    UPDATE recipes
                    SET tags_text = COALESCE((
                        SELECT group_concat(t.name, ' ')
                        FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id
                        WHERE rt.recipe_id = recipes.id
                    ), '')
                """)
                cur.execute("DROP TRIGGER IF EXISTS recipes_ai")
                cur.execute("DROP TRIGGER IF EXISTS recipes_ad")
                cur.execute("DROP TRIGGER IF EXISTS recipes_au")
                cur.execute("DROP TABLE IF EXISTS recipes_fts")
                cur.execute("""
                    CREATE VIRTUAL TABLE recipes_fts USING fts5(
                        title, raw_text, tags_text, content='recipes', content_rowid='id'
                    )
                """)
                for stmt in FTS_TRIGGER_SQL:
                    cur.execute(stmt)
                cur.execute("INSERT INTO recipes_fts(recipes_fts) VALUES('rebuild')")
            elif not fts_cols:
                # recipes table exists but recipes_fts doesn't (shouldn't
                # normally happen, but handle it defensively)
                cur.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
                        title, raw_text, tags_text, content='recipes', content_rowid='id'
                    )
                """)
                for stmt in FTS_TRIGGER_SQL:
                    cur.execute(stmt)
                cur.execute("INSERT INTO recipes_fts(recipes_fts) VALUES('rebuild')")

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
        conn.execute(text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS recipes_fts USING fts5(
                title, raw_text, tags_text, content='recipes', content_rowid='id'
            )
        """))
        for stmt in FTS_TRIGGER_SQL:
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
