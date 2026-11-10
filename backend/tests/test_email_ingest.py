import ast
import email
import io
import os
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .conftest import TINY_PNG

# ---------------------------------------------------------------------------
# Credential encryption
# ---------------------------------------------------------------------------

def test_encryption_round_trip(monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    encrypted = crypto.encrypt_secret("hunter2")
    assert encrypted != "hunter2"
    assert crypto.decrypt_secret(encrypted) == "hunter2"


def test_encryption_fails_closed_without_key(monkeypatch):
    """Without a configured key, storing a credential must raise rather
    than silently falling back to plaintext."""
    from app import crypto

    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    assert crypto.encryption_configured() is False
    with pytest.raises(crypto.EncryptionNotConfiguredError):
        crypto.encrypt_secret("hunter2")


def test_decryption_fails_on_wrong_key(monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    encrypted = crypto.encrypt_secret("hunter2")

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    with pytest.raises(crypto.DecryptionFailedError):
        crypto.decrypt_secret(encrypted)


# ---------------------------------------------------------------------------
# Settings endpoints
# ---------------------------------------------------------------------------

def _settings_payload(**overrides):
    payload = {
        "enabled": False,
        "imap_host": "imap.example.com", "imap_port": 993, "imap_use_ssl": True,
        "smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_use_tls": True,
        "username": "me@example.com",
        "notify_email": "me@example.com",
        "subject_keyword": "[RECIPE]",
        "daily_scan_hour": 3,
        "cooldown_minutes": 30,
    }
    payload.update(overrides)
    return payload


def test_email_settings_defaults(client):
    r = client.get("/api/email-settings")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["password_set"] is False
    assert body["subject_keyword"] == "[RECIPE]"
    assert body["daily_scan_hour"] == 3


def test_email_password_never_returned(client, monkeypatch):
    """The stored credential must never round-trip back to the client in
    any form -- only a boolean indicating whether one is set."""
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())

    r = client.put("/api/email-settings", json=_settings_payload(password="super-secret-pw"))
    assert r.status_code == 200
    assert "super-secret-pw" not in r.text
    assert r.json()["password_set"] is True

    r = client.get("/api/email-settings")
    assert "super-secret-pw" not in r.text
    assert "password" not in {k for k in r.json() if k != "password_set"}

    client.delete("/api/email-settings/password")


def test_email_password_stored_encrypted(client, monkeypatch):
    from cryptography.fernet import Fernet
    from sqlalchemy import text

    from app import crypto
    from app.database import engine

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(password="plaintext-check"))

    with engine.connect() as conn:
        row = conn.execute(text("SELECT password_encrypted FROM email_ingest_settings WHERE id=1")).fetchone()
    assert row[0] is not None
    assert row[0] != "plaintext-check"
    assert crypto.decrypt_secret(row[0]) == "plaintext-check"

    client.delete("/api/email-settings/password")


def test_omitted_password_preserves_stored_credential(client, monkeypatch):
    """Standard 'leave blank to keep current' behavior -- saving other
    settings shouldn't wipe the credential."""
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(password="keep-me"))

    r = client.put("/api/email-settings", json=_settings_payload(daily_scan_hour=7))
    assert r.json()["password_set"] is True
    assert r.json()["daily_scan_hour"] == 7

    client.delete("/api/email-settings/password")


def test_saving_password_without_encryption_key_is_rejected(client, monkeypatch):
    from sqlalchemy import text

    from app import crypto
    from app.database import engine

    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    client.delete("/api/email-settings/password")  # self-contained: don't depend on prior test cleanup
    r = client.put("/api/email-settings", json=_settings_payload(password="should-not-persist"))
    assert r.status_code == 400

    with engine.connect() as conn:
        row = conn.execute(text("SELECT password_encrypted FROM email_ingest_settings WHERE id=1")).fetchone()
    stored = row[0] if row else None
    assert stored is None or "should-not-persist" not in stored


def test_cannot_enable_without_credentials(client, monkeypatch):
    from app import crypto

    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    client.delete("/api/email-settings/password")
    r = client.put("/api/email-settings", json=_settings_payload(enabled=True))
    assert r.status_code == 400


def test_clearing_password_disables_feature(client, monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(enabled=True, password="temp"))

    client.delete("/api/email-settings/password")
    r = client.get("/api/email-settings")
    assert r.json()["password_set"] is False
    assert r.json()["enabled"] is False, "feature can't stay enabled without a credential"


def test_scan_hour_validated(client):
    r = client.put("/api/email-settings", json=_settings_payload(daily_scan_hour=25))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Email content extraction
# ---------------------------------------------------------------------------

def _make_email(subject="[RECIPE] test", body="", attachment=None, attach_type=None, filename=None):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "sender@example.com"
    msg["To"] = "me@example.com"
    msg.set_content(body)
    if attachment is not None:
        maintype, subtype = attach_type.split("/")
        msg.add_attachment(attachment, maintype=maintype, subtype=subtype, filename=filename)
    return msg


def test_extract_from_pdf_attachment(tmp_path):
    from weasyprint import HTML

    from app.ingestion.email_processing import process_tagged_email

    pdf_bytes = HTML(string=(
        "<h1>Attached Recipe</h1><h2>Ingredients</h2><p>2 cups flour</p><p>1 tsp salt</p>"
        "<h2>Instructions</h2><p>1. Mix</p><p>2. Bake</p>"
    )).write_pdf()
    msg = _make_email(body="See attached.", attachment=pdf_bytes,
                      attach_type="application/pdf", filename="recipe.pdf")

    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is True
    assert result["title"] == "Attached Recipe"
    assert [i["raw_line"] for i in result["ingredients"]] == ["2 cups flour", "1 tsp salt"]
    assert result["source_detail"] == "PDF attachment"
    # the source PDF isn't retained once its text is extracted
    assert not any(f.endswith(".pdf") for f in os.listdir(tmp_path))


def test_extract_from_image_attachment(tmp_path):
    from PIL import Image, ImageDraw, ImageFont

    from app.ingestion.email_processing import process_tagged_email

    img = Image.new("L", (700, 250), color=255)
    draw = ImageDraw.Draw(img)
    from .conftest import text_font
    font = text_font(28, bold=True)
    for i, line in enumerate(["My Recipe", "Ingredients", "2 cups flour", "Instructions", "1. Mix well"]):
        draw.text((20, 20 + i * 40), line, fill=0, font=font)
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    msg = _make_email(body="See attached.", attachment=buf.getvalue(),
                      attach_type="image/png", filename="card.png")
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is True
    assert result["source_detail"] == "image attachment"
    # an image attachment IS kept -- it becomes the recipe's showcase image
    assert result["image_path"] is not None
    assert os.path.isfile(os.path.join(tmp_path, result["image_path"]))


def test_extract_from_body_text(tmp_path):
    from app.ingestion.email_processing import process_tagged_email

    msg = _make_email(body="Body Recipe\nIngredients\n2 cups flour\n1 tsp salt\nInstructions\n1. Mix\n2. Bake")
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is True
    assert result["source_detail"] == "email body text"
    assert [i["raw_line"] for i in result["ingredients"]] == ["2 cups flour", "1 tsp salt"]


def test_extract_from_single_url_in_body(tmp_path):
    """A body with just a link should hand off to the existing URL
    ingestion pipeline rather than trying to parse the email itself."""
    from app.ingestion.email_processing import process_tagged_email

    class FakeUrlResult:
        title = "Linked Recipe"
        ingredients = ["2 cups flour"]
        steps = ["Mix"]
        raw_text = "Mix"

    msg = _make_email(body="Check this out: https://example.com/recipe")
    with patch("app.ingestion.email_processing.ingest_url", return_value=FakeUrlResult()):
        result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is True
    assert result["title"] == "Linked Recipe"
    assert "URL in body" in result["source_detail"]
    assert result["source_url"] == "https://example.com/recipe"


def test_emailed_link_keeps_the_page_address_not_the_tracking_link(tmp_path):
    """Newsletter links are click-tracking redirects that can expire; the
    address kept for "View original" is where the redirects ended."""
    from app.ingestion.email_processing import process_tagged_email

    class FakeUrlResult:
        title = "Linked Recipe"
        ingredients = ["2 cups flour"]
        steps = ["Mix"]
        raw_text = "Mix"
        final_url = "https://example.com/recipes/linked"

    msg = _make_email(body="Click: https://click.example.net/t/abc123")
    with patch("app.ingestion.email_processing.ingest_url", return_value=FakeUrlResult()):
        result = process_tagged_email(msg, str(tmp_path))
    assert result["source_url"] == "https://example.com/recipes/linked"


def test_ingest_url_reports_the_address_after_redirects():
    from app.ingestion import url_ingest

    class FakeResp:
        status_code = 200
        url = "https://example.com/recipes/final"
        text = ('<html><script type="application/ld+json">{"@type": "Recipe", "name": "R", '
                '"recipeIngredient": ["1 cup flour"], "recipeInstructions": ["Mix"]}</script></html>')

    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest, "safe_get", return_value=FakeResp()), \
         patch.object(url_ingest, "_try_recipe_scrapers", return_value=None):
        result = url_ingest.ingest_url("https://click.example.net/t/abc123")
    assert result.final_url == "https://example.com/recipes/final"


def test_only_a_link_recipe_has_a_source_url(tmp_path):
    from app.ingestion.email_processing import process_tagged_email

    msg = _make_email(body="Body Recipe\nIngredients\n2 cups flour\nInstructions\n1. Mix\n2. Bake")
    assert process_tagged_email(msg, str(tmp_path))["source_url"] is None


def test_emailed_link_recipe_keeps_its_source_url(client):
    """The recipe page links to the original; for an emailed link that
    means the saved recipe has to carry the address."""
    from app.main import SessionLocal
    from app.routes.email import _save_email_recipe

    base = {"title": "Source link test", "ingredients": [], "steps": ["Mix"], "tags": [],
            "raw_text": "Mix", "ocr_confidence": None, "image_path": None}
    db = SessionLocal()
    try:
        with_link = _save_email_recipe(db, {**base, "source_url": "https://example.com/r"}, "subject")
        without = _save_email_recipe(db, {**base, "source_url": None}, "subject")
    finally:
        db.close()
    got = client.get(f"/api/recipes/{with_link}").json()
    assert (got["source_type"], got["source_url"]) == ("email", "https://example.com/r")
    # The export prints the address for an emailed link recipe too.
    assert "https://example.com/r" in client.get(f"/api/recipes/{with_link}/export.html").text
    assert client.get(f"/api/recipes/{without}").json()["source_url"] is None


def test_extract_failure_on_unusable_email(tmp_path):
    from app.ingestion.email_processing import process_tagged_email

    msg = _make_email(body="hey check this out sometime")
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is False
    assert "error" in result


def test_malicious_attachment_rejected(tmp_path):
    """An attachment claiming to be a PDF but isn't must not be parsed --
    same magic-byte validation as a direct upload."""
    from app.ingestion.email_processing import process_tagged_email

    msg = _make_email(body="See attached.", attachment=b"<script>alert(1)</script>" * 10,
                      attach_type="application/pdf", filename="evil.pdf")
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is False
    assert not os.listdir(tmp_path), "invalid attachment should leave nothing on disk"


# ---------------------------------------------------------------------------
# Full scan round-trip (mocked IMAP/SMTP)
# ---------------------------------------------------------------------------

def test_full_scan_ingests_and_notifies(client, monkeypatch):
    """End-to-end scan with both IMAP and SMTP mocked: a tagged email is
    found, parsed, saved as a recipe, marked Seen, and a notification
    sent."""
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(enabled=True, password="pw"))

    msg = _make_email(subject="[RECIPE] Scan Test Dish",
                      body="Scan Test Dish\nIngredients\n2 cups flour\nInstructions\n1. Mix")

    fake_imap = MagicMock()
    sent = {}

    def fake_send_email(smtp_conn, from_addr, to_addr, subject, body):
        sent["subject"] = subject
        sent["body"] = body

    with patch("app.routes.email.email_client.connect_imap", return_value=fake_imap), \
         patch("app.routes.email.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.routes.email.email_client.fetch_full_message", return_value=msg), \
         patch("app.routes.email.email_client.mark_seen") as mock_mark_seen, \
         patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email", side_effect=fake_send_email):
        r = client.post("/api/email-settings/scan")

    assert r.status_code == 200
    body = r.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 0
    mock_mark_seen.assert_called_once()
    assert "[SUCCESS]" in sent["subject"]

    # the recipe actually landed, tagged as email-sourced
    r = client.get("/api/recipes", params={"q": "Scan Test Dish"})
    matches = [x for x in r.json() if x["title"] == "Scan Test Dish"]
    assert len(matches) == 1
    assert matches[0]["source_type"] == "email"

    client.post("/api/recipes/batch-delete", json={"ids": [matches[0]["id"]]})
    client.delete("/api/email-settings/password")


def test_scan_marks_unparseable_email_seen(client, monkeypatch):
    """A permanently-unparseable tagged email must still be marked Seen,
    or it would be retried on every future scan forever."""
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(enabled=True, password="pw"))

    msg = _make_email(subject="[RECIPE] junk", body="nothing useful here")

    with patch("app.routes.email.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.routes.email.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.routes.email.email_client.fetch_full_message", return_value=msg), \
         patch("app.routes.email.email_client.mark_seen") as mock_mark_seen, \
         patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email"):
        r = client.post("/api/email-settings/scan")

    assert r.json()["failed"] == 1
    mock_mark_seen.assert_called_once()
    client.delete("/api/email-settings/password")


def test_scan_disabled_does_nothing(client, monkeypatch):
    from app import crypto

    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    client.delete("/api/email-settings/password")
    r = client.post("/api/email-settings/scan")
    assert r.status_code == 200
    assert r.json()["scanned"] == 0


def test_scan_handles_connection_failure_gracefully(client, monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(enabled=True, password="pw"))

    with patch("app.routes.email.email_client.connect_imap", side_effect=Exception("connection refused")):
        r = client.post("/api/email-settings/scan")

    assert r.status_code == 200, "a connection failure should report cleanly, not 500"
    assert r.json()["scanned"] == 0
    assert any("connect" in m.lower() for m in r.json()["messages"])
    client.delete("/api/email-settings/password")


def test_notification_failure_keeps_items_queued(client, monkeypatch):
    """If SMTP fails, queued notifications must survive for the next
    attempt rather than being silently dropped."""
    from cryptography.fernet import Fernet

    from app import crypto, models
    from app.database import SessionLocal

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(enabled=True, password="pw"))

    msg = _make_email(subject="[RECIPE] Queue Test",
                      body="Queue Test\nIngredients\n1 cup sugar\nInstructions\n1. Stir")

    with patch("app.routes.email.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.routes.email.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.routes.email.email_client.fetch_full_message", return_value=msg), \
         patch("app.routes.email.email_client.mark_seen"), \
         patch("app.routes.email.email_client.connect_smtp", side_effect=Exception("smtp down")):
        client.post("/api/email-settings/scan")

    db = SessionLocal()
    try:
        remaining = db.query(models.EmailNotificationQueueItem).count()
    finally:
        db.close()
    assert remaining >= 1, "notifications should stay queued when sending fails"

    # cleanup
    db = SessionLocal()
    try:
        db.query(models.EmailNotificationQueueItem).delete()
        db.commit()
    finally:
        db.close()
    r = client.get("/api/recipes", params={"q": "Queue Test"})
    for x in r.json():
        if x["title"] == "Queue Test":
            client.delete(f"/api/recipes/{x['id']}")
    client.delete("/api/email-settings/password")


def test_test_email_reports_smtp_failure(client, monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(password="pw"))

    with patch("app.routes.email.email_client.connect_smtp", side_effect=Exception("auth failed")):
        r = client.post("/api/email-settings/test")

    assert r.json()["success"] is False
    assert "SMTP" in r.json()["message"]
    client.delete("/api/email-settings/password")


def test_test_email_success_round_trip(client, monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(password="pw"))

    sent = {}

    def fake_send_email(smtp_conn, from_addr, to_addr, subject, body):
        sent["subject"] = subject

    with patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email", side_effect=fake_send_email), \
         patch("app.routes.email.email_client.connect_imap", return_value=MagicMock()):
        r = client.post("/api/email-settings/test")

    assert r.json()["success"] is True
    assert "[TEST]" in sent["subject"]
    client.delete("/api/email-settings/password")


def test_unusable_attachment_does_not_block_url_fallback(tmp_path):
    """Regression: the URL branch was gated on whether attachment *bytes*
    existed rather than whether an attachment actually yielded a recipe.
    A decorative signature image (or a malformed PDF) alongside a recipe
    link would therefore stop the documented
    'attachment -> link -> body' chain at step one."""
    from app.ingestion.email_processing import process_tagged_email

    class FakeUrlResult:
        title = "Linked Recipe"
        ingredients = ["2 cups flour"]
        steps = ["Mix"]
        raw_text = "Mix"

    # A junk "image" attachment that will fail validation, plus a link.
    msg = _make_email(
        body="Recipe here: https://example.com/recipe",
        attachment=b"not a real image at all",
        attach_type="image/png", filename="signature.png",
    )
    with patch("app.ingestion.email_processing.ingest_url", return_value=FakeUrlResult()):
        result = process_tagged_email(msg, str(tmp_path))

    assert result["success"] is True, "an unusable attachment must not block the URL fallback"
    assert result["title"] == "Linked Recipe"
    assert "URL in body" in result["source_detail"]


def test_mark_seen_uses_parenthesized_flag_list():
    """RFC 3501's STORE grammar requires the flag list be parenthesized.
    Gmail/Dovecot tolerate the bare form; stricter servers don't."""
    from unittest.mock import MagicMock

    from app import email_client

    conn = MagicMock()
    email_client.mark_seen(conn, b"1")
    args = conn.store.call_args[0]
    assert args[2] == "(\\Seen)", f"expected parenthesized flag list, got {args[2]!r}"


def test_inline_signature_image_is_not_treated_as_attachment(tmp_path):
    """Decorative inline images (signature graphics, newsletter logos)
    carry Content-Disposition: inline WITH a filename. Keying on the
    filename alone sent them through OCR, where e.g. a phone number in a
    signature can be mistaken for an ingredient line."""
    from email.message import EmailMessage

    from app.ingestion.email_processing import extract_email_parts

    msg = EmailMessage()
    msg["Subject"] = "[RECIPE] with signature"
    msg.set_content("Body Recipe\nIngredients\n2 cups flour\nInstructions\n1. Mix")
    msg.add_related(TINY_PNG, maintype="image", subtype="png",
                    filename="signature.png", disposition="inline")

    parts = extract_email_parts(msg)
    assert parts["image_bytes"] is None, "an inline image must not be picked up as an attachment"
    assert "flour" in parts["body_text"]


def test_genuine_attachment_still_detected(tmp_path):
    """The inline fix must not break real attachments."""
    from email.message import EmailMessage

    from app.ingestion.email_processing import extract_email_parts

    msg = EmailMessage()
    msg["Subject"] = "[RECIPE] photo"
    msg.set_content("See attached.")
    msg.add_attachment(TINY_PNG, maintype="image", subtype="png", filename="card.png")

    parts = extract_email_parts(msg)
    assert parts["image_bytes"] is not None


# ---------------------------------------------------------------------------
# In-app key generation (encryption.key in the data directory)
# ---------------------------------------------------------------------------

@pytest.fixture
def no_key(client, monkeypatch):
    """No env var and no key file, before and after. The key file lives in
    the session-wide data dir, so a test that leaves one behind would make
    every later "no key configured" test silently pass for the wrong reason."""
    from app import crypto

    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    path = crypto.key_file_path()
    if os.path.exists(path):
        os.remove(path)
    client.delete("/api/email-settings/password")
    yield path
    if os.path.exists(path):
        os.remove(path)
    client.delete("/api/email-settings/password")


def test_generate_key_file_enables_saving_a_password(client, no_key):
    r = client.get("/api/email-settings")
    assert r.json()["encryption_configured"] is False
    assert r.json()["encryption_source"] is None

    r = client.post("/api/email-settings/encryption-key")
    assert r.status_code == 200
    assert r.json()["encryption_configured"] is True
    assert r.json()["encryption_source"] == "file"

    assert os.path.isfile(no_key)
    assert (os.stat(no_key).st_mode & 0o777) == 0o600

    r = client.put("/api/email-settings", json=_settings_payload(password="app-pw"))
    assert r.status_code == 200
    assert r.json()["password_set"] is True

    from sqlalchemy import text

    from app import crypto
    from app.database import engine
    with engine.connect() as conn:
        stored = conn.execute(text("SELECT password_encrypted FROM email_ingest_settings WHERE id=1")).scalar()
    assert "app-pw" not in stored
    assert crypto.decrypt_secret(stored) == "app-pw"


def test_generate_key_refuses_to_replace_an_existing_key_file(client, no_key):
    assert client.post("/api/email-settings/encryption-key").status_code == 200
    with open(no_key) as f:
        first = f.read()
    r = client.post("/api/email-settings/encryption-key")
    assert r.status_code == 409
    with open(no_key) as f:
        assert f.read() == first


def test_generate_key_refuses_when_env_var_is_set(client, no_key, monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    r = client.post("/api/email-settings/encryption-key")
    assert r.status_code == 409
    assert not os.path.exists(no_key)


def test_invalid_env_var_is_not_silently_replaced_by_key_file(client, no_key, monkeypatch):
    """An env var that is set but broken must not quietly fall back to the
    key file: which key is in use would change without anyone seeing why."""
    from app import crypto

    assert client.post("/api/email-settings/encryption-key").status_code == 200
    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, "not-a-valid-key")
    assert crypto.key_source() == "env_invalid"
    assert crypto.encryption_configured() is False

    r = client.put("/api/email-settings", json=_settings_payload(password="x"))
    assert r.status_code == 400
    assert "compose" in r.json()["detail"]


def test_missing_key_error_tells_the_user_what_to_do(client, no_key):
    r = client.put("/api/email-settings", json=_settings_payload(password="x"))
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "Set up encryption" in detail
    # The old message was a Python one-liner to paste into a shell.
    assert "python3" not in detail


def test_backup_zip_does_not_contain_the_key_file(client, no_key):
    import zipfile

    assert client.post("/api/email-settings/encryption-key").status_code == 200
    r = client.get("/api/backup/database.zip")
    assert r.status_code == 200
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert not any(n.endswith("encryption.key") for n in names)


# ---------------------------------------------------------------------------
# Review round: send headers, scan serialization, key race
# ---------------------------------------------------------------------------

def test_send_email_sets_date_and_utf8_body():
    import email as email_lib

    from app import email_client

    conn = MagicMock()
    email_client.send_email(conn, "a@example.com", "b@example.com",
                            "[SUCCESS] Crème brûlée", "Pâte à choux — saved")
    raw = conn.sendmail.call_args[0][2]
    raw.encode("ascii")  # smtplib requires an ASCII-safe string
    msg = email_lib.message_from_string(raw)
    assert msg["Date"]
    assert msg.get_content_charset() == "utf-8"
    assert msg.get_payload(decode=True).decode("utf-8") == "Pâte à choux — saved"


def test_concurrent_scan_is_refused_not_duplicated(client):
    from app import main, routes

    assert routes.email._email_scan_lock.acquire(blocking=False)
    try:
        r = client.post("/api/email-settings/scan")
        assert r.status_code == 200
        assert "already running" in r.json()["messages"][0]
    finally:
        routes.email._email_scan_lock.release()
    # And the lock is free again afterwards.
    r = client.post("/api/email-settings/scan")
    assert "already running" not in " ".join(r.json()["messages"])


def test_lost_key_creation_race_is_409_not_500(client, no_key):
    from app import crypto

    with patch.object(crypto, "generate_key_file", side_effect=FileExistsError()):
        r = client.post("/api/email-settings/encryption-key")
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# A key file that's there but damaged (Qwen review)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("content", [b"not a key\n", b"", b"\xff\xfe"])
def test_damaged_key_file_is_reported_as_damaged(tmp_path, monkeypatch, content):
    from app import crypto
    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    monkeypatch.setenv("RECIPE_APP_DATA_DIR", str(tmp_path))
    (tmp_path / crypto.KEY_FILE_NAME).write_bytes(content)
    assert crypto.key_source() == "file_invalid"
    assert crypto.encryption_configured() is False
    with pytest.raises(crypto.EncryptionNotConfiguredError, match="doesn't contain a valid key"):
        crypto.encrypt_secret("x")
    with pytest.raises(crypto.EncryptionNotConfiguredError, match="doesn't contain a valid key"):
        crypto.decrypt_secret("x")


def test_no_key_file_is_still_reported_as_not_set_up(tmp_path, monkeypatch):
    from app import crypto
    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    monkeypatch.setenv("RECIPE_APP_DATA_DIR", str(tmp_path))
    assert crypto.key_source() is None


def test_damaged_key_message_is_translated():
    from app import crypto, i18n
    fr = i18n.translate("The password wasn't saved. " + crypto.FILE_INVALID_HELP, "fr")
    assert fr.startswith("Le mot de passe n'a pas été enregistré. encryption.key, dans le dossier")
    assert "valid key" not in fr


def seg(text):
    from app.ingestion.pdf_ingest import rule_segment_raw_text
    return rule_segment_raw_text(text)


ING = "Ingrédients\n250 g de farine\n1 pincée de sel\n\nPréparation\n1. Mélanger.\n2. Cuire 20 minutes.\n"


def _nested(levels):
    body = "Content-Type: text/plain\r\n\r\nhi\r\n"
    for i in range(levels):
        b = f"b{i}"
        body = f"Content-Type: multipart/mixed; boundary={b}\r\n\r\n--{b}\r\n{body}--{b}--\r\n"
    return ("From: a@example.com\r\nSubject: [RECIPE]\r\n" + body).encode()


class _Imap:
    def __init__(self, raw):
        self.raw = raw

    def fetch(self, msg_id, what):
        if "SIZE" in what:
            return "OK", [b"1 (RFC822.SIZE 100)"]
        return "OK", [(b"1", self.raw)]


ROOT = Path(__file__).resolve().parents[2]


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
    return (patch("app.routes.email.email_client.search_unseen_by_subject", return_value=[]),
            patch("app.routes.email.email_client.mark_seen"))


FR = {"X-App-Lang": "fr"}


@pytest.fixture
def mail(client, monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto, main, routes
    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    routes.email._recipe_email_times.clear()
    body = {"enabled": False, "imap_host": "mail.me.example", "imap_port": 993, "imap_use_ssl": True,
            "smtp_host": "mail.me.example", "smtp_port": 465, "smtp_use_tls": False,
            "username": "pantry@me.example", "password": "s3cret", "notify_email": "me@me.example",
            "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 0}
    assert client.put("/api/email-settings", json=body).status_code == 200
    yield body
    client.put("/api/email-settings/recipients", json={"recipients": []})
    client.put("/api/email-settings", json=body)  # back to English
    client.delete("/api/email-settings/password")
    routes.email._recipe_email_times.clear()


class _RecordingImap:
    """Records what was fetched."""
    def __init__(self, size, from_header=b"From: friend@example.com\r\n\r\n"):
        self.size, self.from_header, self.asked = size, from_header, []

    def fetch(self, msg_id, what):
        self.asked.append(what)
        if what == "(RFC822.SIZE)":
            return "OK", [f"1 (RFC822.SIZE {self.size})".encode()]
        if "HEADER.FIELDS (FROM)" in what:
            return "OK", [(b"1 (BODY[HEADER.FIELDS (FROM)]<0> {%d}" % len(self.from_header), self.from_header)]
        raise AssertionError(f"fetched {what}")


def _email(**over):
    base = {"enabled": True, "imap_host": "mail.me.example", "imap_port": 993, "imap_use_ssl": True,
            "smtp_host": "mail.me.example", "smtp_port": 465, "smtp_use_tls": False,
            "username": "me@me.example", "notify_email": "me@me.example",
            "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 30}
    base.update(over)
    return base


@pytest.fixture
def key(monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto
    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())


def _email_off(**over):
    base = {"enabled": False, "imap_host": "mail.me.example", "imap_port": 993, "imap_use_ssl": True,
            "smtp_host": "mail.me.example", "smtp_port": 465, "smtp_use_tls": False,
            "username": "me@me.example", "notify_email": "me@me.example",
            "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 30}
    base.update(over)
    return base


@pytest.mark.parametrize("greeting", ["Salut!", "Bonjour à tous,", "Hi Marc,", "Hello!", "Coucou !", "Dear Anna,"])
def test_a_greeting_is_not_the_title(greeting):
    assert seg(f"{greeting}\nVoici la recette promise.\n\nPâte brisée\n\n{ING}")["title_guess"] == "Pâte brisée"


def test_a_name_starting_with_a_greeting_word_is_still_a_name():
    assert seg(f"Hello Dolly Bars\n\n{ING}")["title_guess"] == "Hello Dolly Bars"


@pytest.mark.parametrize("sign_off", ["Bonne cuisine!\nJulie", "Bon appétit!", "Enjoy!\nSam", "Cheers,\nSam",
                                      "Best regards,\nSam", "À bientôt!"])
def test_a_sign_off_is_not_part_of_the_last_step(sign_off):
    assert seg(f"Pâte brisée\n\n{ING}\n{sign_off}\n")["steps"] == ["Mélanger.", "Cuire 20 minutes."]


def test_a_last_step_beginning_like_a_sign_off_stays():
    steps = seg("Pâte brisée\n\nIngredients\n1 cup flour\n\nMethod\n1. Mix.\n2. Best served warm.\n3. Enjoy with tea.\n")["steps"]
    assert steps == ["Mix.", "Best served warm.", "Enjoy with tea."]


@pytest.mark.parametrize("levels", [60, 1200])
def test_a_deeply_nested_email_is_unreadable_not_retried(levels):
    from app import email_client
    with pytest.raises(email_client.MessageUnreadableError):
        email_client.fetch_full_message(_Imap(_nested(levels)), b"1")
    assert email_client.fetch_full_message(_Imap(_nested(3)), b"1")["Subject"] == "[RECIPE]"


def test_the_scan_marks_it_read_and_refuses_a_stranger_before_downloading(client, monkeypatch):
    from app import email_client
    from app.database import SessionLocal
    from app.routes import email as email_routes
    with SessionLocal() as db:
        s = email_routes._get_email_settings(db)
        s.enabled, s.imap_host, s.username, s.password_encrypted = True, "imap.example.com", "u", "x"
        s.allowed_senders = "friend@example.com"
        db.commit()
    seen = []
    monkeypatch.setattr(email_routes.crypto, "decrypt_secret", lambda v: "pw")
    monkeypatch.setattr(email_client, "connect_imap", lambda *a, **k: type("I", (), {"logout": lambda self: None})())
    monkeypatch.setattr(email_client, "search_unseen_by_subject", lambda imap, kw: [b"1", b"2"])
    monkeypatch.setattr(email_client, "fetch_sender", lambda imap, i: "Stranger <bad@x.example>" if i == b"1" else "friend@example.com")

    def full(imap, i, **kw):
        assert i == b"2", "the stranger's email was downloaded"
        raise email_client.MessageUnreadableError("email can't be read: its parts are nested too deeply")
    monkeypatch.setattr(email_client, "fetch_full_message", full)
    monkeypatch.setattr(email_client, "mark_seen", lambda imap, i: seen.append(i))
    try:
        out = email_routes.run_email_scan()
        assert seen == [b"1", b"2"]                       # both marked read: neither retried
        assert any(m.startswith("IGNORED: an email from Stranger") for m in out["messages"])
        assert any("nested too deeply" in m for m in out["messages"])
    finally:
        from sqlalchemy import text
        with SessionLocal() as db:
            s = email_routes._get_email_settings(db)
            s.enabled, s.imap_host, s.username, s.password_encrypted, s.allowed_senders = False, None, None, None, ""
            db.execute(text("DELETE FROM email_notification_queue"))   # its lines aren't the next test's
            db.commit()


def test_the_daily_scan_catches_up():
    from app.routes.email import _scan_due
    tz = datetime.now().astimezone().tzinfo
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    yesterday = (now - timedelta(days=1)).replace(tzinfo=tz)
    assert _scan_due(now.replace(hour=2), 2, yesterday)          # the hour itself
    assert _scan_due(now, 2, yesterday)                           # missed at 2: caught up later that day
    assert not _scan_due(now, 2, now.replace(hour=3, tzinfo=tz))  # already scanned today
    assert not _scan_due(now.replace(hour=1), 2, yesterday)       # not yet
    assert not _scan_due(now, 2, None)                            # never scanned: only at the hour


def test_the_inbox_login_checks_the_certificate(monkeypatch):
    from app import email_client
    seen = {}

    class FakeIMAP:
        def __init__(self, host, port, **kw):
            seen.update(kw)

        def login(self, *a):
            pass

        def select(self, *a):
            pass

    monkeypatch.setattr(email_client.imaplib, "IMAP4_SSL", FakeIMAP)
    email_client.connect_imap("imap.example.com", 993, "u", "p")
    ctx = seen["ssl_context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_an_unknown_charset_is_read_as_utf8():
    from app.ingestion.email_processing import extract_email_parts
    for charset in ("unknown-8bit", "x-user-defined"):
        raw = (f"From: a@example.com\r\nSubject: [RECIPE]\r\nContent-Type: text/plain; charset={charset}\r\n"
               "Content-Transfer-Encoding: 8bit\r\n\r\nCrème brûlée https://example.com/r\r\n").encode("utf-8")
        parts = extract_email_parts(email.message_from_bytes(raw))
        assert "Crème brûlée" in parts["body_text"], charset


def test_a_scheduled_scan_failure_is_logged_in_full():
    tree = ast.parse((ROOT / "backend/app/routes/email.py").read_text(encoding="utf-8"))
    loop = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_daily_email_scan_loop")
    handlers = [h for h in ast.walk(loop) if isinstance(h, ast.ExceptHandler)]
    assert handlers and all("log.exception" in ast.unparse(h) for h in handlers)


def test_a_codec_that_isnt_a_text_encoding_is_read_as_utf8():
    from app.ingestion.email_processing import extract_email_parts
    for charset in ("base64", "hex", "rot13", "unknown-8bit"):
        raw = (f"From: a@example.com\r\nSubject: [RECIPE]\r\nContent-Type: text/plain; charset={charset}\r\n"
               "Content-Transfer-Encoding: 8bit\r\n\r\nCrème brûlée https://example.com/r\r\n").encode("utf-8")
        parts = extract_email_parts(email.message_from_bytes(raw))
        assert "Crème brûlée" in parts["body_text"], charset


def test_a_reading_failure_leaves_no_attachment_behind(tmp_path, monkeypatch):
    from weasyprint import HTML

    from app.ingestion import email_processing
    from tests.test_email_ingest import _make_email

    def boom(text):
        raise RuntimeError("segmenter broke")
    monkeypatch.setattr(email_processing, "segment_raw_text", boom)
    pdf = HTML(string="<h1>X</h1><p>2 cups flour</p>").write_pdf()
    msg = _make_email(body="See attached.", attachment=pdf, attach_type="application/pdf", filename="r.pdf")
    result = email_processing.process_tagged_email(msg, str(tmp_path))
    assert result["success"] is False and "Could not read PDF attachment" in result["error"]
    assert list(tmp_path.iterdir()) == []


def test_emailed_link_keeps_its_times(client, monkeypatch, tmp_path):
    from email.message import EmailMessage

    from app.ingestion import email_processing
    from app.ingestion.url_ingest import UrlIngestResult
    result = UrlIngestResult(title="Timed Soup", ingredients=["1 l stock"], steps=["Simmer."], raw_text="x",
                             servings="4", prep_time="10 min", cook_time="PT30M", total_time="40 min")
    result.final_url = "https://example.com/timed-soup"
    monkeypatch.setattr(email_processing, "ingest_url", lambda url: result)
    msg = EmailMessage()
    msg["Subject"] = "[RECIPE] soup"
    msg.set_content("https://example.com/timed-soup")
    out = email_processing.process_tagged_email(msg, str(tmp_path))
    assert out["success"] and (out["prep_time"], out["cook_time"], out["total_time"]) == ("10 min", "PT30M", "40 min")
    from app.main import SessionLocal
    from app.routes.email import _save_email_recipe
    db = SessionLocal()
    try:
        rid = _save_email_recipe(db, out, "soup")
    finally:
        db.close()
    r = client.get(f"/api/recipes/{rid}").json()
    try:
        assert (r["prep_time"], r["cook_time"], r["total_time"]) == ("10 min", "PT30M", "40 min")
    finally:
        client.delete(f"/api/recipes/{rid}", headers={"X-Requested-With": "OpenThePantry"})


def test_email_settings_fields_are_bounded(client):
    r = client.put("/api/email-settings", json={"password": "p" * 1025})
    assert r.status_code == 422
    r = client.put("/api/email-settings", json={"imap_host": "h" * 254})
    assert r.status_code == 422


def test_emailed_recipe_is_held_to_the_api_limits(client, data_dir):
    from app import main, models, routes
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        rid = routes.email._save_email_recipe(db, {
            "title": "T" * 900, "source_url": "https://example.com/" + "a" * 3000, "servings": "4" * 300,
            "raw_text": "x" * 1_100_000, "ingredients": [], "steps": [], "tags": []}, "subject")
        r = db.get(models.Recipe, rid)
        assert len(r.title) == 500 and r.source_url is None and len(r.servings) == 100 and len(r.raw_text) == 1_000_000
    finally:
        db.close()
    client.delete(f"/api/recipes/{rid}")
    client.delete(f"/api/recently-deleted/{rid}")


def test_a_damaged_key_file_is_reported_once(tmp_path, monkeypatch):
    from app import crypto
    key = tmp_path / "encryption.key"
    key.write_text("not a key")
    monkeypatch.setattr(crypto, "key_file_path", lambda: str(key))
    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(crypto, "_warned_invalid_file", None)
    warned = []
    monkeypatch.setattr(crypto.log, "warning", lambda msg, *a, **k: warned.append(msg))
    for _ in range(5):
        assert crypto.key_source() == "file_invalid"
    assert len(warned) == 1
    key.write_text("still not a key, changed")
    crypto.key_source()
    assert len(warned) == 2


def test_broken_sending_is_shown_in_settings_after_a_scan(client, monkeypatch):
    """The 3 AM scan's only way to report was the email that couldn't be
    sent. Now Settings says what went wrong and how much is waiting."""
    _enable(client, monkeypatch)
    from app.main import SessionLocal
    from app.routes.email import _queue_notification
    db = SessionLocal(); _queue_notification(db, True, "Pancakes"); db.commit(); db.close()
    s1, s2 = _no_mail()
    with patch("app.routes.email.email_client.connect_imap", return_value=MagicMock()), s1, s2, \
         patch("app.routes.email.email_client.connect_smtp", side_effect=Exception("535 authentication failed")):
        client.post("/api/email-settings/scan")
    got = client.get("/api/email-settings").json()
    assert "535 authentication failed" in got["last_problem"]
    assert got["last_problem_at"] and got["pending_notifications"] == 1

    # Next scan, sending works: queue goes out, warning clears.
    with patch("app.routes.email.email_client.connect_imap", return_value=MagicMock()), s1, s2, \
         patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email"):
        client.post("/api/email-settings/scan")
    got = client.get("/api/email-settings").json()
    assert got["last_problem"] is None and got["pending_notifications"] == 0
    client.delete("/api/email-settings/password")


def test_inbox_connection_failure_is_shown_in_settings(client, monkeypatch):
    _enable(client, monkeypatch)
    with patch("app.routes.email.email_client.connect_imap", side_effect=Exception("timed out")):
        client.post("/api/email-settings/scan")
    assert "inbox" in client.get("/api/email-settings").json()["last_problem"]

    # A passing Send test email clears it.
    with patch("app.routes.email.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email"):
        assert client.post("/api/email-settings/test").json()["success"] is True
    assert client.get("/api/email-settings").json()["last_problem"] is None
    client.delete("/api/email-settings/password")


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


def test_notification_email_uses_the_saved_language(client, mail):
    from app.database import SessionLocal
    from app.routes.email import _flush_notifications, _get_email_settings, _queue_notification
    client.put("/api/email-settings", json={**mail, "password": None}, headers=FR)
    db = SessionLocal()
    try:
        _queue_notification(db, True, 'Tarte (from "[RECIPE] tarte", via email body text)')
        db.commit()
        with patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
             patch("app.routes.email.email_client.send_email") as send:
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


def test_an_oversized_email_is_refused_before_its_header(client, monkeypatch):
    from app import email_client
    from app.database import SessionLocal
    from app.routes import email as email_routes
    imap = _RecordingImap(40 * 1024 * 1024)
    with SessionLocal() as db:
        s = email_routes._get_email_settings(db)
        s.enabled, s.imap_host, s.username, s.password_encrypted = True, "imap.example.com", "u", "x"
        s.allowed_senders = "friend@example.com"
        db.commit()
    seen = []
    monkeypatch.setattr(email_routes.crypto, "decrypt_secret", lambda v: "pw")
    monkeypatch.setattr(email_client, "connect_imap", lambda *a, **k: imap)
    imap.logout = lambda: None
    monkeypatch.setattr(email_client, "search_unseen_by_subject", lambda i, kw: [b"1"])
    monkeypatch.setattr(email_client, "mark_seen", lambda i, m: seen.append(m))
    try:
        out = email_routes.run_email_scan()
        assert imap.asked == ["(RFC822.SIZE)"], imap.asked          # no header, no body
        assert seen == [b"1"] and "the limit is 30 MB" in " ".join(out["messages"])
    finally:
        from sqlalchemy import text
        with SessionLocal() as db:
            s = email_routes._get_email_settings(db)
            s.enabled, s.imap_host, s.username, s.password_encrypted, s.allowed_senders = False, None, None, None, ""
            db.execute(text("DELETE FROM email_notification_queue"))
            db.commit()


def test_the_from_header_is_fetched_bounded_and_shown_short():
    from app import email_client
    long_from = b"From: " + b"a" * 5000 + b"@example.com\r\n\r\n"
    imap = _RecordingImap(1000, long_from)
    sender = email_client.fetch_sender(imap, b"1")
    assert "<0.2048>" in imap.asked[0]
    assert len(sender) <= email_client.SENDER_SHOWN


def test_email_times_are_sent_with_their_time_zone(client, mail):
    from app import main, routes
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        s = routes.email._get_email_settings(db)
        s.last_scan_at = routes.common._utc_now()
        db.commit()
    finally:
        db.close()
    got = client.get("/api/email-settings").json()["last_scan_at"]
    assert got.endswith("+00:00"), got


def test_host_typo_fix_explains_the_cleared_password(client, key):
    """Fixing a host typo without retyping the password used to say "a
    password is required", even though one was saved -- the saved one
    hadn't actually been lost, only cleared by this same request because
    the host changed."""
    client.put("/api/email-settings", json=_email(password="s3cret", imap_host="mial.me.example"))
    r = client.put("/api/email-settings", json=_email(imap_host="mail.me.example"))
    assert r.status_code == 400
    assert "clear" in r.json()["detail"].lower()
    client.delete("/api/email-settings/password")


def test_password_cleared_flag_on_a_successful_save(client, key):
    client.put("/api/email-settings", json=_email(enabled=False, password="s3cret"))
    r = client.put("/api/email-settings", json=_email(enabled=False, smtp_host="new.example"))
    assert r.status_code == 200
    assert r.json()["password_cleared"] is True
    assert r.json()["password_set"] is False
    client.delete("/api/email-settings/password")


def test_bare_domain_allowlist_entry_matches_its_addresses():
    from app.routes.email import _parse_allowed_senders, _sender_allowed

    allowed = _parse_allowed_senders("example.com, someone@other.example")
    assert _sender_allowed("Person <cook@example.com>", allowed)
    assert _sender_allowed("someone@other.example", allowed)
    assert not _sender_allowed("cook@notexample.com", allowed)


@pytest.mark.parametrize("field", ["imap_host", "smtp_host", "username"])
def test_changing_server_or_username_drops_the_saved_password(client, key, field):
    """Reproduced: change only the hosts, press Send test email, and the
    saved password was sent to attacker.example.net."""
    client.put("/api/email-settings", json=_email_off(password="s3cret"))
    r = client.put("/api/email-settings", json=_email_off(**{field: "attacker.example.net"}))
    assert r.json()["password_set"] is False
    client.delete("/api/email-settings/password")


def test_saving_same_server_keeps_the_password(client, key):
    client.put("/api/email-settings", json=_email_off(password="s3cret"))
    r = client.put("/api/email-settings", json=_email_off(daily_scan_hour=5))
    assert r.json()["password_set"] is True
    client.delete("/api/email-settings/password")


def test_changing_server_with_new_password_keeps_it(client, key):
    client.put("/api/email-settings", json=_email_off(password="s3cret"))
    r = client.put("/api/email-settings", json=_email_off(smtp_host="smtp.new.example", password="n3w"))
    assert r.json()["password_set"] is True
    client.delete("/api/email-settings/password")


def test_sender_allowlist(client, key):
    import app.main as m
    import app.routes.email as m_email
    assert m_email._sender_allowed("Me <me@home.example>", ["me@home.example"])
    assert m_email._sender_allowed("sis@family.example", ["@family.example"])
    assert not m_email._sender_allowed("stranger@spam.example", ["me@home.example", "@family.example"])
    assert m_email._sender_allowed("anyone@x.example", [])       # empty list: anyone, as before


def test_scan_ignores_senders_not_on_the_list(client, key):
    from email.message import EmailMessage
    client.put("/api/email-settings", json=_email_off(enabled=True, password="pw", allowed_senders="me@me.example",
                                                  cooldown_minutes=0))
    msg = EmailMessage()
    msg["Subject"] = "[RECIPE] Stranger Stew"
    msg["From"] = "stranger@spam.example"
    msg.set_content("Stew\nIngredients\n1 onion\nInstructions\n1. Cook")
    with patch("app.routes.email.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.routes.email.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.routes.email.email_client.fetch_full_message", return_value=msg), \
         patch("app.routes.email.email_client.mark_seen") as seen, \
         patch("app.routes.email._run_heavy") as processed:
        body = client.post("/api/email-settings/scan").json()
    processed.assert_not_called()
    seen.assert_called_once()
    assert any(line.startswith("IGNORED:") for line in body["messages"])
    client.delete("/api/email-settings/password")


def test_email_images_are_not_opened_before_the_magic_check():
    """_image_size gave inbound email images to every Pillow decoder."""
    from app.ingestion.email_processing import _image_size
    with patch("PIL.Image.open") as opened:
        assert _image_size(b"BM" + b"\x00" * 100) is None          # BMP: not accepted
    opened.assert_not_called()


def test_huge_notification_cooldown_is_rejected(client):
    r = client.put("/api/email-settings", json={
        "enabled": False, "imap_host": "h", "imap_port": 993, "imap_use_ssl": True, "smtp_host": "h",
        "smtp_port": 465, "smtp_use_tls": False, "username": "u", "notify_email": "u@x",
        "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 10 ** 12})
    assert r.status_code == 422


def test_email_settings_report_server_timezone(client):
    assert "server_timezone" in client.get("/api/email-settings").json()


def test_a_scheduled_scan_that_cant_reach_the_inbox_is_tried_again_in_an_hour(monkeypatch):
    """Review of 0169: the day was marked done before the scan, so one that
    couldn't reach the inbox (a NAS rebooting) waited until tomorrow."""
    import asyncio
    from datetime import datetime as real_dt
    from datetime import timedelta
    from types import SimpleNamespace

    from app.routes import email as email_routes
    clock = [real_dt(2026, 11, 10, 6, 0)]
    state = SimpleNamespace(enabled=True, daily_scan_hour=6, last_scan_at=None)
    scans = []

    class Clock(real_dt):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    def scan():
        scans.append(clock[0])
        if len(scans) >= 3:                                   # the inbox is back
            state.last_scan_at = clock[0]

    ticks = [0]

    async def sleep(_):
        ticks[0] += 1
        if ticks[0] > 1:
            clock[0] += timedelta(minutes=15)
        if ticks[0] > 30:
            raise asyncio.CancelledError

    monkeypatch.setattr(email_routes, "datetime", Clock)
    monkeypatch.setattr(email_routes, "_get_email_settings", lambda db: state)
    monkeypatch.setattr(email_routes, "run_email_scan", scan)
    monkeypatch.setattr(email_routes.asyncio, "sleep", sleep)
    monkeypatch.setattr(email_routes.learning, "schedule_retrain", lambda: None)
    try:
        asyncio.run(email_routes._daily_email_scan_loop())
    except asyncio.CancelledError:
        pass
    assert [t.strftime("%H:%M") for t in scans] == ["06:00", "07:00", "08:00"]


def test_links_over_2000_characters_arent_taken_from_an_email():
    from app.ingestion.email_processing import extract_email_parts
    long_link = "https://example.com/r?" + "a" * 2500
    raw = ("From: a@example.com\r\nSubject: [RECIPE]\r\nContent-Type: text/plain\r\n\r\n"
           f"{long_link}\r\nhttps://example.com/soup\r\n").encode()
    assert extract_email_parts(email.message_from_bytes(raw))["urls"] == ["https://example.com/soup"]
