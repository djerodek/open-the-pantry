"""Security findings from the Opus 5 review (group B), each reproduced against
the previous code first. No login is added; see README "Security notes".

app.* is imported inside tests only; see the note in test_backup.py.
"""
from unittest.mock import MagicMock, patch

import pytest


def _email(**over):
    base = {"enabled": False, "imap_host": "mail.me.example", "imap_port": 993, "imap_use_ssl": True,
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


@pytest.mark.parametrize("field", ["imap_host", "smtp_host", "username"])
def test_changing_server_or_username_drops_the_saved_password(client, key, field):
    """Reproduced: change only the hosts, press Send test email, and the
    saved password was sent to attacker.example.net."""
    client.put("/api/email-settings", json=_email(password="s3cret"))
    r = client.put("/api/email-settings", json=_email(**{field: "attacker.example.net"}))
    assert r.json()["password_set"] is False
    client.delete("/api/email-settings/password")


def test_saving_same_server_keeps_the_password(client, key):
    client.put("/api/email-settings", json=_email(password="s3cret"))
    r = client.put("/api/email-settings", json=_email(daily_scan_hour=5))
    assert r.json()["password_set"] is True
    client.delete("/api/email-settings/password")


def test_changing_server_with_new_password_keeps_it(client, key):
    client.put("/api/email-settings", json=_email(password="s3cret"))
    r = client.put("/api/email-settings", json=_email(smtp_host="smtp.new.example", password="n3w"))
    assert r.json()["password_set"] is True
    client.delete("/api/email-settings/password")


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.100.100.100", "192.0.0.8", "198.18.0.1", "::ffff:10.0.0.1"])
def test_ssrf_guard_blocks_non_public_ranges(ip):
    from app.ingestion import url_ingest
    fam = 10 if ":" in ip else 2
    with patch.object(url_ingest.socket, "getaddrinfo", return_value=[(fam, 1, 6, "", (ip, 0))]):
        with pytest.raises(url_ingest.UrlValidationError):
            url_ingest.validate_public_url("http://looks-public.example/recipe")


def _streaming_response(chunks, content_length=None):
    """A real requests.Response over an in-memory body, as stream=True gives."""
    import io
    import requests
    from requests.structures import CaseInsensitiveDict
    resp = requests.Response()
    resp.status_code = 200
    resp.headers = CaseInsensitiveDict({"Content-Type": "text/html; charset=utf-8"})
    if content_length:
        resp.headers["Content-Length"] = str(content_length)
    resp.raw = io.BytesIO(b"".join(chunks))
    resp.encoding = "utf-8"
    return resp


def test_downloads_stop_at_the_size_limit():
    """safe_get loaded whole responses into memory; the limit is enforced
    while reading, even when the server doesn't say how big it is."""
    from app.ingestion import url_ingest
    chunks = [b"x" * 65536] * 40            # 2.5 MB, no Content-Length
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest.requests, "get", return_value=_streaming_response(chunks)):
        with pytest.raises(url_ingest.FetchTooLargeError):
            url_ingest.safe_get("https://big.example/", max_bytes=1024 * 1024)


def test_downloads_stop_at_the_time_limit(monkeypatch):
    from app.ingestion import url_ingest
    monkeypatch.setattr(url_ingest, "TOTAL_FETCH_SECONDS", 0)
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest.requests, "get", return_value=_streaming_response([b"a", b"b"])):
        with pytest.raises(url_ingest.UrlValidationError, match="longer than"):
            url_ingest.safe_get("https://slow.example/")


def test_normal_page_still_downloads():
    from app.ingestion import url_ingest
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest.requests, "get", return_value=_streaming_response([b"<html>ok</html>"])):
        assert url_ingest.safe_get("https://fine.example/").text == "<html>ok</html>"


def test_tall_scanned_pages_render_within_the_pixel_budget():
    """An 8.5 x 200 inch image-only page rendered at 300 dpi was 153 MP."""
    from app.ingestion.pdf_ingest import _ocr_resolution, MAX_OCR_PIXELS
    for w, h in [(612, 14400), (393, 14400), (14400, 14400)]:
        dpi = _ocr_resolution(w, h)
        assert (w / 72 * dpi) * (h / 72 * dpi) <= MAX_OCR_PIXELS * 1.02
    assert _ocr_resolution(612, 792) == 300          # an ordinary page is unchanged


@pytest.mark.parametrize("host", ["attacker.example.com", "evil.com:8090"])
def test_foreign_host_names_are_refused(client, host):
    """DNS rebinding arrives with the attacker's hostname in Host."""
    assert client.get("/api/recipes", headers={"host": host}).status_code == 400


@pytest.mark.parametrize("host", ["192.168.60.50:8090", "localhost:8090", "hnmty:8090", "nas.local", "[::1]:8090"])
def test_lan_host_names_are_allowed(client, host):
    assert client.get("/api/recipes", headers={"host": host}).status_code == 200


def test_listed_host_names_are_allowed(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "ALLOWED_HOSTS", {"pantry.example.com"})
    assert client.get("/api/recipes", headers={"host": "pantry.example.com"}).status_code == 200


def test_writes_need_the_custom_header(client):
    """A cross-site page could POST /email-settings/scan (no body, no
    preflight). Writes now need a header only the app's own pages send."""
    from fastapi.testclient import TestClient
    import app.main as m
    bare = TestClient(m.app)   # no default X-Requested-With
    assert bare.post("/api/email-settings/scan").status_code == 403
    assert bare.delete("/api/recipes/999999").status_code == 403
    assert bare.get("/api/recipes").status_code == 200      # reads unaffected


def test_sender_allowlist(client, key):
    import app.main as m
    assert m._sender_allowed("Me <me@home.example>", ["me@home.example"])
    assert m._sender_allowed("sis@family.example", ["@family.example"])
    assert not m._sender_allowed("stranger@spam.example", ["me@home.example", "@family.example"])
    assert m._sender_allowed("anyone@x.example", [])       # empty list: anyone, as before


def test_scan_ignores_senders_not_on_the_list(client, key):
    from email.message import EmailMessage
    client.put("/api/email-settings", json=_email(enabled=True, password="pw", allowed_senders="me@me.example",
                                                  cooldown_minutes=0))
    msg = EmailMessage()
    msg["Subject"] = "[RECIPE] Stranger Stew"
    msg["From"] = "stranger@spam.example"
    msg.set_content("Stew\nIngredients\n1 onion\nInstructions\n1. Cook")
    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.main.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.main.email_client.fetch_full_message", return_value=msg), \
         patch("app.main.email_client.mark_seen") as seen, \
         patch("app.main._run_heavy") as processed:
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
