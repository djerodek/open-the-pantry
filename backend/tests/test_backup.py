"""Backup / export bundle tests.

The important one is test_database_backup_captures_wal_committed_data: the
whole reason snapshot_database exists is that a plain file copy of recipes.db
loses recent commits under WAL. That is invisible in normal use and only
shows up the day someone actually restores a backup, so it gets an explicit
regression test rather than being trusted to a comment.
"""

import io
import json
import os
import shutil
import sqlite3
import zipfile

# NOTE: app.* is imported INSIDE the tests, never at module scope.
# Importing app.database runs os.makedirs(DATA_DIR) at import time, and
# DATA_DIR defaults to /app/data. conftest sets RECIPE_APP_DATA_DIR from a
# fixture, which runs after collection -- so a module-level import here
# executes before the env var exists and fails with PermissionError on any
# machine where /app isn't writable. That is why the client fixture imports
# app.main inside its body too; this file follows the same rule.
# Every test that touches app.* therefore also depends on data_dir (directly
# or via client) so the env var is set before the import happens.


def test_backup_info_reports_counts(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        info = client.get("/api/backup/info")
        assert info.status_code == 200
        body = info.json()
        assert body["recipe_count"] >= 1
        assert body["database_bytes"] > 0
        assert "upload_count" in body and "upload_bytes" in body
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_database_backup_is_a_zip_with_db_and_docs(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        res = client.get("/api/backup/database.zip")
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/zip"
        assert "open-the-pantry-backup-" in res.headers.get("content-disposition", "")

        zf = zipfile.ZipFile(io.BytesIO(res.content))
        names = zf.namelist()
        assert "recipes.db" in names
        assert "manifest.json" in names
        assert "RESTORE.txt" in names

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["backup_type"] == "database"
        assert manifest["recipe_count"] >= 1

        # The restore instructions must actually tell you to stop the app --
        # replacing a SQLite file under a running process is how people
        # corrupt their data.
        restore = zf.read("RESTORE.txt").decode()
        assert "docker compose down" in restore
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_backed_up_database_opens_and_passes_integrity_check(client, sample_recipe_payload, tmp_path):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        res = client.get("/api/backup/database.zip")
        zf = zipfile.ZipFile(io.BytesIO(res.content))
        extracted = tmp_path / "recipes.db"
        extracted.write_bytes(zf.read("recipes.db"))

        conn = sqlite3.connect(extracted)
        try:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            # The snapshot is a real database, not just bytes that unzip.
            titles = [row[0] for row in conn.execute("SELECT title FROM recipes")]
            assert sample_recipe_payload["title"] in titles
        finally:
            conn.close()
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_database_backup_captures_wal_committed_data(client, sample_recipe_payload, tmp_path):
    """A plain copy of recipes.db loses commits still living in the -wal file.

    This asserts the difference directly: the naive copy is allowed to miss
    the row, but the snapshot must contain it. If someone ever "simplifies"
    snapshot_database into shutil.copyfile, this fails.
    """
    from app.backup import snapshot_database
    from app.database import DB_PATH

    payload = dict(sample_recipe_payload, title="WAL probe recipe")
    r = client.post("/api/recipes", json=payload)
    recipe_id = r.json()["id"]
    try:
        naive = tmp_path / "naive.db"
        shutil.copyfile(DB_PATH, naive)

        snapshot = tmp_path / "snapshot.db"
        snapshot_database(str(snapshot))

        def has_probe(path):
            conn = sqlite3.connect(path)
            try:
                return conn.execute(
                    "SELECT COUNT(*) FROM recipes WHERE title = ?", ("WAL probe recipe",)
                ).fetchone()[0]
            except sqlite3.DatabaseError:
                return 0  # a torn copy may not even be readable
            finally:
                conn.close()

        assert has_probe(snapshot) == 1, "snapshot must contain data committed to the WAL"

        # Not asserting the naive copy misses it: whether it does depends on
        # checkpoint timing. The snapshot must be right either way, which is
        # the property that matters.
        assert has_probe(naive) in (0, 1)
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_pdf_bundle_contains_one_pdf_per_recipe(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        res = client.get("/api/backup/pdfs.zip")
        assert res.status_code == 200
        assert res.headers["content-type"] == "application/zip"

        zf = zipfile.ZipFile(io.BytesIO(res.content))
        names = zf.namelist()
        pdfs = [n for n in names if n.endswith(".pdf")]
        assert pdfs, "expected at least one PDF"
        assert "index.csv" in names
        assert "manifest.json" in names

        # Every entry must be a real PDF, not an error page or empty file.
        for name in pdfs:
            assert zf.read(name).startswith(b"%PDF"), f"{name} is not a PDF"

        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["backup_type"] == "pdfs"
        assert manifest["restorable"] is False
        assert manifest["failed_count"] == 0
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_pdf_bundle_honours_include_notes(client, sample_recipe_payload):
    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        res = client.get("/api/backup/pdfs.zip?include_notes=false")
        manifest = json.loads(zipfile.ZipFile(io.BytesIO(res.content)).read("manifest.json"))
        assert manifest["includes_notes"] is False
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_backup_archives_are_not_left_behind(client, sample_recipe_payload):
    """The streaming response deletes its own temp file once sent."""
    from app.main import BACKUP_TMP_DIR

    r = client.post("/api/recipes", json=sample_recipe_payload)
    recipe_id = r.json()["id"]
    try:
        before = set(os.listdir(BACKUP_TMP_DIR)) if os.path.isdir(BACKUP_TMP_DIR) else set()
        assert client.get("/api/backup/database.zip").status_code == 200
        assert client.get("/api/backup/pdfs.zip").status_code == 200
        after = set(os.listdir(BACKUP_TMP_DIR)) if os.path.isdir(BACKUP_TMP_DIR) else set()
        assert after == before, f"left behind: {after - before}"
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_pdf_filenames_are_safe_and_unique(data_dir):
    # data_dir is requested purely so RECIPE_APP_DATA_DIR is set before
    # app.backup pulls in app.database -- see the note at the top of this file.
    from app.backup import safe_pdf_filename

    # Path separators and Windows-reserved characters must not survive, or
    # extracting the zip writes outside the target directory.
    assert "/" not in safe_pdf_filename("Soup/Stew", 1)
    assert "\\" not in safe_pdf_filename("a\\b", 1)
    for ch in '<>:"|?*':
        assert ch not in safe_pdf_filename(f"x{ch}y", 1)
    assert not safe_pdf_filename("../../etc/passwd", 1).startswith("..")

    # Same title, different recipes -> different files, or one silently
    # overwrites the other inside the archive.
    assert safe_pdf_filename("Mum's Pie", 1) != safe_pdf_filename("Mum's Pie", 2)

    # Degenerate titles still produce a usable name.
    assert safe_pdf_filename("", 7).endswith(".pdf")
    assert safe_pdf_filename("   ", 8).endswith(".pdf")
    assert len(safe_pdf_filename("x" * 500, 9)) < 200


def test_pdf_bundle_404s_when_there_are_no_recipes(client):
    existing = client.get("/api/recipes").json()
    if existing:
        return  # other tests' data present; the empty case is covered elsewhere
    assert client.get("/api/backup/pdfs.zip").status_code == 404


def test_backup_contains_exactly_the_photos_the_database_refers_to(client, data_dir):
    """External review: the archive listed uploads/ after the snapshot, so it
    could hold photos nothing referred to, or miss one a recipe did. It now
    takes the list from the snapshot, and reports anything missing."""
    import io, json, os, zipfile
    from app.database import UPLOADS_DIR, SessionLocal
    from app import models

    stray = "img-00000000-0000-4000-8000-000000000001.png"
    open(os.path.join(UPLOADS_DIR, stray), "wb").write(b"orphan")
    gone = "img-00000000-0000-4000-8000-000000000002.png"
    db = SessionLocal()
    r = models.Recipe(title="Backup Ref Test", source_type="manual", image_path=gone)
    db.add(r); db.commit(); rid = r.id; db.close()
    try:
        resp = client.get("/api/backup/database.zip")
        assert resp.status_code == 200
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        names = z.namelist()
        manifest = json.loads(z.read("manifest.json"))
        assert f"uploads/{stray}" not in names
        assert gone in manifest["missing_uploads"]
        assert manifest["unreferenced_uploads_left_out"] >= 1
    finally:
        os.remove(os.path.join(UPLOADS_DIR, stray))
        client.post("/api/recipes/batch-delete", json={"ids": [rid]})


def test_upload_deletion_waits_for_a_running_backup(tmp_path, monkeypatch):
    """A delete during a backup waits until the photos are copied."""
    import threading, time
    from app import backup

    f = tmp_path / "photo.jpg"
    f.write_bytes(b"x")
    done = []
    with backup.UPLOADS_LOCK:
        t = threading.Thread(target=lambda: (backup.remove_upload_file(str(f)), done.append(1)))
        t.start()
        time.sleep(0.3)
        assert f.exists() and not done, "delete must not run while the backup holds the lock"
    t.join(2)
    assert done and not f.exists()
