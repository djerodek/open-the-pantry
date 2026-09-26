"""From the DeepSeek review (September 26). Each fails on the previous code.

app.* is imported inside tests only; see the note in test_backup.py.
"""
from unittest.mock import MagicMock, patch

import pytest


def _enable(client, monkeypatch):
    from cryptography.fernet import Fernet
    from app import crypto
    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    r = client.put("/api/email-settings", json={
        "enabled": True, "imap_host": "imap.me.example", "imap_port": 993, "imap_use_ssl": True,
        "smtp_host": "smtp.me.example", "smtp_port": 465, "smtp_use_tls": False,
        "username": "me@me.example", "password": "s3cret", "notify_email": "me@me.example",
        "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 0,
    })
    assert r.status_code == 200


def _no_mail():
    return (patch("app.main.email_client.search_unseen_by_subject", return_value=[]),
            patch("app.main.email_client.mark_seen"))


def test_broken_sending_is_shown_in_settings_after_a_scan(client, monkeypatch):
    """The 3 AM scan's only way to report was the email that couldn't be
    sent. Now Settings says what went wrong and how much is waiting."""
    _enable(client, monkeypatch)
    from app.main import _queue_notification, SessionLocal
    db = SessionLocal(); _queue_notification(db, True, "Pancakes"); db.commit(); db.close()
    s1, s2 = _no_mail()
    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), s1, s2, \
         patch("app.main.email_client.connect_smtp", side_effect=Exception("535 authentication failed")):
        client.post("/api/email-settings/scan")
    got = client.get("/api/email-settings").json()
    assert "535 authentication failed" in got["last_problem"]
    assert got["last_problem_at"] and got["pending_notifications"] == 1

    # Next scan, sending works: queue goes out, warning clears.
    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), s1, s2, \
         patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email"):
        client.post("/api/email-settings/scan")
    got = client.get("/api/email-settings").json()
    assert got["last_problem"] is None and got["pending_notifications"] == 0
    client.delete("/api/email-settings/password")


def test_inbox_connection_failure_is_shown_in_settings(client, monkeypatch):
    _enable(client, monkeypatch)
    with patch("app.main.email_client.connect_imap", side_effect=Exception("timed out")):
        client.post("/api/email-settings/scan")
    assert "inbox" in client.get("/api/email-settings").json()["last_problem"]

    # A passing Send test email clears it.
    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email"):
        assert client.post("/api/email-settings/test").json()["success"] is True
    assert client.get("/api/email-settings").json()["last_problem"] is None
    client.delete("/api/email-settings/password")


def test_problem_columns_are_added_to_an_existing_database(tmp_path):
    import sqlite3
    from unittest.mock import patch
    from app import init_db
    db = tmp_path / "recipes.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE email_ingest_settings (id INTEGER PRIMARY KEY, enabled BOOLEAN NOT NULL DEFAULT 0, "
                  "allowed_senders TEXT NOT NULL DEFAULT '')")
        c.execute("INSERT INTO email_ingest_settings (id, enabled) VALUES (1, 0)")
    with patch.object(init_db, "DB_PATH", str(db)):
        init_db._migrate_and_setup_schema()
    with sqlite3.connect(db) as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(email_ingest_settings)")}
    assert {"last_problem", "last_problem_at"} <= cols


def test_probe_reports_a_failed_tls_handshake(monkeypatch):
    """A TLS failure that isn't about the certificate used to come out as
    "never answers", which points at a firewall instead of TLS."""
    from app import email_client
    from tests.test_email_real_world import _listen
    alert = b"\x15\x03\x01\x00\x02\x02\x46"   # TLS alert: protocol_version
    srv = _listen(alert)
    try:
        assert email_client.probe_port("127.0.0.1", srv.getsockname()[1], timeout=3) == "tls_error"
    finally:
        srv.close()


@pytest.mark.parametrize("title,ingredients,tag", [
    ("Braised Oxtail", ["2 lb oxtail", "1 onion"], "Beef"),
])
def test_tagger_oxtail_is_beef(title, ingredients, tag):
    from app.ingestion.tagger import suggest_tags
    names = [t[0] for t in suggest_tags(title, ingredients)]
    assert tag in names and "Vegetarian" not in names


def test_tagger_bone_marrow_is_not_vegetarian_but_vegetable_marrow_is():
    from app.ingestion.tagger import suggest_tags
    assert "Vegetarian" not in [t[0] for t in suggest_tags("Roasted Bone Marrow", ["4 bone marrow canoes", "parsley"])]
    assert "Vegetarian" in [t[0] for t in suggest_tags("Stuffed Marrow", ["1 large marrow", "rice", "tomatoes"])]


# Found by running the tagger over a real recipe library (not in the review).
@pytest.mark.parametrize("title,ingredients,expect,forbid", [
    ("Irish Stew", ["beef chuck", "bacon", "chicken stock/broth", "Guinness"], {"Beef", "Pork"}, {"Chicken", "Vegetarian"}),
    ("Pork Tenderloin with Egg Noodles", ["pork tenderloin", "egg noodles", "chicken stock"], {"Pork"}, {"Chicken"}),
    ("Garlic Pork Stir-Fry", ["ground pork", "soy sauce", "oyster sauce", "unsalted chicken broth"], {"Pork"}, {"Chicken", "Fish"}),
    ("Blade Roast", ["potatoes", "carrots", "1 blade roast", "onion soup mix"], {"Beef"}, {"Vegetarian"}),
    ("Pad See Ew with Tofu", ["tofu", "rice noodles", "fish sauce"], set(), {"Fish", "Vegetarian"}),
    ("Minestrone", ["beans", "pasta", "vegetable broth"], {"Vegetarian"}, set()),
])
def test_stock_and_sauce_are_not_the_main_ingredient(title, ingredients, expect, forbid):
    from app.ingestion.tagger import suggest_tags
    names = {t[0] for t in suggest_tags(title, ingredients)}
    assert expect <= names and not (forbid & names), names
