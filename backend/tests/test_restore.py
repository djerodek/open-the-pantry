"""Restore from backup in Settings (backlog item 3, app/restore.py).

The test session shares one database with every other test, so the
restores here put back a backup of that same database (taken at the start
of each test), and check what changed in between."""
import io
import json
import os
import sqlite3
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


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
    from app.database import engine
    from sqlalchemy import text
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
    tok = _check(stranger, client.get("/api/backup/database.zip").content).json()["token"]
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
