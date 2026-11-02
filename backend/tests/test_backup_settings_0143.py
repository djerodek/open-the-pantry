"""0143 (owner's decision on the Claude security audit of 0140): a backup,
downloaded or automatic, never holds the email login and password, the
subject keyword, allowed senders, notification address and recent
recipients, or the HTTPS name and addresses. The download needs no
password, and these are what the settings password guards. A restore
keeps this server's own; an older backup's are still restored."""
import io
import sqlite3
import zipfile

import pytest

H = {"X-Requested-With": "OpenThePantry"}
SECRET = {"username": "cook@home.example", "password_encrypted": "gAAAA-secret-token", "notify_email": "me@home.example",
          "subject_keyword": "[pantry-7f3a]", "allowed_senders": "me@home.example, @family.example",
          "recent_recipients": '["aunt@family.example"]', "last_problem": "IMAP login failed for cook@home.example"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.fixture
def configured():
    """Email and HTTPS settings as a configured server has them."""
    from sqlalchemy import text
    from app.database import engine
    with engine.begin() as c:
        before = [dict(r) for r in c.execute(text("SELECT * FROM email_ingest_settings")).mappings()]
        https_before = [dict(r) for r in c.execute(text("SELECT * FROM https_settings")).mappings()]
        c.execute(text("DELETE FROM email_ingest_settings"))
        c.execute(text("INSERT INTO email_ingest_settings (id, enabled, imap_host, imap_port, imap_use_ssl, smtp_host, "
                       "smtp_port, smtp_use_tls, username, password_encrypted, notify_email, subject_keyword, "
                       "allowed_senders, daily_scan_hour, cooldown_minutes, language, recent_recipients, last_problem) "
                       "VALUES (1, 0, 'imap.home.example', 993, 1, 'smtp.home.example', 587, 1, :username, "
                       ":password_encrypted, :notify_email, :subject_keyword, :allowed_senders, 3, 30, 'en', "
                       ":recent_recipients, :last_problem)"), SECRET)
        c.execute(text("DELETE FROM https_settings"))
        c.execute(text("INSERT INTO https_settings (id, enabled, domain, lan_address, email, public_port, state) "
                       "VALUES (1, 0, 'pantry.home.example', '192.168.60.20', 'acme@home.example', 8443, 'off')"))
    yield
    with engine.begin() as c:
        for table, rows in (("email_ingest_settings", before), ("https_settings", https_before)):
            c.execute(text(f"DELETE FROM {table}"))
            for row in rows:
                c.execute(text(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join(':' + k for k in row)})"), row)


def _db_bytes(zip_bytes):
    return zipfile.ZipFile(io.BytesIO(zip_bytes)).read("recipes.db")


def test_the_download_holds_none_of_them(client, configured, tmp_path):
    data = client.get("/api/backup/database.zip").content
    raw = _db_bytes(data)
    # Not in the rows, nor left in the file's free pages.
    for value in list(SECRET.values()) + ["pantry.home.example", "192.168.60.20", "acme@home.example"]:
        assert value.encode() not in raw, value
    path = tmp_path / "b.db"
    path.write_bytes(raw)
    with sqlite3.connect(path) as c:
        row = c.execute("SELECT enabled, username, password_encrypted, subject_keyword, allowed_senders, "
                        "recent_recipients, imap_host, smtp_host FROM email_ingest_settings").fetchone()
        assert row == (0, None, None, "", "", "[]", "imap.home.example", "smtp.home.example")   # servers stay
        assert c.execute("SELECT domain, lan_address, email FROM https_settings").fetchone() == (None, None, None)


def test_automatic_backups_too(client, configured, tmp_path):
    from app.backup import build_database_backup
    dest = tmp_path / "auto.zip"
    build_database_backup(str(dest))
    raw = _db_bytes(dest.read_bytes())
    assert SECRET["subject_keyword"].encode() not in raw and SECRET["password_encrypted"].encode() not in raw


def test_the_live_settings_are_untouched(client, configured):
    from sqlalchemy import text
    from app.database import engine
    client.get("/api/backup/database.zip")
    with engine.connect() as c:
        assert c.execute(text("SELECT subject_keyword FROM email_ingest_settings")).scalar() == SECRET["subject_keyword"]


def test_a_restore_keeps_this_servers_email_settings(client, configured):
    from sqlalchemy import text
    from app.database import engine
    data = client.get("/api/backup/database.zip").content
    tok = client.post("/api/backup/restore/check", headers=H,
                      files={"file": ("b.zip", data, "application/zip")}).json()["token"]
    r = client.post("/api/backup/restore", headers=H, json={"token": tok})
    assert r.status_code == 200, r.text
    with engine.connect() as c:
        row = c.execute(text("SELECT username, subject_keyword, allowed_senders FROM email_ingest_settings")).one()
        assert tuple(row) == (SECRET["username"], SECRET["subject_keyword"], SECRET["allowed_senders"])
        assert c.execute(text("SELECT domain FROM https_settings")).scalar() == "pantry.home.example"


def test_an_older_backups_email_settings_are_restored(tmp_path):
    from app.backup import settings_blanked
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE email_ingest_settings (username TEXT, password_encrypted TEXT, subject_keyword TEXT, "
                  "allowed_senders TEXT)")
        c.execute("INSERT INTO email_ingest_settings VALUES ('cook@home.example', 'tok', '[RECIPE]', '')")
    assert settings_blanked(str(old)) is False
    with sqlite3.connect(old) as c:
        c.execute("UPDATE email_ingest_settings SET username = NULL, password_encrypted = NULL, subject_keyword = ''")
    assert settings_blanked(str(old)) is True


def test_the_compose_files_say_the_ports_are_on_every_address():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for name in ("docker-compose.yml", "docker-compose.build.yml"):
        assert "Published on every address of this machine" in (root / name).read_text(encoding="utf-8"), name


def test_a_backup_says_it_was_blanked(client, configured, tmp_path):
    """GPT review of 0147: the backup carries a mark, rather than the
    restore guessing from blank fields; the mark doesn't stay in the live
    database after a restore."""
    from app.backup import REDACTED_MARK_TABLE, settings_blanked
    path = tmp_path / "b.db"
    path.write_bytes(_db_bytes(client.get("/api/backup/database.zip").content))
    with sqlite3.connect(path) as c:
        assert c.execute(f"SELECT version FROM {REDACTED_MARK_TABLE}").fetchone() == (1,)
    assert settings_blanked(str(path)) is True
    from sqlalchemy import text
    from app.database import engine
    from app.init_db import init_db
    with engine.begin() as c:
        c.execute(text(f"CREATE TABLE IF NOT EXISTS {REDACTED_MARK_TABLE} (version INTEGER)"))
    init_db()
    with engine.connect() as c:
        assert c.execute(text("SELECT 1 FROM sqlite_master WHERE name = :n"), {"n": REDACTED_MARK_TABLE}).first() is None


def test_an_older_backup_with_only_some_fields_blank_is_restored_as_it_was(tmp_path):
    """GPT review of 0147: no keyword, no senders and no login, but a
    notification address and recipients: an older backup, not a blanked
    one."""
    from app.backup import settings_blanked
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as c:
        c.execute("CREATE TABLE email_ingest_settings (enabled INTEGER, username TEXT, password_encrypted TEXT, "
                  "notify_email TEXT, subject_keyword TEXT, allowed_senders TEXT, recent_recipients TEXT, "
                  "last_problem TEXT)")
        c.execute("INSERT INTO email_ingest_settings VALUES (0, NULL, NULL, 'me@home.example', '', '', "
                  "'[\"aunt@family.example\"]', NULL)")
    assert settings_blanked(str(old)) is False


def _restore(client, data):
    tok = client.post("/api/backup/restore/check", headers=H,
                      files={"file": ("b.zip", data, "application/zip")}).json()["token"]
    r = client.post("/api/backup/restore", headers=H, json={"token": tok})
    assert r.status_code == 200, r.text
    return r.json()


def test_a_restore_keeps_this_servers_backup_schedule(client):
    """Claude review of 0148: restoring a backup taken with automatic
    backups off turned them off again, without a word."""
    from sqlalchemy import text
    from app.database import engine
    data = client.get("/api/backup/database.zip").content
    with engine.begin() as c:
        before = [dict(r) for r in c.execute(text("SELECT * FROM backup_schedule")).mappings()]
        c.execute(text("DELETE FROM backup_schedule"))
        c.execute(text("INSERT INTO backup_schedule (id, enabled, hour, keep) VALUES (1, 1, 2, 14)"))
    try:
        _restore(client, data)
        with engine.connect() as c:
            assert tuple(c.execute(text("SELECT enabled, hour, keep FROM backup_schedule")).one()) == (1, 2, 14)
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM backup_schedule"))
            for row in before:
                c.execute(text(f"INSERT INTO backup_schedule ({', '.join(row)}) VALUES "
                               f"({', '.join(':' + k for k in row)})"), row)


def test_a_new_server_gets_the_backups_mail_servers(client, configured):
    """Claude review of 0148: the whole live row was put back, so a new
    server's defaults replaced the backup's mail servers and scan hour."""
    from sqlalchemy import text
    from app.database import engine
    with engine.begin() as c:
        c.execute(text("UPDATE email_ingest_settings SET daily_scan_hour = 5"))
    data = client.get("/api/backup/database.zip").content
    with engine.begin() as c:                       # a new server: defaults, its own login
        c.execute(text("UPDATE email_ingest_settings SET imap_host = NULL, smtp_host = NULL, daily_scan_hour = 3, "
                       "username = 'new@server.example'"))
    _restore(client, data)
    with engine.connect() as c:
        row = c.execute(text("SELECT imap_host, smtp_host, daily_scan_hour, username FROM email_ingest_settings")).one()
    assert tuple(row) == ("imap.home.example", "smtp.home.example", 5, "new@server.example")


def test_the_safety_copy_keeps_the_settings(client, configured):
    """Claude review of 0148: the copy made before a restore stays in the
    data folder and is how a restore is undone, so it isn't blanked."""
    import os
    from app.database import DATA_DIR
    body = _restore(client, client.get("/api/backup/database.zip").content)
    raw = _db_bytes(open(os.path.join(DATA_DIR, body["safety_backup"]), "rb").read())
    assert SECRET["subject_keyword"].encode() in raw and b"pantry.home.example" in raw


def test_the_safety_copy_says_it_needs_the_key(client, configured):
    """DeepSeek review of 0149: the safety copy keeps the encrypted
    credentials, so its RESTORE.txt must not say encryption.key isn't
    needed; a downloaded backup's still does. Both languages."""
    import os
    from app import backup
    from app.database import DATA_DIR
    body = _restore(client, client.get("/api/backup/database.zip").content)
    safety = zipfile.ZipFile(os.path.join(DATA_DIR, body["safety_backup"])).read("RESTORE.txt").decode()
    assert "safety copy written before a restore" in safety and "isn't needed" not in safety
    assert "Left out on purpose" not in safety
    download = zipfile.ZipFile(io.BytesIO(client.get("/api/backup/database.zip").content)).read("RESTORE.txt").decode()
    assert "isn't needed to restore it" in download and "safety copy written" not in download
    fr = backup.restore_instructions("fr", blank=False)
    assert "copie de sécurité écrite" in fr and "n'est donc pas nécessaire" not in fr
    assert backup.restore_instructions("fr") == backup.RESTORE_INSTRUCTIONS_FR


def test_waiting_notifications_dont_carry_the_keyword(client, configured):
    """Claude review of 0148: result lines quote email subjects."""
    from sqlalchemy import text
    from app.database import engine
    with engine.begin() as c:
        c.execute(text("INSERT INTO email_notification_queue (success, message) VALUES (1, :m)"),
                  {"m": f'Saved (from "{SECRET["subject_keyword"]} Lasagna")'})
    try:
        raw = _db_bytes(client.get("/api/backup/database.zip").content)
        assert SECRET["subject_keyword"].encode() not in raw
    finally:
        with engine.begin() as c:
            c.execute(text("DELETE FROM email_notification_queue"))
