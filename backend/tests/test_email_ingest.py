import io
import os
from email.message import EmailMessage
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
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
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

    with patch("app.main.email_client.connect_imap", return_value=fake_imap), \
         patch("app.main.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.main.email_client.fetch_full_message", return_value=msg), \
         patch("app.main.email_client.mark_seen") as mock_mark_seen, \
         patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email", side_effect=fake_send_email):
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

    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.main.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.main.email_client.fetch_full_message", return_value=msg), \
         patch("app.main.email_client.mark_seen") as mock_mark_seen, \
         patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email"):
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

    with patch("app.main.email_client.connect_imap", side_effect=Exception("connection refused")):
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

    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.main.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.main.email_client.fetch_full_message", return_value=msg), \
         patch("app.main.email_client.mark_seen"), \
         patch("app.main.email_client.connect_smtp", side_effect=Exception("smtp down")):
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

    with patch("app.main.email_client.connect_smtp", side_effect=Exception("auth failed")):
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

    with patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email", side_effect=fake_send_email), \
         patch("app.main.email_client.connect_imap", return_value=MagicMock()):
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

    from app import crypto
    from app.database import engine
    from sqlalchemy import text
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
    from app import main

    assert main._email_scan_lock.acquire(blocking=False)
    try:
        r = client.post("/api/email-settings/scan")
        assert r.status_code == 200
        assert "already running" in r.json()["messages"][0]
    finally:
        main._email_scan_lock.release()
    # And the lock is free again afterwards.
    r = client.post("/api/email-settings/scan")
    assert "already running" not in " ".join(r.json()["messages"])


def test_lost_key_creation_race_is_409_not_500(client, no_key):
    from app import crypto

    with patch.object(crypto, "generate_key_file", side_effect=FileExistsError()):
        r = client.post("/api/email-settings/encryption-key")
    assert r.status_code == 409
