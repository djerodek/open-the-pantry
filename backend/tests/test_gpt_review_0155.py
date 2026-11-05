"""GPT review of 0155: a restore that failed after the library was
replaced let the app serve the backup's database with the old photos (the
restore guard was cleared in `finally`); a full safety copy without a
marker could pass for a blanked backup; the safety copy's RESTORE.txt said
it holds the cPanel token; index.csv's formula guard missed a line feed,
full-width signs and leading spaces. (The conversion findings: test_weights.py
and test_volumes.py.)"""
import asyncio
import csv
import io
import os
import sqlite3
import time
import uuid
import zipfile

import pytest

from .test_restore import H, _check, _recipe


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.fixture
def after_restart():
    """What a restart does after a test leaves a restore unfinished: the
    flags go (a new process) and start-up recovery and init_db run; the
    old-photo copies the test made are removed (other tests count them)."""
    from app import restore as restore_mod
    from app.database import DATA_DIR, UPLOADS_DIR, engine
    before = restore_mod.set_aside(UPLOADS_DIR)
    yield
    from app.init_db import init_db
    from app.routes import common
    common._restore_incomplete.clear()
    common._restoring.clear()
    restore_mod.recover_interrupted_restore(DATA_DIR, UPLOADS_DIR)
    staged = UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX
    if os.path.isdir(staged):                     # no marker: left for a person; here, put in place
        restore_mod.mark_database_replaced(DATA_DIR)
        restore_mod.recover_interrupted_restore(DATA_DIR, UPLOADS_DIR)
    restore_mod.clear_marker(DATA_DIR)
    engine.dispose()
    init_db()
    import shutil
    for name in restore_mod.set_aside(UPLOADS_DIR) - before:
        shutil.rmtree(os.path.join(os.path.dirname(UPLOADS_DIR.rstrip("/")), name), ignore_errors=True)


def _backup_with_a_photo_then_lose_it(client):
    """A backup holding a recipe with a photo; then that recipe is deleted
    for good, so the photo is only in the backup."""
    kept = _recipe(client, f"Fail keep {uuid.uuid4().hex[:6]}", photo=True)
    backup = client.get("/api/backup/database.zip").content
    client.delete(f"/api/recipes/{kept['id']}", headers=H)
    client.delete(f"/api/recently-deleted/{kept['id']}", headers=H)
    assert client.get(f"/uploads/{kept['image_path']}").status_code == 404
    return kept, _check(client, backup).json()["token"]


def _fail(times):
    """A stand-in that raises the first `times` calls (None: always)."""
    calls = {"n": 0}

    def wrap(real):
        def f(*a, **k):
            calls["n"] += 1
            if times is None or calls["n"] <= times:
                raise OSError("injected failure")
            return real(*a, **k)
        return f
    return wrap, calls


@pytest.mark.parametrize("step", ["init_db", "put_back", "swap", "swap_half", "mark"])
def test_a_step_that_fails_once_is_retried_and_the_restore_finishes(client, monkeypatch, step, after_restart):
    from app import restore as restore_mod
    from app.database import DATA_DIR, UPLOADS_DIR
    from app.routes import backup as routes_backup
    kept, token = _backup_with_a_photo_then_lose_it(client)
    wrap, calls = _fail(1)
    if step == "init_db":
        monkeypatch.setattr(routes_backup, "init_db", wrap(routes_backup.init_db))
    elif step == "put_back":
        monkeypatch.setattr(routes_backup, "_put_back_rows", wrap(routes_backup._put_back_rows))
    elif step == "mark":
        monkeypatch.setattr(restore_mod, "mark_database_replaced", wrap(restore_mod.mark_database_replaced))
    elif step == "swap":
        monkeypatch.setattr(restore_mod, "swap_dirs", wrap(restore_mod.swap_dirs))
    else:
        # Stopped between the two renames: the old photos aside, the new
        # ones not yet in place.
        real = restore_mod.swap_dirs
        state = {"n": 0}

        def half(new_dir, live_dir):
            state["n"] += 1
            if state["n"] == 1:
                os.rename(live_dir, f"{live_dir}-before-restore-{uuid.uuid4().hex[:8]}")
                raise OSError("injected failure between the renames")
            return real(new_dir, live_dir)
        monkeypatch.setattr(restore_mod, "swap_dirs", half)
    r = client.post("/api/backup/restore", headers=H, json={"token": token})
    assert r.status_code == 200, r.text
    if step != "swap_half":
        assert calls["n"] >= 2
    back = client.get(f"/api/recipes/{kept['id']}").json()
    assert client.get(f"/uploads/{back['image_path']}").status_code == 200
    assert not os.path.exists(os.path.join(DATA_DIR, restore_mod.MARKER))
    assert not os.path.isdir(UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX)


@pytest.mark.parametrize("step", ["init_db", "put_back", "swap", "mark"])
def test_a_restore_that_cant_finish_keeps_the_app_closed(client, monkeypatch, step, after_restart):
    from app import restore as restore_mod
    from app.database import DATA_DIR, UPLOADS_DIR
    from app.main import picker_app
    from app.routes import backup as routes_backup, common, email as email_routes
    from fastapi.testclient import TestClient
    kept, token = _backup_with_a_photo_then_lose_it(client)
    wrap, _ = _fail(None)
    target = {"init_db": (routes_backup, "init_db"), "put_back": (routes_backup, "_put_back_rows"),
              "swap": (restore_mod, "swap_dirs"), "mark": (restore_mod, "mark_database_replaced")}[step]
    monkeypatch.setattr(*target, wrap(getattr(*target)))
    r = client.post("/api/backup/restore", headers=H, json={"token": token})
    assert r.status_code == 500 and "Restart the app" in r.json()["detail"], r.text
    # Neither port serves the half-restored library; /healthz still answers.
    for c in (client, TestClient(picker_app)):
        got = c.get("/api/recipes" if c is client else "/api/picker/options")
        assert got.status_code == 503 and "Restart the app" in got.json()["detail"]
        assert c.get("/healthz").status_code == 200
    assert client.post("/api/backup/restore", headers=H, json={"token": token}).status_code == 503
    # Nothing else runs on it meanwhile.
    assert common._restoring.is_set()
    with pytest.raises(Exception, match="restore"):
        routes_backup.run_scheduled_backup()
    assert "being restored" in email_routes.run_email_scan()["messages"][0]
    staged = UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX
    assert os.path.isdir(staged)
    # With the marker, start-up finishes it; without (the marker itself
    # couldn't be written), start-up leaves the folders for a person.
    assert os.path.exists(os.path.join(DATA_DIR, restore_mod.MARKER)) == (step != "mark")
    monkeypatch.undo()
    if step != "mark":
        restore_mod.recover_interrupted_restore(DATA_DIR, UPLOADS_DIR)
        assert not os.path.isdir(staged)
        common._restore_incomplete.clear()
        common._restoring.clear()
        back = client.get(f"/api/recipes/{kept['id']}").json()
        assert client.get(f"/uploads/{back['image_path']}").status_code == 200


def test_the_sweep_leaves_uploads_alone_during_a_restore(client):
    from app import main
    from app.database import UPLOADS_DIR
    from app.routes import common
    name = f"orphan-{uuid.uuid4().hex}.jpg"
    path = os.path.join(UPLOADS_DIR, name)
    open(path, "wb").write(b"x")
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    common._restoring.set()
    try:
        asyncio.run(main._run_sweeps())
        assert os.path.exists(path)
    finally:
        common._restoring.clear()
    asyncio.run(main._run_sweeps())
    assert not os.path.exists(path)                # and swept once it's over


def test_a_full_safety_copy_is_marked_full(tmp_path, client):
    """An unconfigured server's safety copy has every redacted field blank,
    like a blanked backup: only its mark tells them apart."""
    from app import backup
    full, blanked = tmp_path / "full.zip", tmp_path / "blanked.zip"
    backup.build_database_backup(str(full), blank=False)
    backup.build_database_backup(str(blanked))
    for path, want in ((full, False), (blanked, True)):
        db = tmp_path / f"{path.stem}.db"
        db.write_bytes(zipfile.ZipFile(path).read("recipes.db"))
        assert backup.settings_blanked(str(db)) is want, path.name
    # An unmarked one (from before 0156) with every field blank: still read
    # as blanked, as before.
    db = tmp_path / "full.db"
    con = sqlite3.connect(db)
    con.execute(f"DROP TABLE {backup.REDACTED_MARK_TABLE}")
    con.commit()
    con.close()
    blank = backup.REDACTED_SETTINGS["email_ingest_settings"]
    con = sqlite3.connect(db)
    row = con.execute(f"SELECT {', '.join(blank)} FROM email_ingest_settings").fetchone()
    con.close()
    assert backup.settings_blanked(str(db)) is (row is None or all(v == blank[k] for k, v in zip(blank, row)))


def test_the_safety_copy_says_the_cpanel_login_isnt_in_it():
    from app import backup
    for lang in ("en", "fr"):
        text = backup.restore_instructions(lang, blank=False)
        assert "cpanel-login.json" in text and "jeton cPanel, chiffr" not in text
        assert "cPanel token\nincluded" not in text and "every setting" not in text


@pytest.mark.parametrize("title", ["=1+1", "\n=1+1", "＝1+1", "＋cmd", "－1", "＠SUM(1)", "  =1+1", "\t=1", "\r=1"])
def test_index_csv_titles_cant_become_formulas(title):
    from app import backup
    assert backup._csv_safe(title).startswith("'")
    out = io.StringIO()
    csv.writer(out).writerow([backup._csv_safe(title)])
    assert next(csv.reader(io.StringIO(out.getvalue())))[0].startswith("'")


@pytest.mark.parametrize("title", ["Lasagna", "1-2-3 cake", "Crêpes", "", "Sauce: tomato"])
def test_ordinary_titles_are_left_alone(title):
    from app import backup
    assert backup._csv_safe(title) == title
