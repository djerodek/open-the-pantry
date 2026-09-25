"""Email ingest against the message shapes and server setups real mail
clients produce. Most of these would have failed before: see each docstring.

As in test_backup.py, app.* is imported inside test bodies only -- the
conftest fixtures set RECIPE_APP_DATA_DIR, and a module-scope import would
run before they do (that broke CI once already).
"""
import io
import socket
import threading
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import message_from_bytes
from unittest.mock import MagicMock, patch

import pytest


def _jpeg(w, h):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 180, 150)).save(buf, "JPEG")
    return buf.getvalue()


def _roundtrip(m):
    return message_from_bytes(m.as_bytes())


def _apple_mail(part):
    """Apple Mail (iPhone and Mac): multipart/mixed, the file marked
    `inline` with a filename so it shows in the body."""
    m = MIMEMultipart("mixed")
    m.attach(MIMEText("Grandma's recipe\n", "plain"))
    m.attach(part)
    m.attach(MIMEText("\nSent from my iPhone", "plain"))
    return _roundtrip(m)


def test_apple_mail_inline_photo_is_used():
    """Regression: only Content-Disposition: attachment was accepted, and
    Apple Mail marks photos inline -- every photo from an iPhone was
    ignored."""
    from app.ingestion.email_processing import extract_email_parts

    img = MIMEImage(_jpeg(1200, 900), "jpeg", name="IMG_4021.jpeg")
    img.add_header("Content-Disposition", "inline", filename="IMG_4021.jpeg")
    parts = extract_email_parts(_apple_mail(img))
    assert parts["image_bytes"] is not None
    assert parts["image_name"] == "IMG_4021.jpeg"


def test_apple_mail_inline_pdf_is_used():
    from app.ingestion.email_processing import extract_email_parts

    pdf = MIMEApplication(b"%PDF-1.4\n" + b"x" * 100, "pdf", name="recipe.pdf")
    pdf.add_header("Content-Disposition", "inline", filename="recipe.pdf")
    parts = extract_email_parts(_apple_mail(pdf))
    assert parts["pdf_bytes"] is not None


def test_photo_in_html_related_part_is_used():
    """iOS Mail HTML message with the photo placed in the body: it sits in
    multipart/related and is referenced by cid, exactly like a signature
    logo. Size is what tells them apart."""
    from app.ingestion.email_processing import extract_email_parts

    m = MIMEMultipart("alternative")
    m.attach(MIMEText("Grandma's recipe", "plain"))
    rel = MIMEMultipart("related")
    rel.attach(MIMEText('<div>Grandma\'s recipe<img src="cid:p1"></div>', "html"))
    img = MIMEImage(_jpeg(1600, 1200), "jpeg", name="IMG_4022.jpeg")
    img.add_header("Content-ID", "<p1>")
    img.add_header("Content-Disposition", "inline", filename="IMG_4022.jpeg")
    rel.attach(img)
    m.attach(rel)
    assert extract_email_parts(_roundtrip(m))["image_bytes"] is not None


def test_signature_logo_still_skipped_and_reason_recorded():
    """What the old rule protected against still holds: a small inline
    logo isn't OCR'd, and the link in the same email is found instead."""
    from app.ingestion.email_processing import extract_email_parts

    m = MIMEMultipart("alternative")
    m.attach(MIMEText("Here's the link https://example.com/r", "plain"))
    rel = MIMEMultipart("related")
    rel.attach(MIMEText('<p>link</p><img src="cid:logo1">', "html"))
    logo = MIMEImage(_jpeg(180, 60), "jpeg", name="logo.jpg")
    logo.add_header("Content-ID", "<logo1>")
    logo.add_header("Content-Disposition", "inline", filename="logo.jpg")
    rel.attach(logo)
    m.attach(rel)
    parts = extract_email_parts(_roundtrip(m))
    assert parts["image_bytes"] is None
    assert parts["urls"] == ["https://example.com/r"]
    assert any("logo.jpg" in s and "too small" in s for s in parts["skipped"])


def test_largest_inline_photo_wins():
    from app.ingestion.email_processing import extract_email_parts

    m = MIMEMultipart("mixed")
    m.attach(MIMEText("two photos", "plain"))
    for name, size in (("banner.jpg", (700, 200)), ("card.jpg", (2000, 1500))):
        p = MIMEImage(_jpeg(*size), "jpeg", name=name)
        p.add_header("Content-Disposition", "inline", filename=name)
        m.attach(p)
    parts = extract_email_parts(_roundtrip(m))
    assert parts["image_name"] == "card.jpg"


def test_html_only_email_link_is_found():
    """Some apps' Share -> Mail sends HTML only, with the URL hidden behind
    link text. There was no text/plain part, so the body was empty."""
    from app.ingestion.email_processing import extract_email_parts

    m = MIMEMultipart("alternative")
    m.attach(MIMEText('<p>Try this: <a href="https://www.example.com/best-lasagna">Best Lasagna</a></p>', "html"))
    parts = extract_email_parts(_roundtrip(m))
    assert parts["urls"] == ["https://www.example.com/best-lasagna"]


def test_same_link_twice_counts_as_one():
    from app.ingestion.email_processing import extract_email_parts

    m = MIMEText("https://example.com/r\n\nhttps://example.com/r.", "plain")
    assert extract_email_parts(_roundtrip(m))["urls"] == ["https://example.com/r"]


def test_failure_reason_says_what_was_tried(tmp_path):
    """The reported case: [RECIPE] with "Test test test" in the body. It
    can't become a recipe, but the reason used to be one fixed sentence
    that didn't say what was checked."""
    from email.message import EmailMessage
    from app.ingestion.email_processing import process_tagged_email

    m = EmailMessage()
    m["Subject"] = "[RECIPE]"
    m.set_content("Test\n\nTest test test\n\nTest test\n\nTest\n")
    result = process_tagged_email(m, str(tmp_path))
    assert result["success"] is False
    assert "no PDF, photo or link" in result["error"]
    assert "body text: no ingredient or step lines" in result["error"]


def test_failed_link_reason_is_reported(tmp_path):
    """A link that fails to load used to be swallowed, leaving "no link"
    in the message even though there was one."""
    from email.message import EmailMessage
    from app.ingestion.email_processing import process_tagged_email

    m = EmailMessage()
    m["Subject"] = "[RECIPE]"
    m.set_content("https://example.com/recipe")
    with patch("app.ingestion.email_processing.ingest_url", side_effect=ValueError("HTTP 403 from the site")):
        result = process_tagged_email(m, str(tmp_path))
    assert "link https://example.com/recipe: HTTP 403 from the site" in result["error"]


# --- TLS mode ---------------------------------------------------------------

def test_smtp_465_uses_implicit_tls_even_with_stale_starttls_flag():
    """Regression: the reported timeout. A row saved before the port-based
    fix still said STARTTLS for port 465; STARTTLS against an implicit-TLS
    listener waits for a greeting that never comes."""
    from app import email_client

    with patch.object(email_client.smtplib, "SMTP_SSL") as ssl_cls, \
         patch.object(email_client.smtplib, "SMTP") as plain_cls:
        email_client.connect_smtp("mail.example.com", 465, "u", "p", use_tls=True)
    ssl_cls.assert_called_once()
    plain_cls.assert_not_called()


def test_smtp_587_uses_starttls():
    from app import email_client

    with patch.object(email_client.smtplib, "SMTP_SSL") as ssl_cls, \
         patch.object(email_client.smtplib, "SMTP") as plain_cls:
        email_client.connect_smtp("mail.example.com", 587, "u", "p", use_tls=True)
    plain_cls.assert_called_once()
    plain_cls.return_value.starttls.assert_called_once()
    ssl_cls.assert_not_called()


def test_imap_993_uses_implicit_tls_even_with_stale_flag():
    from app import email_client

    with patch.object(email_client.imaplib, "IMAP4_SSL") as ssl_cls, \
         patch.object(email_client.imaplib, "IMAP4") as plain_cls:
        email_client.connect_imap("mail.example.com", 993, "u", "p", use_ssl=False)
    ssl_cls.assert_called_once()
    plain_cls.assert_not_called()


def test_saving_settings_derives_tls_flags_from_ports(client, monkeypatch):
    """The server derives the stored flags too, so an old cached app.js
    sending smtp_use_tls=true with port 465 can't store a combination that
    can only time out."""
    from cryptography.fernet import Fernet
    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    payload = {
        "enabled": False, "imap_host": "mail.example.com", "imap_port": 993, "imap_use_ssl": False,
        "smtp_host": "mail.example.com", "smtp_port": 465, "smtp_use_tls": True,
        "username": "me@example.com", "notify_email": "me@example.com",
        "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 30,
    }
    body = client.put("/api/email-settings", json=payload).json()
    assert body["smtp_use_tls"] is False   # False = implicit TLS (see email_client)
    assert body["imap_use_ssl"] is True


def _listen(banner: bytes | None):
    """A local TCP server that sends `banner` on connect (or nothing)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)

    def serve():
        for _ in range(4):
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            if banner:
                conn.sendall(banner)
            threading.Timer(3, conn.close).start()

    threading.Thread(target=serve, daemon=True).start()
    return srv


def test_probe_identifies_starttls_port_and_unreachable_port():
    from app import email_client

    srv = _listen(b"220 mail.example.com ESMTP\r\n")
    try:
        assert email_client.probe_port("127.0.0.1", srv.getsockname()[1], timeout=2) == "plaintext"
    finally:
        srv.close()

    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()   # nothing listening now
    assert email_client.probe_port("127.0.0.1", port, timeout=2) == "unreachable"


def test_failure_message_explains_starttls_port_used_as_implicit():
    """A timeout on its own gives nothing to act on. The probe turns it
    into "this port is a STARTTLS port"."""
    from app import email_client

    srv = _listen(b"220 mail.example.com ESMTP\r\n")
    port = srv.getsockname()[1]
    try:
        with patch.object(email_client, "probe_port", wraps=lambda h, p: email_client.probe_port.__wrapped__(h, p) if hasattr(email_client.probe_port, "__wrapped__") else "plaintext"):
            suffix = email_client._diagnose_suffix("127.0.0.1", port, True, TimeoutError("timed out"))
    finally:
        srv.close()
    assert "STARTTLS port" in suffix


# --- test button and scan ---------------------------------------------------

def test_test_button_reports_imap_even_when_smtp_fails(client, monkeypatch):
    """The test used to stop at the first failure, so broken sending hid
    whether reading -- the half that ingests recipes -- worked at all."""
    from cryptography.fernet import Fernet
    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json={
        "enabled": False, "imap_host": "mail.example.com", "imap_port": 993, "imap_use_ssl": True,
        "smtp_host": "mail.example.com", "smtp_port": 465, "smtp_use_tls": False,
        "username": "me@example.com", "password": "pw", "notify_email": "me@example.com",
        "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 30,
    })
    with patch("app.main.email_client.connect_smtp", side_effect=Exception("timed out")), \
         patch("app.main.email_client.connect_imap", return_value=MagicMock()):
        body = client.post("/api/email-settings/test").json()
    assert body["success"] is False
    assert "Sending (SMTP) failed: timed out" in body["message"]
    assert "Reading: OK" in body["message"]
    client.delete("/api/email-settings/password")


def _enable(client, monkeypatch):
    from cryptography.fernet import Fernet
    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json={
        "enabled": True, "imap_host": "mail.example.com", "imap_port": 993, "imap_use_ssl": True,
        "smtp_host": "mail.example.com", "smtp_port": 465, "smtp_use_tls": False,
        "username": "me@example.com", "password": "pw", "notify_email": "me@example.com",
        "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 0,
    })


def test_scan_shows_reason_and_unsent_notice(client, monkeypatch):
    """The reported scan: the UI said "1 failed" and nothing else, and the
    reason's only other route was an email that SMTP couldn't send."""
    from email.message import EmailMessage

    _enable(client, monkeypatch)
    m = EmailMessage()
    m["Subject"] = "[RECIPE]"
    m.set_content("Test\n\nTest test test")
    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.main.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.main.email_client.fetch_full_message", return_value=m), \
         patch("app.main.email_client.mark_seen") as seen, \
         patch("app.main.email_client.connect_smtp", side_effect=Exception("timed out")):
        body = client.post("/api/email-settings/scan").json()
    assert body["failed"] == 1
    assert any("body text: no ingredient or step lines" in line for line in body["messages"])
    assert any("result email couldn't be sent" in line for line in body["messages"])
    seen.assert_called_once()   # no recipe in it: a retry wouldn't change that
    client.delete("/api/email-settings/password")


def test_scan_leaves_email_unread_after_a_temporary_error(client, monkeypatch):
    """Something failing around the email (fetch, save, disk) can be
    temporary, so the email stays unread for the next scan."""
    _enable(client, monkeypatch)
    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.main.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.main.email_client.fetch_full_message", side_effect=OSError("connection reset")), \
         patch("app.main.email_client.mark_seen") as seen, \
         patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email"):
        body = client.post("/api/email-settings/scan").json()
    assert body["failed"] == 1
    seen.assert_not_called()
    assert "left unread" in body["messages"][0]
    client.delete("/api/email-settings/password")


def test_oversized_email_is_refused_before_download():
    from app import email_client

    imap = MagicMock()
    imap.fetch.return_value = ("OK", [b"1 (RFC822.SIZE 104857600)"])
    with pytest.raises(email_client.MessageTooLargeError):
        email_client.fetch_full_message(imap, b"1")
    # only the size was fetched, never the body
    assert all("BODY" not in str(c.args[1]) for c in imap.fetch.call_args_list)


# --- mail-app footers and multiple links ------------------------------------

def test_outlook_ios_footer_link_does_not_block_the_recipe_link(tmp_path):
    """The reported email: a recipe link sent from Outlook for iOS. Its
    footer ("Get Outlook for iOS<https://aka.ms/o0ukef>") made two links,
    and the link step only ran for exactly one."""
    from app.ingestion.email_processing import process_tagged_email

    m = MIMEMultipart("alternative")
    m["Subject"] = "[RECIPE]"
    m.attach(MIMEText("https://heygrillhey.com/homemade-smoked-bacon/\r\n\r\n"
                      "Get Outlook for iOS<https://aka.ms/o0ukef>\r\n", "plain"))
    m.attach(MIMEText('<div><a href="https://heygrillhey.com/homemade-smoked-bacon/">'
                      'https://heygrillhey.com/homemade-smoked-bacon/</a></div>'
                      '<div>Get <a href="https://aka.ms/o0ukef">Outlook for iOS</a></div>', "html"))

    class Fake:
        title = "Homemade Smoked Bacon"
        ingredients = ["5 lb pork belly"]
        steps = ["Cure", "Smoke"]
        raw_text = ""

    with patch("app.ingestion.email_processing.ingest_url", return_value=Fake()) as fetch:
        result = process_tagged_email(_roundtrip(m), str(tmp_path))
    assert result["success"] is True
    fetch.assert_called_once_with("https://heygrillhey.com/homemade-smoked-bacon/")


@pytest.mark.parametrize("footer", [
    "Sent from my iPhone", "Sent from my Galaxy", "Sent from Outlook for Android",
    "Get Outlook for Android<https://aka.ms/AAb9ysg>", "Sent from Yahoo Mail for iPhone",
])
def test_mail_app_footers_are_stripped(footer):
    from app.ingestion.email_processing import strip_client_boilerplate
    assert strip_client_boilerplate(f"https://example.com/r\n\n{footer}\n").strip() == "https://example.com/r"


def test_signature_after_delimiter_is_ignored():
    from app.ingestion.email_processing import extract_email_parts

    m = MIMEText("https://example.com/r\n\n-- \nJane Doe\nhttps://janedoe.example\n", "plain")
    assert extract_email_parts(_roundtrip(m))["urls"] == ["https://example.com/r"]


def test_links_are_tried_in_order_until_one_works(tmp_path):
    from email.message import EmailMessage
    from app.ingestion.email_processing import process_tagged_email

    class Fake:
        title = "Second"
        ingredients = ["x"]
        steps = ["y"]
        raw_text = ""

    m = EmailMessage()
    m["Subject"] = "[RECIPE]"
    m.set_content("https://a.example/one\nhttps://b.example/two\n")
    with patch("app.ingestion.email_processing.ingest_url",
               side_effect=[ValueError("no recipe on that page"), Fake()]):
        result = process_tagged_email(m, str(tmp_path))
    assert result["success"] is True
    assert "b.example/two" in result["source_detail"]


# --- logging ----------------------------------------------------------------

def _log_text(data_dir):
    import logging
    for h in logging.getLogger("otp").handlers:
        h.flush()
    path = data_dir / "logs" / "app.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_blocked_page_is_diagnosable_from_the_log(client, data_dir, tmp_path):
    """The point of the log: when a link fails, it should say whether the
    site refused (and with what page), not just that nothing was found."""
    from email.message import EmailMessage
    from app.ingestion import url_ingest
    from app.ingestion.email_processing import process_tagged_email

    blocked = MagicMock(status_code=403, is_redirect=False, is_permanent_redirect=False,
                        text="<html><title>Just a moment...</title></html>",
                        content=b"x" * 50, headers={"Content-Type": "text/html", "Server": "cloudflare"})
    blocked.raise_for_status.side_effect = url_ingest.requests.HTTPError("403 Client Error: Forbidden")

    m = EmailMessage()
    m["Subject"] = "[RECIPE]"
    m.set_content("https://blocked.example/recipe\n\nGet Outlook for iOS<https://aka.ms/o0ukef>")
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest.requests, "get", return_value=blocked):
        result = process_tagged_email(m, str(tmp_path))

    assert result["success"] is False
    text = _log_text(data_dir)
    assert "Parts: text/plain" in text
    assert "urls=['https://blocked.example/recipe']" in text        # footer link gone
    assert "GET https://blocked.example/recipe -> 403" in text
    assert "'Just a moment...'" in text and "'cloudflare'" in text
    assert "Traceback" in text                                       # full error, not just the message


def test_passwords_are_not_logged(client, data_dir, monkeypatch):
    from cryptography.fernet import Fernet
    from app import crypto

    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json={
        "enabled": True, "imap_host": "mail.example.com", "imap_port": 993, "imap_use_ssl": True,
        "smtp_host": "mail.example.com", "smtp_port": 465, "smtp_use_tls": False,
        "username": "me@example.com", "password": "s3cret-Pa55", "notify_email": "me@example.com",
        "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 0,
    })
    with patch("app.main.email_client.connect_imap", side_effect=Exception("auth failed")), \
         patch("app.main.email_client.connect_smtp", side_effect=Exception("nope")):
        client.post("/api/email-settings/scan")
        client.post("/api/email-settings/test")
    assert "s3cret-Pa55" not in _log_text(data_dir)
    client.delete("/api/email-settings/password")


def test_browser_errors_reach_the_server_log_and_are_capped(client, data_dir):
    import app.main as m
    m._client_error_times.clear()
    r = client.post("/api/client-error", json={
        "message": "TypeError: x is undefined", "source": "/js/app.js", "line": 120, "column": 7,
        "stack": "renderRecipeList@/js/app.js:120:7", "page": "/#recipe-3", "user_agent": "iPhone",
    })
    assert r.status_code == 204
    text = _log_text(data_dir)
    assert "Browser error on /#recipe-3: TypeError: x is undefined (at /js/app.js:120:7)" in text
    for _ in range(40):
        client.post("/api/client-error", json={"message": "loop"})
    assert _log_text(data_dir).count(": loop (at") <= 19   # 20 a minute, one used above
    m._client_error_times.clear()


def test_previously_silent_errors_are_logged(client, data_dir):
    """Every broad except in the app now logs. Spot check: a draft discard
    whose file removal fails used to vanish without a trace."""
    import ast, pathlib
    silent = []
    for path in pathlib.Path(__file__).resolve().parents[1].joinpath("app").rglob("*.py"):
        for h in ast.walk(ast.parse(path.read_text())):
            if isinstance(h, ast.ExceptHandler) and (h.type is None or ast.unparse(h.type) in ("Exception", "OSError")):
                if not any(k in ast.unparse(h) for k in ("log.", "scan_log.", "_log.")):
                    silent.append(f"{path.name}:{h.lineno}")
    assert silent == [], f"exceptions caught without logging: {silent}"
