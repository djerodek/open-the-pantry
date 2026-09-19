"""Backup / export bundles.

Two shapes, for two different jobs:

  * database.zip -- the SQLite database plus every uploaded file. This is the
    one you restore from: put it back and you have the app exactly as it was,
    ratings, notes, showcase photos and all.

  * pdfs.zip -- one PDF per recipe. This one does NOT restore; it is the
    archive you keep so the recipes outlive the app. PDFs open on anything,
    forever, with no Docker and no SQLite.

Both are written to a temp file and streamed, never assembled in memory: a
library with a few hundred photos is comfortably larger than it would be
sensible to hold in RAM on a NAS.
"""

import csv
import io
import json
import os
import re
import sqlite3
import zipfile
from datetime import datetime, timezone

from .database import DB_PATH, UPLOADS_DIR
from .export import render_recipe_pdf


# Keeps a recipe title usable as a filename on Windows, macOS and Linux
# alike: the Windows-reserved set is the strictest, so satisfying it
# satisfies the others.
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_pdf_filename(title: str, recipe_id: int) -> str:
    """A filename that survives extraction on any OS.

    The id prefix keeps the archive stably ordered and, more importantly,
    keeps two recipes with the same title from colliding inside the zip --
    "Mum's Pie" twice would otherwise silently become one file.
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("", title or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Leave room for the id prefix and the extension inside the common
    # 255-byte filename limit.
    cleaned = cleaned[:120] or "recipe"
    return f"{recipe_id:05d} - {cleaned}.pdf"


def snapshot_database(dest_path: str) -> None:
    """Write a consistent copy of the live database to dest_path.

    This uses SQLite's online backup API rather than copying recipes.db,
    and that distinction is the whole reason this function exists. The app
    runs in WAL mode, so at any moment an unknown amount of committed data
    lives in recipes.db-wal and not in recipes.db. Copying the main file
    alone yields a backup that is silently missing recent recipes, or -- if
    a checkpoint happens to be in flight -- torn.

    The backup API reads through the WAL and produces a single, internally
    consistent file, while readers and writers carry on. It is the only
    approach here that is safe without stopping the app.

    The destination is then explicitly checkpointed and taken out of WAL
    mode before closing. The backup inherits the source's journal mode, so
    without this the snapshot is a recipes.db plus its own -wal/-shm
    sidecars -- and the archive only carries recipes.db. Relying on "SQLite
    checkpoints when the last connection closes" would make the completeness
    of the backup an implementation detail; doing it here makes it a
    guarantee.
    """
    source = sqlite3.connect(DB_PATH)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
            dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # DELETE mode removes the -wal/-shm files outright, leaving one
            # self-contained file to put in the zip.
            dest.execute("PRAGMA journal_mode=DELETE")
        finally:
            dest.close()
    finally:
        source.close()

    # Belt and braces: if the pragmas above were ever skipped or failed, the
    # sidecars would otherwise accumulate in the data directory.
    for suffix in ("-wal", "-shm"):
        sidecar = dest_path + suffix
        if os.path.isfile(sidecar):
            os.remove(sidecar)


RESTORE_INSTRUCTIONS = """\
Open the Pantry -- restoring this backup
========================================

This archive contains a complete copy of your recipe library:

  recipes.db    the database (recipes, tags, ratings, notes, settings)
  uploads/      every showcase photo and uploaded PDF
  manifest.json what was in it, and when it was taken

To restore
----------

1. Stop the app:

       docker compose down

2. Unpack this archive into the data directory next to your compose file,
   replacing what is there. From the directory holding docker-compose.yml:

       unzip -o open-the-pantry-backup-YYYYMMDD-HHMMSS.zip -d ./data

   Your data directory should then contain recipes.db and uploads/.

3. Start the app again:

       docker compose up -d

The app is stopped for step 2 on purpose. Replacing the database underneath
a running app is how you corrupt it -- the running process still holds open
handles to the old file.

Notes
-----

* Restoring REPLACES the current library. Anything added since this backup
  was taken is gone. If you are unsure, copy your existing ./data somewhere
  else first.
* recipes.db is an ordinary SQLite database. You can open it with any SQLite
  tool if you ever want the data out without running this app.
* If you set RECIPE_APP_ENCRYPTION_KEY for email ingest, keep using the SAME
  key after restoring. Stored email credentials are encrypted with it, and a
  different key cannot decrypt them -- the app will refuse to use them rather
  than fail silently. Everything else restores regardless.
"""


def build_database_backup(dest_zip_path: str) -> dict:
    """Database + uploads, as a restorable zip. Returns manifest counts."""
    tmp_db = dest_zip_path + ".db.tmp"
    snapshot_database(tmp_db)

    try:
        upload_names = sorted(
            n for n in os.listdir(UPLOADS_DIR)
            if os.path.isfile(os.path.join(UPLOADS_DIR, n))
        ) if os.path.isdir(UPLOADS_DIR) else []

        uploads_bytes = sum(
            os.path.getsize(os.path.join(UPLOADS_DIR, n)) for n in upload_names
        )

        with sqlite3.connect(tmp_db) as snap:
            recipe_count = snap.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]

        manifest = {
            "application": "open-the-pantry",
            "backup_type": "database",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "recipe_count": recipe_count,
            "upload_count": len(upload_names),
            "uploads_bytes": uploads_bytes,
            "database_bytes": os.path.getsize(tmp_db),
            "restores_with": "see RESTORE.txt",
        }

        # ZIP_DEFLATED on a SQLite file is worth it (they compress well);
        # already-compressed JPEGs and PDFs simply won't shrink much, which
        # costs a little CPU and no correctness.
        with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(tmp_db, "recipes.db")
            for name in upload_names:
                zf.write(os.path.join(UPLOADS_DIR, name), f"uploads/{name}")
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("RESTORE.txt", RESTORE_INSTRUCTIONS)

        return manifest
    finally:
        if os.path.isfile(tmp_db):
            os.remove(tmp_db)


def build_pdf_bundle(recipes, dest_zip_path: str, include_notes: bool = True) -> dict:
    """One PDF per recipe, plus an index. Does not restore -- see module docstring.

    A recipe that fails to render is recorded in the index and skipped rather
    than aborting the archive: one malformed recipe should not cost you a
    backup of the other two hundred.
    """
    index_rows = []
    failures = 0

    with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for recipe in recipes:
            filename = safe_pdf_filename(recipe.title, recipe.id)
            try:
                image_path = None
                if recipe.image_path:
                    candidate = os.path.join(UPLOADS_DIR, recipe.image_path)
                    if os.path.isfile(candidate):
                        image_path = candidate
                # The showcase image IS included here, unlike the share
                # export: this is your own archive, not something handed to
                # someone else, and a recipe card reads better with its photo.
                pdf_bytes = render_recipe_pdf(
                    recipe, image_path, include_notes=include_notes, include_image=True,
                )
                zf.writestr(filename, pdf_bytes)
                index_rows.append({
                    "id": recipe.id, "title": recipe.title or "",
                    "file": filename, "status": "ok",
                    "source_type": recipe.source_type or "",
                })
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
                failures += 1
                index_rows.append({
                    "id": recipe.id, "title": recipe.title or "",
                    "file": "", "status": f"failed: {type(exc).__name__}",
                    "source_type": recipe.source_type or "",
                })

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["id", "title", "file", "status", "source_type"])
        writer.writeheader()
        writer.writerows(index_rows)
        zf.writestr("index.csv", buf.getvalue())

        manifest = {
            "application": "open-the-pantry",
            "backup_type": "pdfs",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "recipe_count": len(index_rows),
            "pdf_count": len(index_rows) - failures,
            "failed_count": failures,
            "includes_notes": include_notes,
            "restorable": False,
            "note": "A reading archive, not a backup you can restore. "
                    "Use the database backup to restore into the app.",
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    return manifest
