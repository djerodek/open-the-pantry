"""Fixes from reading a real app.log (Oct 3), and Settings -> Logs.

app.* is imported inside tests only; see the note in test_backup.py.
"""
import email
import io
import logging
from email.message import EmailMessage
from unittest.mock import patch

import pytest



@pytest.fixture(autouse=True)
def _isolated_data_dir(data_dir):
    pass


# ---------------------------------------------------------------------------
# Apple Mail's encoded attachment names
# ---------------------------------------------------------------------------

APPLE_PDF = (b"Content-Type: multipart/mixed; boundary=X\r\nSubject: [RECIPE]\r\n\r\n"
             b"--X\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; "
             b"filename=\"=?utf-8?B?U2NyZWVuc2hvdCAyMDI2LTA5LTI1IGF0IDEuNDQuMznigK9QTS5wZGY=?=\"\r\n"
             b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--X--\r\n")


def test_attachment_names_are_decoded():
    from app.ingestion.email_processing import extract_email_parts
    parts = extract_email_parts(email.message_from_bytes(APPLE_PDF))
    assert parts["pdf_name"] == "Screenshot 2026-09-25 at 1.44.39 PM.pdf"


# ---------------------------------------------------------------------------
# Unreadable photos are refused instead of saved as gibberish
# ---------------------------------------------------------------------------

def _photo_email():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (120, 80), "white").save(buf, format="PNG")
    m = EmailMessage()
    m["Subject"] = "[RECIPE]"
    m.set_content("")
    m.add_attachment(buf.getvalue(), maintype="image", subtype="png", filename="card.png")
    return m


def _ocr(text, conf):
    from app.ingestion.image_ingest import ImageIngestResult
    return ImageIngestResult(raw_text=text, ocr_confidence=conf)


GARBLED = "merce rene neste\nIngredients\n2 cups flour\nInstructions\n1. Mix it"


def test_low_confidence_photo_is_refused(tmp_path):
    from app.ingestion import email_processing
    with patch.object(email_processing, "ingest_image", return_value=_ocr(GARBLED, 31.0)):
        result = email_processing.process_tagged_email(_photo_email(), str(tmp_path))
    assert result["success"] is False
    assert "couldn't be read reliably (text recognition confidence 31%)" in result["error"]
    assert list(tmp_path.iterdir()) == []      # the photo isn't left behind


def test_readable_photo_is_still_saved(tmp_path):
    from app.ingestion import email_processing
    with patch.object(email_processing, "ingest_image", return_value=_ocr(GARBLED, 78.0)):
        result = email_processing.process_tagged_email(_photo_email(), str(tmp_path))
    assert result["success"] is True


def test_refusal_reason_is_translated():
    from app import i18n
    msg = ('No recipe found. Tried: card.png: the photo couldn\'t be read reliably (text recognition '
           'confidence 31%); a sharper, straight-on photo or the typed text works better; body: empty.')
    fr = i18n.translate(msg, "fr")
    assert "fiabilité de la reconnaissance 31 %" in fr and "reliably" not in fr


# ---------------------------------------------------------------------------
# A site's refusal is one clear line, not a traceback
# ---------------------------------------------------------------------------

def test_bot_check_refusal_is_explained(caplog):
    import requests
    from requests.structures import CaseInsensitiveDict
    from app.ingestion import url_ingest

    resp = requests.Response()
    resp.status_code = 403
    resp.headers = CaseInsensitiveDict({"Content-Type": "text/html", "Server": "cloudflare"})
    resp.raw = io.BytesIO(b"<html><title>Just a moment...</title></html>")
    resp.encoding = "utf-8"
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest.requests, "get", return_value=resp):
        with pytest.raises(url_ingest.SiteRefusedError, match=r"heygrillhey\.com refused the request \(HTTP 403\)"):
            url_ingest.safe_get("https://heygrillhey.com/homemade-smoked-bacon/")


def test_url_refusal_logs_without_traceback(client, caplog):
    from app.ingestion import url_ingest
    err = url_ingest.SiteRefusedError("example.com refused the request (HTTP 403).")
    caplog.set_level(logging.WARNING, logger="otp")
    logging.getLogger("otp").propagate = True
    try:
        with patch("app.main.ingest_url", side_effect=err):
            r = client.post("/api/ingest/url", json={"url": "https://example.com/r"})
    finally:
        logging.getLogger("otp").propagate = False
    assert r.status_code == 422 and "refused the request" in r.json()["detail"]
    records = [rec for rec in caplog.records if "Add from URL failed" in rec.getMessage()]
    assert records and not records[0].exc_info


# ---------------------------------------------------------------------------
# Settings -> Logs
# ---------------------------------------------------------------------------

def test_logs_view_filter_download_and_clear(client):
    log = logging.getLogger("otp.app")
    log.info("plain entry for the log test")
    try:
        raise ValueError("boom for the log test")
    except ValueError:
        log.warning("problem entry for the log test", exc_info=True)
    for h in logging.getLogger("otp").handlers:
        h.flush()

    every = client.get("/api/logs?lines=50").json()
    text = "\n".join(every["records"])
    assert "plain entry for the log test" in text and "problem entry for the log test" in text

    problems = client.get("/api/logs?lines=50&problems=true").json()["records"]
    assert not any("plain entry for the log test" in r for r in problems)
    tb = next(r for r in problems if "problem entry for the log test" in r)
    assert "ValueError: boom for the log test" in tb      # the traceback stays with its entry

    dl = client.get("/api/logs/download")
    assert dl.status_code == 200 and b"problem entry for the log test" in dl.content
    assert dl.headers["content-disposition"].startswith('attachment; filename="open-the-pantry-log-')

    assert client.delete("/api/logs").json() == {"ok": True}
    after = client.get("/api/logs?lines=50").json()
    assert not any("for the log test" in r for r in after["records"])
    assert any("Log cleared from Settings" in r for r in after["records"])
    log.info("logging still works after clearing")
    for h in logging.getLogger("otp").handlers:
        h.flush()
    assert any("still works after clearing" in r for r in client.get("/api/logs").json()["records"])


# ---------------------------------------------------------------------------
# Half a recipe from a URL counts as a failed import, with PDF advice
# ---------------------------------------------------------------------------

def _url_result(ingredients, steps):
    from app.ingestion.url_ingest import UrlIngestResult
    return UrlIngestResult(title="Poulet au vinaigre", ingredients=ingredients, steps=steps,
                           raw_text="x", method="heuristic")


def test_single_url_reports_an_incomplete_recipe_but_keeps_the_draft(client):
    with patch("app.main.ingest_url", return_value=_url_result(["1 chicken", "vinegar"], [])):
        r = client.post("/api/ingest/url", json={"url": "https://example.com/poulet"})
        fr = client.post("/api/ingest/url", json={"url": "https://example.com/poulet"}, headers={"X-App-Lang": "fr"})
    d = r.json()
    assert r.status_code == 200 and d["incomplete"]["missing"] == ["steps"]
    assert d["incomplete"]["message"] == ("This page didn't give a complete recipe (no steps found). "
                                          "Save the page as a PDF and add that instead.")
    assert len(d["ingredients"]) == 2            # still there for "Continue anyway"
    assert fr.json()["incomplete"]["message"].startswith("Cette page ne donne pas une recette complète (aucune étape")


def test_complete_recipe_is_not_flagged(client):
    with patch("app.main.ingest_url", return_value=_url_result(["1 chicken"], ["Roast it."])):
        assert client.post("/api/ingest/url", json={"url": "https://example.com/ok"}).json()["incomplete"] is None


def test_batch_fails_an_incomplete_recipe(client):
    with patch("app.main.ingest_url", return_value=_url_result([], ["Mix."])):
        r = client.post("/api/ingest/url/batch", json={"urls": ["https://example.com/half"]}).json()
    assert r["succeeded"] == [] and "no ingredients found" in r["failed"][0]["error"]


def test_email_link_with_half_a_recipe_fails_with_pdf_advice(tmp_path):
    from app.ingestion import email_processing
    m = EmailMessage()
    m["Subject"] = "[RECIPE]"
    m.set_content("https://example.com/poulet")
    with patch.object(email_processing, "ingest_url", return_value=_url_result(["1 chicken"], [])):
        result = email_processing.process_tagged_email(m, str(tmp_path))
    assert result["success"] is False
    assert "link https://example.com/poulet: This page didn't give a complete recipe (no steps found)." in result["error"]
    assert "Save the page as a PDF and add that instead" not in result["error"]
    assert result["error"].endswith("save the page as a PDF and email that as an attachment, "
                                    "with the same word in the subject.")


def test_notes_are_capped(client):
    rid = client.post("/api/recipes", json={"title": "Notes Cap", "source_type": "manual", "steps": ["x"]}).json()["id"]
    assert client.patch(f"/api/recipes/{rid}/notes", json={"notes": "a" * 50_000}).status_code == 200
    assert client.patch(f"/api/recipes/{rid}/notes", json={"notes": "a" * 50_001}).status_code == 422
