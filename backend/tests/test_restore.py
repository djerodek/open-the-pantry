"""Restore from backup in Settings (backlog item 3, app/restore.py).

The test session shares one database with every other test, so the
restores here put back a backup of that same database (taken at the start
of each test), and check what changed in between."""
import asyncio
import io
import json
import os
import sqlite3
import time
import uuid
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .conftest import TINY_PNG
from .test_backup_settings import configured  # noqa: F401  (fixture)

H = {"X-Requested-With": "OpenThePantry"}


def _check(client, data, name="backup.zip"):
    return client.post("/api/backup/restore/check", headers=H, files={"file": (name, data, "application/zip")})


def _rezip(data: bytes, change_db=None, extra=None, drop=()):
    """The backup with its database changed by change_db(sqlite connection)
    and extra entries added."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename in drop:
                continue
            body = src.read(info.filename)
            if info.filename == "recipes.db" and change_db:
                path = f"/tmp/restore-test-{uuid.uuid4().hex}.db"
                with open(path, "wb") as f:
                    f.write(body)
                con = sqlite3.connect(path)
                change_db(con)
                con.commit()
                con.close()
                body = open(path, "rb").read()
                os.remove(path)
            dst.writestr(info.filename, body)
        for name, body in (extra or {}).items():
            dst.writestr(name, body)
    return out.getvalue()


def _recipe(client, title, photo=False):
    image = None
    if photo:
        image = client.post("/api/upload-image", headers=H,
                            files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]
    r = client.post("/api/recipes", headers=H, json={"title": title, "source_type": "manual", "image_path": image,
                                                     "ingredients": [{"raw_line": "1 egg"}], "steps": ["Boil."], "tags": []})
    assert r.status_code == 200, r.text
    return r.json()


def test_check_tells_a_backup_from_an_import(client):
    assert _check(client, b'{"@type": "Recipe"}', "r.json").json() == {"kind": "import"}
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("recipes/a.json", "{}")
    assert _check(client, out.getvalue()).json() == {"kind": "import"}
    assert _check(client, b"PK\x03\x04 nope").status_code == 422
    backup = client.get("/api/backup/database.zip").content
    info = _check(client, backup).json()
    assert info["kind"] == "backup" and len(info["token"]) == 32 and info["created_at"]
    assert info["recipes"] == info["current_recipes"]
    # Not this app's database, or a damaged one: refused before anything happens.
    other = _rezip(backup, change_db=lambda c: c.execute("DROP TABLE steps"))
    assert "isn't Open the Pantry's" in _check(client, other).json()["detail"]
    with zipfile.ZipFile(io.BytesIO(backup)) as zf:
        db = bytearray(zf.read("recipes.db"))
    db[0:16] = b"not a database!!"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("recipes.db", bytes(db))
    assert "can't be opened" in _check(client, out.getvalue()).json()["detail"]
    broken = backup.replace(b"SQLite format 3", b"SQLite format X", 1)      # bad CRC in the zip
    if broken != backup:
        assert _check(client, broken).status_code == 422


def test_restore_puts_the_backup_back(client):
    from app import main
    from app.database import DATA_DIR
    kept = _recipe(client, f"Restore keep {uuid.uuid4().hex[:6]}", photo=True)
    backup = client.get("/api/backup/database.zip").content
    later = _recipe(client, f"Restore later {uuid.uuid4().hex[:6]}")
    client.delete(f"/api/recipes/{kept['id']}", headers=H)
    client.delete(f"/api/recently-deleted/{kept['id']}", headers=H)        # for good (0109)
    assert client.get(f"/uploads/{kept['image_path']}").status_code == 404

    # A backup whose email password this server can't read, a bad file
    # name and a non-image under a photo's name, and no picker table (an
    # older backup).
    def change(con):
        con.execute("DELETE FROM email_ingest_settings")
        con.execute("INSERT INTO email_ingest_settings (id, enabled, imap_port, imap_use_ssl, smtp_port, smtp_use_tls, "
                    "subject_keyword, allowed_senders, daily_scan_hour, cooldown_minutes, language, recent_recipients, "
                    "password_encrypted) VALUES (1, 1, 993, 1, 587, 1, '[RECIPE]', '', 3, 30, 'en', '[]', 'not-a-token')")
        con.execute("DROP TABLE IF EXISTS picker_settings")
        # As an older backup, which carried its email settings (from 0143
        # a backup leaves them out and says so in this table).
        con.execute("DROP TABLE IF EXISTS backup_settings_blanked")
    fake = f"img-{uuid.uuid4().hex}.jpg"
    data = _rezip(backup, change, extra={f"uploads/{fake}": b"<html>not an image</html>",
                                          "uploads/../escape.jpg": TINY_PNG})
    info = _check(client, data).json()
    assert info["files_left_out"] == 1          # the ../ name; the fake image is caught when unpacked
    r = client.post("/api/backup/restore", headers=H, json={"token": info["token"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email_password_cleared"] is True
    assert os.path.isfile(os.path.join(DATA_DIR, body["safety_backup"]))
    titles = {x["title"] for x in client.get("/api/recipes").json()}
    assert kept["title"] in titles and later["title"] not in titles
    back = client.get(f"/api/recipes/{kept['id']}").json()
    assert client.get(f"/uploads/{back['image_path']}").status_code == 200
    assert client.get(f"/uploads/{fake}").status_code == 404
    assert not os.path.exists(os.path.join(DATA_DIR, "escape.jpg"))
    assert client.get("/api/picker/status").json()["on"] is False          # table recreated, off
    es = client.get("/api/email-settings").json()
    assert es["enabled"] is False and es["password_set"] is False
    # The same token can't be used twice.
    assert client.post("/api/backup/restore", headers=H, json={"token": info["token"]}).status_code == 404
    assert client.post("/api/backup/restore", headers=H, json={"token": "../../x"}).status_code == 422
    # The safety backup restores what was there before.
    safety = open(os.path.join(DATA_DIR, body["safety_backup"]), "rb").read()
    tok = _check(client, safety).json()["token"]
    assert client.post("/api/backup/restore", headers=H, json={"token": tok}).status_code == 200
    titles = {x["title"] for x in client.get("/api/recipes").json()}
    assert later["title"] in titles and kept["title"] not in titles
    client.delete(f"/api/recipes/{later['id']}", headers=H)


def test_restore_keeps_this_servers_https_settings(client):
    from sqlalchemy import text

    from app.database import engine
    with engine.begin() as c:
        c.execute(text("DELETE FROM https_settings"))
        c.execute(text("INSERT INTO https_settings (id, enabled, domain, public_port, state) "
                       "VALUES (1, 0, 'here.example.com', 8443, 'off')"))
    backup = _rezip(client.get("/api/backup/database.zip").content,
                    change_db=lambda con: con.execute("UPDATE https_settings SET domain = 'backup.example.com'"))
    tok = _check(client, backup).json()["token"]
    assert client.post("/api/backup/restore", headers=H, json={"token": tok}).status_code == 200
    with engine.connect() as c:
        assert c.execute(text("SELECT domain FROM https_settings")).scalar() == "here.example.com"
        c.execute(text("DELETE FROM https_settings"))
        c.commit()


def test_restore_needs_the_settings_password(client):
    from app.main import app
    stranger = TestClient(app)
    backup = client.get("/api/backup/database.zip").content
    # Checking a full backup needs it too (round 10): the page asks and resends.
    r = _check(stranger, backup)
    assert r.status_code == 401 and r.json().get("locked")
    # An import file is still checked without it.
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("recipes/a.json", "{}")
    assert _check(stranger, out.getvalue()).json() == {"kind": "import"}
    tok = _check(client, backup).json()["token"]
    r = stranger.post("/api/backup/restore", headers=H, json={"token": tok})
    assert r.status_code == 401 and r.json().get("locked")


def test_other_requests_wait_while_restoring(client):
    from app import main
    from app.main import picker_app
    main._restoring.set()
    try:
        r = client.get("/api/recipes")
        assert r.status_code == 503 and r.headers["retry-after"] == "30"
        assert TestClient(picker_app).get("/api/picker/options").status_code == 503
        assert client.get("/healthz").status_code == 200
    finally:
        main._restoring.clear()
    assert client.get("/api/recipes").status_code == 200


ROOT = Path(__file__).resolve().parents[2]


def _restore_check(c, data):
    return c.post("/api/backup/restore/check", headers=H, files={"file": ("b.zip", data, "application/zip")})


def _zip(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return out.getvalue()


def _layout(tmp_path):
    live, staged = tmp_path / "uploads", tmp_path / "uploads.restore-new"
    live.mkdir()
    (live / "img-old.jpg").write_bytes(b"old")
    staged.mkdir()
    (staged / "img-restored.jpg").write_bytes(b"new")
    return live, staged


def _zip_stored(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return out.getvalue()


def _png(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _multipart(name, content, boundary="b0119"):
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _send_chunked(path, chunks, content_type, cookie=None):
    """Drives BodySizeLimit directly, the body in chunks with no
    Content-Length: returns (status, body, bytes the inner app received)."""
    from app.main import BodySizeLimit
    received = {"n": 0}

    async def inner(scope, receive, send):
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                break
            received["n"] += len(m.get("body", b""))
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})

    pending = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    out = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(m):
        out.append(m)

    headers = [(b"content-type", content_type.encode()), (b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b"",
             "client": ("127.0.0.1", 1), "server": ("testserver", 80), "scheme": "http", "root_path": ""}
    asyncio.run(BodySizeLimit(inner)(scope, receive, send))
    status = next(m["status"] for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return status, body, received["n"]


def _rice_recipe(client, title, tags=()):
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual",
                                          "ingredients": [{"raw_line": "1 cup rice"}], "steps": ["Cook."],
                                          "tags": [{"name": t, "category": "custom"} for t in tags]})
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(client, *ids):
    for rid in ids:
        client.delete(f"/api/recipes/{rid}")
        client.delete(f"/api/recently-deleted/{rid}")


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


def test_restore_instructions_dont_ask_for_encryption_key():
    from app.backup import RESTORE_INSTRUCTIONS, RESTORE_INSTRUCTIONS_FR
    assert "copy encryption.key" not in RESTORE_INSTRUCTIONS
    assert "copiez encryption.key" not in RESTORE_INSTRUCTIONS_FR


def test_manual_restore_deletes_the_wal_first():
    from app.backup import RESTORE_INSTRUCTIONS, RESTORE_INSTRUCTIONS_FR
    rm = "rm -f ./data/recipes.db-wal ./data/recipes.db-shm"
    for text in (RESTORE_INSTRUCTIONS, RESTORE_INSTRUCTIONS_FR, (ROOT / "frontend/index.html").read_text(encoding="utf-8")):
        assert rm in text
        # Deleted before the unzip that follows it.
        assert "unzip -o open-the-pantry-backup-" in text[text.index(rm):]


def test_a_restore_failing_before_the_copy_removes_its_staged_photos(client, monkeypatch):
    from app import main
    from app import restore as restore_mod
    from app.database import UPLOADS_DIR
    _recipe(client, f"L1 {uuid.uuid4().hex[:6]}", photo=True)
    token = _check(client, client.get("/api/backup/database.zip").content).json()["token"]

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(restore_mod, "copy_into_live", full)
    r = client.post("/api/backup/restore", headers=H, json={"token": token})
    assert r.status_code == 500 and "Restart" not in r.json()["detail"]
    assert not os.path.isdir(UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX)
    assert client.get("/api/recipes").status_code == 200


def test_a_failure_before_staging_leaves_an_earlier_staged_folder(client, monkeypatch):
    """Only the folder this restore staged is removed: one an earlier,
    interrupted restore left for a person stays when this one fails before
    staging (here, writing its safety copy)."""
    from app import restore as restore_mod
    from app.database import UPLOADS_DIR
    from app.routes import backup as routes_backup
    staged = UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX
    os.makedirs(staged, exist_ok=True)
    token = _check(client, client.get("/api/backup/database.zip").content).json()["token"]

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(routes_backup, "build_database_backup", full)
    try:
        r = client.post("/api/backup/restore", headers=H, json={"token": token})
        assert r.status_code == 500
        assert os.path.isdir(staged)
    finally:
        os.rmdir(staged)


def test_a_second_check_leaves_the_first_waiting(client):
    from app import main, routes
    backup = client.get("/api/backup/database.zip").content
    first = _restore_check(client, backup).json()["token"]
    second = _restore_check(client, backup).json()["token"]
    for tok in (first, second):
        assert all(os.path.isfile(p) for p in routes.backup._pending_restore_paths(tok))
    # A stale one (over 30 minutes) goes on the next check.
    old = time.time() - routes.backup.PENDING_RESTORE_SECONDS - 60
    for p in routes.backup._pending_restore_paths(first):
        os.utime(p, (old, old))
    _restore_check(client, backup)
    assert not any(os.path.isfile(p) for p in routes.backup._pending_restore_paths(first))
    assert all(os.path.isfile(p) for p in routes.backup._pending_restore_paths(second))
    routes.backup._clear_pending_restores(None)


def test_a_failed_check_removes_only_its_own_files(client):
    from app import main, routes
    backup = client.get("/api/backup/database.zip").content
    waiting = _restore_check(client, backup).json()["token"]
    assert _restore_check(client, _zip({"recipes.db": b"not a database"})).status_code == 422
    assert all(os.path.isfile(p) for p in routes.backup._pending_restore_paths(waiting))
    routes.backup._clear_pending_restores(None)


def test_without_the_password_uploads_stop_at_the_import_limit(client, monkeypatch):
    from app import main, routes
    stranger = TestClient(main.app)
    monkeypatch.setattr(routes.backup, "MAX_IMPORT_BYTES", 1000)
    r = _restore_check(stranger, b"PK" + b"\0" * 5000)
    assert r.status_code == 401 and r.json()["locked"]           # only a backup could be that big
    assert _restore_check(client, b"PK" + b"\0" * 5000).status_code == 422  # with it: checked (and not a zip)
    assert not [n for n in os.listdir(main.BACKUP_TMP_DIR) if n.startswith("restore-")]


def test_a_restore_forgets_the_guesses(monkeypatch):
    from app import picker
    picker._guessed[999_999] = ((1, 1), "fr")
    picker.forget_languages()
    assert picker._guessed == {}


def test_an_interrupted_restore_is_finished(tmp_path):
    """DeepSeek review of 0144: the database is already the backup's when
    the folders are swapped, so the staged photos go into place, not the
    old ones (0144 moved the old ones back)."""
    from app.restore import recover_interrupted_restore
    live = tmp_path / "uploads"
    old = tmp_path / "uploads-before-restore-20261101-101010"
    staged = tmp_path / "uploads.restore-new"
    old.mkdir()
    (old / "img-old.jpg").write_bytes(b"x")
    staged.mkdir()
    (staged / "img-restored.jpg").write_bytes(b"y")
    live.mkdir()                                           # recreated empty at import
    from app.restore import mark_database_replaced
    mark_database_replaced(str(tmp_path))                  # written after the database copy (0146)
    assert recover_interrupted_restore(str(tmp_path), str(live)) == str(staged)
    assert (live / "img-restored.jpg").exists() and not staged.exists()
    assert (old / "img-old.jpg").exists()                  # left, named in the log


def test_after_recovery_the_restored_recipes_photo_resolves(client):
    """The same, through the app: a recipe as the restored database has it,
    its photo only in the staged folder, uploads/ empty as after a crash."""
    import shutil

    from app import restore
    from app.database import DATA_DIR, UPLOADS_DIR
    from app.restore import STAGED_SUFFIX, recover_interrupted_restore
    from tests.conftest import TINY_PNG
    stored = client.post("/api/upload-image", headers=H, files={"file": ("p.png", TINY_PNG, "image/png")}).json()
    r = client.post("/api/recipes", headers=H, json={"title": "Restored with photo", "source_type": "manual",
                                                     "image_path": stored["stored_file"],
                                                     "ingredients": [{"raw_line": "1 egg"}], "steps": ["Boil."]}).json()
    photo = r["image_path"]
    live = UPLOADS_DIR.rstrip("/")
    before, staged = live + "-before-restore-20261101-101010", live + STAGED_SUFFIX
    try:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(live, staged)                       # the backup's photos, unpacked
        os.remove(os.path.join(live, photo))                # not among the photos from before
        os.rename(live, before)                             # swap_dirs' first rename...
        os.makedirs(live)                                   # ...then the crash; recreated empty at import
        restore.mark_database_replaced(DATA_DIR)            # as restore_backup wrote it before the swap
        assert client.get(f"/uploads/{photo}").status_code == 404
        assert recover_interrupted_restore(DATA_DIR, UPLOADS_DIR) == staged
        got = client.get(f"/api/recipes/{r['id']}").json()["image_path"]
        assert client.get(f"/uploads/{got}").status_code == 200
    finally:
        if os.path.isdir(before):
            shutil.rmtree(before)
        if os.path.isdir(staged):
            shutil.rmtree(staged)
        client.delete(f"/api/recipes/{r['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{r['id']}", headers=H)


@pytest.mark.parametrize("setup", ["two left, uploads empty", "nothing left over",
                                   "old folder, uploads not empty, nothing staged", "one left, uploads empty, no marker"])
def test_otherwise_nothing_is_moved(tmp_path, setup):
    from app.restore import recover_interrupted_restore
    live = tmp_path / "uploads"
    live.mkdir()
    if setup != "two left, uploads empty":
        (live / "img-2.jpg").write_bytes(b"y")
    if setup != "nothing left over":
        (tmp_path / "uploads-before-restore-20261101-101010").mkdir()
    if setup == "two left, uploads empty":
        (tmp_path / "uploads-before-restore-20261101-111111").mkdir()
    if setup in ("two left, uploads empty", "one left, uploads empty, no marker"):
        (tmp_path / "uploads.restore-new").mkdir()
        (tmp_path / "uploads.restore-new" / "img-restored.jpg").write_bytes(b"new")
    if setup == "one left, uploads empty, no marker":
        (live / "img-2.jpg").unlink()
    assert recover_interrupted_restore(str(tmp_path), str(live)) is None
    assert live.is_dir()
    if "uploads empty" in setup:
        # GPT review of 0147: without a marker this state is ambiguous; the
        # staged photos stay for the owner.
        assert (tmp_path / "uploads.restore-new" / "img-restored.jpg").exists() and not os.listdir(live)


def test_stopped_after_the_database_copy_the_photos_follow(tmp_path):
    """DeepSeek review of 0145: stopped after the database was replaced but
    before the swap began, uploads/ still had the old photos and the staged
    ones were never moved. The marker says the database was replaced."""
    from app import restore
    live, staged = _layout(tmp_path)
    restore.mark_database_replaced(str(tmp_path))
    assert restore.recover_interrupted_restore(str(tmp_path), str(live)) == str(staged)
    assert (live / "img-restored.jpg").exists() and not staged.exists()
    old = [p for p in tmp_path.iterdir() if p.name.startswith("uploads-before-restore-")]
    assert len(old) == 1 and (old[0] / "img-old.jpg").exists()
    assert not (tmp_path / restore.MARKER).exists()


def test_staged_photos_without_a_marker_are_left_for_the_owner(tmp_path, monkeypatch):
    """No marker: usually stopped before the database copy, but a stop just
    after it, before the marker, looks the same, so the staged photos
    aren't deleted (DeepSeek review of 0146; 0146 deleted them)."""
    from app import restore
    warned = []
    monkeypatch.setattr(restore.log, "warning", lambda msg, *a, **k: warned.append(msg % a))
    live, staged = _layout(tmp_path)
    assert restore.recover_interrupted_restore(str(tmp_path), str(live)) is None
    assert (live / "img-old.jpg").exists() and (staged / "img-restored.jpg").exists()
    assert any("uploads.restore-new" in w and "no marker" in w for w in warned)


def test_the_restore_marks_the_database_and_clears_the_mark(client, monkeypatch):
    """Through a real restore: the marker exists from the database copy to
    the swap, and not after."""
    from app import restore
    from app.database import DATA_DIR
    seen = {}
    real_swap = restore.swap_dirs

    def spy(new, live):
        seen["marked during swap"] = os.path.exists(os.path.join(DATA_DIR, restore.MARKER))
        return real_swap(new, live)
    monkeypatch.setattr(restore, "swap_dirs", spy)
    data = client.get("/api/backup/database.zip").content
    tok = client.post("/api/backup/restore/check", headers=H,
                      files={"file": ("b.zip", data, "application/zip")}).json()["token"]
    assert client.post("/api/backup/restore", headers=H, json={"token": tok}).status_code == 200
    assert seen == {"marked during swap": True}
    assert not os.path.exists(os.path.join(DATA_DIR, restore.MARKER))


def test_an_oversized_manifest_is_not_read(client, tmp_path, monkeypatch):
    from app import restore
    backup = client.get("/api/backup/database.zip").content
    with zipfile.ZipFile(io.BytesIO(backup)) as zf:
        db = zf.read("recipes.db")
    monkeypatch.setattr(restore, "MAX_MANIFEST_BYTES", 100)
    path = tmp_path / "b.zip"
    path.write_bytes(_zip_stored({"recipes.db": db, "manifest.json": json.dumps({"created_at": "x", "pad": "y" * 500})}))
    read = []
    real = zipfile.ZipFile.read
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda self, n, *a: read.append(getattr(n, "filename", n)) or real(self, n, *a))
    info = restore.inspect(str(path), str(tmp_path / "b.db"))
    assert info["created_at"] is None and "manifest.json" not in read
    path.write_bytes(_zip_stored({"recipes.db": db, "manifest.json": json.dumps({"created_at": "2026-10-17"})}))
    assert restore.inspect(str(path), str(tmp_path / "c.db"))["created_at"] == "2026-10-17"


def test_restored_files_meet_the_upload_standard(tmp_path):
    from PIL import Image

    from app import restore
    clean = _png(tmp_path, "img-a.png", TINY_PNG)
    assert restore._content_ok(clean, "img-a.png") and open(clean, "rb").read() == TINY_PNG   # kept as is
    # A JPEG made the way uploads are, kept byte for byte.
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, "JPEG", quality=90)
    jpg = _png(tmp_path, "img-b.jpg", buf.getvalue())
    assert restore._content_ok(jpg, "img-b.jpg") and open(jpg, "rb").read() == buf.getvalue()
    # Trailing content after the image: re-encoded, which drops it.
    poly = _png(tmp_path, "img-c.png", TINY_PNG + b"<script>alert(1)</script>")
    assert restore._content_ok(poly, "img-c.png")
    data = open(poly, "rb").read()
    assert b"<script>" not in data and data.endswith(b"IEND\xaeB`\x82")
    # Wrong type for the name, or not an image at all.
    assert not restore._content_ok(_png(tmp_path, "img-d.jpg", TINY_PNG), "img-d.jpg")
    assert not restore._content_ok(_png(tmp_path, "img-e.jpg", b"%PDF-1.4 x"), "img-e.jpg")
    assert restore._content_ok(_png(tmp_path, "doc-f.pdf", b"%PDF-1.4 x"), "doc-f.pdf")
    assert not restore._content_ok(_png(tmp_path, "img-g.png", TINY_PNG[:30] + b"\x00" * 40), "img-g.png")


def test_restored_photos_with_metadata_are_cleaned(tmp_path):
    """GPT review of 0095: a JPEG ending cleanly kept its EXIF (camera,
    possibly location), which an upload would have dropped."""
    from PIL import Image, PngImagePlugin

    from app import restore
    exif = Image.Exif()
    exif[0x010E] = "kitchen photo"
    exif[0x013B] = "Someone"
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 100, 50)).save(buf, "JPEG", exif=exif.tobytes(), comment=b"hello")
    jpg = _png(tmp_path, "img-h.jpg", buf.getvalue())
    assert restore._content_ok(jpg, "img-h.jpg")
    with Image.open(jpg) as im:
        assert "exif" not in im.info and "comment" not in im.info and restore._metadata(im) == []
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Author", "Someone")
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, "PNG", pnginfo=meta, icc_profile=b"\0" * 128)
    png = _png(tmp_path, "img-i.png", buf.getvalue())
    with Image.open(png) as im:
        im.load()
        assert set(restore._metadata(im)) >= {"icc_profile", "text"}
    assert restore._content_ok(png, "img-i.png")
    with Image.open(png) as im:
        im.load()
        assert restore._metadata(im) == []


def test_restore_check_without_the_password_stops_at_the_import_limit(client, monkeypatch):
    """Without the password the box only imports (1 GB); past that the page
    gets the locked answer, asks for the password and resends."""
    from app import main, routes
    monkeypatch.setattr(routes.backup, "MAX_IMPORT_BYTES", 2000)
    monkeypatch.setattr(main, "MULTIPART_MARGIN_BYTES", 0)
    body, ctype = _multipart("backup.zip", b"PK" + b"x" * 5000)
    chunks = [body[i:i + 1000] for i in range(0, len(body), 1000)]
    status, answer, seen = _send_chunked("/api/backup/restore/check", chunks, ctype)
    assert status == 401 and json.loads(answer)["locked"] is True and seen <= 2000
    # With the password: the full-backup limit.
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    assert main._multipart_limit({"path": "/api/backup/restore/check", "type": "http",
                                  "headers": [(b"cookie", cookie.encode())]})[0] == main.restore_mod.MAX_TOTAL_BYTES
    status, _, seen = _send_chunked("/api/backup/restore/check", chunks, ctype, cookie=cookie)
    assert status == 200 and seen == len(body)


def test_restore_forgets_searchs_word_list(client):
    """Restore isn't one of the changes that refreshed it: the restored
    library was searched with the old one's words for up to 30 s."""
    backup = client.get("/api/backup/database.zip").content
    r = _rice_recipe(client, "Plimbertoast")
    # The list as it is now, with "plimbertoast" in it, cached.
    found = client.get("/api/recipes", params={"q": "plimbertoasr"})
    assert "plimbertoasr" in json.loads(found.headers.get("X-Search-Corrected", "{}"))
    tok = client.post("/api/backup/restore/check", headers=H,
                      files={"file": ("b.zip", backup, "application/zip")}).json()["token"]
    assert client.post("/api/backup/restore", headers=H, json={"token": tok}).status_code == 200
    # Restored: no Plimbertoast, so nothing to correct to.
    found = client.get("/api/recipes", params={"q": "plimbertoasr"})
    assert found.status_code == 200 and found.json() == []
    assert "X-Search-Corrected" not in found.headers
    _cleanup(client, r["id"])


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
    from fastapi.testclient import TestClient

    from app import restore as restore_mod
    from app.database import DATA_DIR, UPLOADS_DIR
    from app.main import picker_app
    from app.routes import backup as routes_backup
    from app.routes import common
    from app.routes import email as email_routes
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


def test_no_photo_sweep_while_staged_photos_wait(client):
    from app import main
    from app import restore as restore_mod
    from app.database import UPLOADS_DIR
    staged = UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX
    path = os.path.join(UPLOADS_DIR, f"old-{uuid.uuid4().hex}.jpg")
    open(path, "wb").write(b"x")
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    os.makedirs(staged)
    try:
        asyncio.run(main._run_sweeps())
        assert os.path.exists(path)
    finally:
        os.rmdir(staged)
    asyncio.run(main._run_sweeps())
    assert not os.path.exists(path)


def test_an_unfinished_restore_keeps_this_servers_settings_after_a_restart(client, configured, monkeypatch, after_restart):
    """Claude review of 0165: the settings a restore keeps (automatic-backup
    schedule, email login, HTTPS) were only in memory, so a restore that
    stopped after the copy lost them at the restart: backups off, no login,
    the backup's HTTPS. They're written to restore-kept.json first and put
    back at start-up."""
    import stat

    from sqlalchemy import text

    from app import restore as restore_mod
    from app.database import DATA_DIR, UPLOADS_DIR, engine
    from app.init_db import init_db
    from app.routes import backup as routes_backup
    from app.routes import common
    with engine.begin() as c:
        schedule_before = [dict(r) for r in c.execute(text("SELECT * FROM backup_schedule")).mappings()]
        c.execute(text("DELETE FROM backup_schedule"))
    token = _check(client, client.get("/api/backup/database.zip").content).json()["token"]   # backups off, blanked
    with engine.begin() as c:
        c.execute(text("INSERT INTO backup_schedule (id, enabled, hour, keep) VALUES (1, 1, 2, 14)"))
    kept = os.path.join(DATA_DIR, routes_backup.KEPT_FILE)
    try:
        wrap, _ = _fail(None)
        monkeypatch.setattr(routes_backup, "init_db", wrap(routes_backup.init_db))
        r = client.post("/api/backup/restore", headers=H, json={"token": token})
        assert r.status_code == 500 and "Restart the app" in r.json()["detail"], r.text
        assert stat.S_IMODE(os.stat(kept).st_mode) == 0o600
        # The restart: a new process (flags gone), init_db, then start-up.
        monkeypatch.undo()
        common._restore_incomplete.clear()
        common._restoring.clear()
        engine.dispose()
        init_db()
        assert routes_backup.put_back_after_restart() is True
        restore_mod.recover_interrupted_restore(DATA_DIR, UPLOADS_DIR)
        assert not os.path.exists(kept)
        with engine.connect() as c:
            assert tuple(c.execute(text("SELECT enabled, hour, keep FROM backup_schedule")).one()) == (1, 2, 14)
            assert c.execute(text("SELECT username FROM email_ingest_settings")).scalar() == "cook@home.example"
            assert c.execute(text("SELECT domain FROM https_settings")).scalar() == "pantry.home.example"
        assert routes_backup.put_back_after_restart() is False      # once only
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM backup_schedule"))
            for row in schedule_before:
                c.execute(text(f"INSERT INTO backup_schedule ({', '.join(row)}) VALUES "
                               f"({', '.join(':' + k for k in row)})"), row)


def test_the_kept_settings_file_goes_when_a_restore_finishes_or_never_starts(client):
    from app.database import DATA_DIR
    from app.routes import backup as routes_backup
    kept = os.path.join(DATA_DIR, routes_backup.KEPT_FILE)
    data = client.get("/api/backup/database.zip").content
    r = client.post("/api/backup/restore", headers=H, json={"token": _check(client, data).json()["token"]})
    assert r.status_code == 200 and not os.path.exists(kept)


def test_kept_rows_round_trip_through_the_file(tmp_path, monkeypatch):
    """Values SQLite hands back (text, numbers, None, bytes) come back the
    same from restore-kept.json."""
    from app.routes import backup as routes_backup
    monkeypatch.setattr(routes_backup, "DATA_DIR", str(tmp_path))
    rows = {"backup_schedule": [{"id": 1, "enabled": 1, "hour": 2, "keep": 14, "last": None}],
            "https_settings": [{"id": 1, "domain": "pantry.home.example", "blob": b"\x00\xffkey", "ratio": 0.5}]}
    routes_backup._write_kept(rows, [{"username": "cook@home.example"}])
    assert routes_backup._read_kept() == (rows, [{"username": "cook@home.example"}])
    routes_backup._remove_kept()
    assert not os.listdir(tmp_path)


def test_a_kept_settings_file_without_the_restore_marker_isnt_applied(monkeypatch, tmp_path):
    """Claude review of 0167: a restore-kept.json that a finished restore
    couldn't remove put old settings back over newer ones at every start.
    Without the marker it's removed, not applied."""
    from app.routes import backup as routes_backup
    monkeypatch.setattr(routes_backup, "DATA_DIR", str(tmp_path))
    applied = []
    monkeypatch.setattr(routes_backup, "_put_back_kept", lambda rows, email: applied.append(rows))
    routes_backup._write_kept({"backup_schedule": [{"id": 1, "enabled": 0}]}, None)
    assert routes_backup.put_back_after_restart() is False
    assert applied == [] and not os.listdir(tmp_path)


def test_a_failed_put_back_keeps_its_file_under_another_name(monkeypatch, tmp_path):
    """Review of 0169: the file a failed put-back left was removed at the
    next start as left over. It's renamed, and the log names it."""
    from app import restore as restore_mod
    from app.routes import backup as routes_backup
    monkeypatch.setattr(routes_backup, "DATA_DIR", str(tmp_path))
    (tmp_path / restore_mod.MARKER).write_text("x")

    def boom(rows, email):
        raise OSError("disk full")
    monkeypatch.setattr(routes_backup, "_put_back_kept", boom)
    routes_backup._write_kept({"backup_schedule": []}, None)
    assert routes_backup.put_back_after_restart() is False
    names = sorted(os.listdir(tmp_path))
    assert len(names) == 2 and restore_mod.MARKER in names
    assert any(n.startswith("restore-kept.failed-") and n.endswith(".json") for n in names)


def test_a_second_failed_put_back_doesnt_replace_the_first(monkeypatch, tmp_path):
    """Review of 0176: both were renamed restore-kept.failed.json, so the
    first server's settings were lost to the second."""
    from app import restore as restore_mod
    from app.routes import backup as routes_backup
    monkeypatch.setattr(routes_backup, "DATA_DIR", str(tmp_path))
    stamps = iter(["20261113-100000", "20261113-110000"])
    real = routes_backup.time.strftime
    # Only the file name's call: logging formats its times with strftime too.
    monkeypatch.setattr(routes_backup.time, "strftime",
                        lambda fmt, *a: fmt.replace("%Y%m%d-%H%M%S", next(stamps)) if ".failed-" in fmt else real(fmt, *a))

    def boom(rows, email):
        raise OSError("disk full")
    monkeypatch.setattr(routes_backup, "_put_back_kept", boom)
    for rows in ([{"id": 1}], [{"id": 2}]):
        (tmp_path / restore_mod.MARKER).write_text("x")
        routes_backup._write_kept({"backup_schedule": rows}, None)
        assert routes_backup.put_back_after_restart() is False
    failed = sorted(n for n in os.listdir(tmp_path) if ".failed-" in n)
    assert failed == ["restore-kept.failed-20261113-100000.json", "restore-kept.failed-20261113-110000.json"]
