"""Stale app files, the notes indicator, the French interface on the
server side, and Share -> Email PDF.

app.* is imported inside tests only; see the note in test_backup.py.
"""
import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest

FR = {"X-App-Lang": "fr"}


def _recipe(client, title, notes=None, **extra):
    body = {"title": title, "source_type": "manual",
            "ingredients": [{"raw_line": "2 cups flour"}], "steps": ["Mix."]}
    body.update(extra)
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    if notes is not None:
        assert client.patch(f"/api/recipes/{rid}/notes", json={"notes": notes}).status_code == 200
    return rid


# ---------------------------------------------------------------------------
# Stale app files
# ---------------------------------------------------------------------------

def test_version_endpoint_reports_a_fingerprint(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    assert isinstance(r.json()["frontend"], str) and r.json()["frontend"]


def test_app_files_revalidate_but_api_and_photos_do_not(client):
    assert client.get("/healthz").headers.get("cache-control") != "no-cache"
    assert client.get("/api/version").headers.get("cache-control") != "no-cache"
    # Any non-API GET gets it, found or not (the static mount may be absent
    # in a test checkout).
    assert client.get("/index.html").headers.get("cache-control") == "no-cache"


# ---------------------------------------------------------------------------
# Notes indicator
# ---------------------------------------------------------------------------

def test_list_says_which_recipes_have_notes(client):
    with_notes = _recipe(client, "Has Notes Card", notes="Use less salt.")
    blank_notes = _recipe(client, "Blank Notes Card", notes="   ")
    rows = {r["id"]: r for r in client.get("/api/recipes?q=Notes Card").json()}
    assert rows[with_notes]["has_notes"] is True
    assert rows[blank_notes]["has_notes"] is False
    assert "notes" not in rows[with_notes]  # the text itself stays out of the list


# ---------------------------------------------------------------------------
# French on the server side
# ---------------------------------------------------------------------------

def test_error_messages_follow_the_app_language(client):
    assert client.get("/api/recipes/999999").json()["detail"] == "Recipe not found"
    assert client.get("/api/recipes/999999", headers=FR).json()["detail"] == "Recette introuvable"
    # ?lang= for a plain navigation that can't send the header
    assert client.get("/api/recipes/999999?lang=fr").json()["detail"] == "Recette introuvable"


def test_unknown_language_falls_back_to_english(client):
    r = client.get("/api/recipes/999999", headers={"X-App-Lang": "de"})
    assert r.json()["detail"] == "Recipe not found"


def test_composed_messages_are_translated_part_by_part():
    from app import i18n
    msg = ("Sending (SMTP) failed: Could not connect/login to SMTP (mail.x.example:465, implicit TLS): "
           "timed out -- Diagnosis: mail.x.example:465 accepts the connection but never answers. "
           "Usually a firewall or proxy in between, or the wrong port.\n"
           "Reading: OK -- logged in to the inbox. Recipes can still be ingested; only the result "
           "notifications can't be sent until sending works.")
    fr = i18n.translate(msg, "fr")
    assert fr.startswith("L'envoi (SMTP) a échoué : Connexion ou authentification SMTP impossible "
                         "(mail.x.example:465, TLS implicite) : timed out -- Diagnostic : ")
    assert "Lecture : OK." in fr and "Sending" not in fr and "Reading" not in fr
    assert i18n.translate(msg, "en") == msg


def test_scan_result_lines_are_translated():
    from app import i18n
    assert i18n.translate('OK: Pie (from "[RECIPE] pie", via PDF attachment)', "fr") == \
        "OK : Pie (de « [RECIPE] pie », par la pièce jointe PDF)"
    assert i18n.translate('FAILED: "x": No recipe found. Tried: body: empty.', "fr") == \
        'ÉCHEC : "x": Aucune recette trouvée. Essais : corps : vide.'


def test_large_json_and_non_json_responses_pass_through(client):
    r = client.get("/api/recipes", headers=FR)
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_export_labels_follow_the_app_language(client):
    rid = _recipe(client, "Export Langue", servings="4", notes="Moins de sel.")
    en = client.get(f"/api/recipes/{rid}/export.html?include_notes=true").text
    fr = client.get(f"/api/recipes/{rid}/export.html?include_notes=true", headers=FR).text
    assert "<h2>Ingredients</h2>" in en and "Servings:" in en
    assert "<h2>Ingrédients</h2>" in fr and "<h2>Préparation</h2>" in fr
    assert "Portions : 4" in fr and 'lang="fr-CA"' in fr
    assert "saisie manuelle" in fr


def test_backup_instructions_follow_the_app_language(client):
    _recipe(client, "Backup Langue")
    r = client.get("/api/backup/database.zip", headers=FR)
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "RESTAURER.txt" in names and "RESTORE.txt" not in names


# ---------------------------------------------------------------------------
# Email: settings language, notification emails, Share -> Email PDF
# ---------------------------------------------------------------------------

@pytest.fixture
def mail(client, monkeypatch):
    from cryptography.fernet import Fernet
    from app import crypto, main
    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    main._recipe_email_times.clear()
    body = {"enabled": False, "imap_host": "mail.me.example", "imap_port": 993, "imap_use_ssl": True,
            "smtp_host": "mail.me.example", "smtp_port": 465, "smtp_use_tls": False,
            "username": "pantry@me.example", "password": "s3cret", "notify_email": "me@me.example",
            "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 0}
    assert client.put("/api/email-settings", json=body).status_code == 200
    yield body
    client.put("/api/email-settings/recipients", json={"recipients": []})
    client.put("/api/email-settings", json=body)  # back to English
    client.delete("/api/email-settings/password")
    main._recipe_email_times.clear()


def test_notification_email_uses_the_saved_language(client, mail):
    from app.database import SessionLocal
    from app.main import _queue_notification, _flush_notifications, _get_email_settings
    client.put("/api/email-settings", json={**mail, "password": None}, headers=FR)
    db = SessionLocal()
    try:
        _queue_notification(db, True, 'Tarte (from "[RECIPE] tarte", via email body text)')
        db.commit()
        with patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
             patch("app.main.email_client.send_email") as send:
            assert _flush_notifications(db, _get_email_settings(db), force=True) is True
        subject, body = send.call_args.args[3], send.call_args.args[4]
        assert subject.startswith("[SUCCESS] Open the Pantry : 1 recette(s)")
        assert body.startswith("Reçues :") and "par le texte du corps du courriel" in body
    finally:
        db.close()


def test_settings_say_whether_sending_can_work(client, mail):
    got = client.get("/api/email-settings").json()
    assert got["can_send"] is True and got["enabled"] is False  # the daily scan needn't be on
    client.delete("/api/email-settings/password")
    assert client.get("/api/email-settings").json()["can_send"] is False


def test_email_pdf_sends_from_the_ingest_account(client, mail):
    rid = _recipe(client, "Tourtière\nBcc: x@evil.example", notes="secret note")
    with patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email_with_attachment") as send:
        r = client.post(f"/api/recipes/{rid}/email",
                        json={"to": ["Ann@family.example", " ann@family.example ", "bob@family.example"],
                              "message": "Try this one."},
                        headers=FR)
    assert r.status_code == 200, r.text
    args = send.call_args.args
    assert args[1] == "pantry@me.example"
    assert args[2] == ["Ann@family.example", "bob@family.example"]
    assert args[3] == "Recette : Tourtière Bcc: x@evil.example"  # newline flattened
    assert args[4].startswith("Try this one.") and "Envoyé depuis Open the Pantry." in args[4]
    assert args[5][:4] == b"%PDF" and args[6].endswith(".pdf")
    assert b"secret note" not in args[5]
    assert r.json()["recent_recipients"][:2] == ["Ann@family.example", "bob@family.example"]
    assert client.get("/api/email-settings").json()["recent_recipients"][:2] == \
        ["Ann@family.example", "bob@family.example"]


def test_a_real_send_builds_a_pdf_attachment():
    from email import message_from_bytes
    from app import email_client
    smtp = MagicMock()
    email_client.send_email_with_attachment(smtp, "a@x.example", ["b@y.example", "c@y.example"],
                                            "Recipe: Pâté", "Hi", b"%PDF-1.4 x", "Pâté.pdf")
    msg = smtp.send_message.call_args.args[0]
    raw = msg.as_bytes()
    parsed = message_from_bytes(raw)
    assert parsed["To"] == "b@y.example, c@y.example"
    parts = [p for p in parsed.walk() if p.get_content_disposition() == "attachment"]
    assert parts[0].get_filename() == "Pâté.pdf" and parts[0].get_payload(decode=True) == b"%PDF-1.4 x"


@pytest.mark.parametrize("to, status, fragment", [
    ([], 400, "Ajoutez au moins un destinataire."),
    (["not-an-address"], 400, "« not-an-address » n'est pas une adresse courriel."),
    (["a@b.example\r\nBcc: z@evil.example"], 400, "n'est pas une adresse courriel"),
    ([f"p{i}@x.example" for i in range(6)], 400, "Au plus 5 destinataires"),
])
def test_email_pdf_validation(client, mail, to, status, fragment):
    rid = _recipe(client, "Validation Courriel")
    with patch("app.main.email_client.connect_smtp") as smtp:
        r = client.post(f"/api/recipes/{rid}/email", json={"to": to}, headers=FR)
    assert r.status_code == status and fragment in r.json()["detail"]
    smtp.assert_not_called()


def test_email_pdf_reports_the_smtp_problem(client, mail):
    rid = _recipe(client, "Erreur SMTP")
    with patch("app.main.email_client.connect_smtp",
               side_effect=Exception("Could not connect/login to SMTP (mail.me.example:465, implicit TLS): "
                                     "535 authentication failed")):
        r = client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("The email wasn't sent: Could not connect/login to SMTP")
    assert "535 authentication failed" in r.json()["detail"]


def test_email_pdf_is_rate_limited(client, mail):
    from app import main
    rid = _recipe(client, "Limite Courriel")
    with patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email_with_attachment"):
        codes = [client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]}).status_code
                 for _ in range(main.MAX_RECIPE_EMAILS_PER_HOUR + 1)]
    assert codes[:-1] == [200] * main.MAX_RECIPE_EMAILS_PER_HOUR and codes[-1] == 429


def test_email_pdf_needs_sending_set_up(client, mail):
    rid = _recipe(client, "Pas configuré")
    client.delete("/api/email-settings/password")
    r = client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]})
    assert r.status_code == 400 and "isn't set up" in r.json()["detail"]


def test_recent_recipients_are_editable(client, mail):
    r = client.put("/api/email-settings/recipients",
                   json={"recipients": ["x@y.example", "X@y.example", "z@y.example"]})
    assert r.json()["recipients"] == ["x@y.example", "z@y.example"]
    assert client.get("/api/email-settings").json()["recent_recipients"] == ["x@y.example", "z@y.example"]
    bad = client.put("/api/email-settings/recipients", json={"recipients": ["nope"]})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# "Servings: None"
# ---------------------------------------------------------------------------

def test_a_page_without_a_yield_has_no_servings():
    from unittest.mock import patch
    from app.ingestion import url_ingest

    scraper = MagicMock()
    scraper.title.return_value = "No Yield Stew"
    scraper.ingredients.return_value = ["1 cup lentils", "2 cups water"]
    scraper.instructions_list.return_value = ["Simmer."]
    scraper.instructions.return_value = "Simmer."
    scraper.yields.return_value = None
    with patch("recipe_scrapers.scrape_html", return_value=scraper):
        result = url_ingest._try_recipe_scrapers("https://example.com/stew", "<html></html>")
    assert result is not None and result.title == "No Yield Stew"
    assert result.servings is None


def test_stored_none_servings_are_cleared_at_startup(client):
    from sqlalchemy import text
    from app.database import engine
    from app.init_db import init_db
    rid = _recipe(client, "Servings None Text")
    with engine.begin() as conn:
        conn.execute(text("UPDATE recipes SET servings = 'None' WHERE id = :i"), {"i": rid})
    init_db()
    assert client.get(f"/api/recipes/{rid}").json()["servings"] is None
