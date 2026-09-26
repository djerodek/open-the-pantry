"""Follow-up review findings on the round-2 fixes (0015-0018), each
reproduced against the pre-fix code first where that's practical.

app.* is imported inside tests only; see the note in test_backup.py.
"""
import io

import pytest


# ---------------------------------------------------------------------------
# 1. OCR pixel budget: an extreme aspect ratio blows past MAX_OCR_PIXELS
#    during the upscale-to-1500px step, even though the initial render (or
#    the raw upload) stayed inside it.
# ---------------------------------------------------------------------------

def test_preprocess_for_ocr_caps_extreme_upscale():
    """A 12 x 60000 px render (a 3 x 14400 pt page rendered within budget)
    used to upscale to 1500 px wide unconditionally -- 1500 x 7,500,000 =
    11.2 gigapixels."""
    from PIL import Image
    from app.ingestion import pdf_ingest

    narrow_tall = Image.new("L", (12, 60000), color=255)
    result = pdf_ingest._preprocess_for_ocr(narrow_tall)
    assert result.width * result.height <= pdf_ingest.MAX_OCR_PIXELS * 1.02


def test_preprocess_for_ocr_downscales_an_already_oversized_render():
    """Safety net for an image that reaches preprocessing over budget: the
    old code only ever upscaled narrow images, so an already-too-big one
    passed through untouched."""
    from PIL import Image
    from app.ingestion import pdf_ingest

    oversized = Image.new("L", (6200, 6200), color=255)
    result = pdf_ingest._preprocess_for_ocr(oversized)
    assert result.width * result.height <= pdf_ingest.MAX_OCR_PIXELS * 1.02


def test_huge_page_renders_within_budget():
    """The first attempt only shrank the image after rendering: a 200 x 200
    inch page still rendered at 72 dpi (the old floor), 14400 x 14400 =
    207 MP, 1.7 GB peak, before the cap could apply."""
    from app.ingestion.pdf_ingest import _ocr_resolution, MAX_OCR_PIXELS
    dpi = _ocr_resolution(14400, 14400)
    assert (200 * dpi) ** 2 <= MAX_OCR_PIXELS


def test_image_preprocess_caps_extreme_upscale(tmp_path):
    """A 20 x 40000 px upload asked for 4.5 gigapixels after the
    upscale-to-1500-wide step."""
    from PIL import Image
    from app.ingestion import image_ingest

    p = tmp_path / "tall.png"
    Image.new("RGB", (20, 40000), color=(255, 255, 255)).save(p)
    processed, _ = image_ingest._preprocess(str(p))
    assert processed.size[0] * processed.size[1] <= image_ingest.MAX_OCR_PIXELS * 1.02


# ---------------------------------------------------------------------------
# 2. Download time limit: iter_content(64 KB) blocks until a full chunk
#    arrives, so a server trickling data byte-by-byte never triggers the
#    per-chunk deadline check.
# ---------------------------------------------------------------------------

class _TricklingRaw:
    """Mimics a server that sends one byte at a time: read1 returns
    whatever is available right now rather than waiting to fill amt, and
    each read costs real time -- the delay iter_content's full-chunk wait
    couldn't be interrupted out of."""

    def __init__(self, data: bytes, delay: float = 0.0):
        self._buf = io.BytesIO(data)
        self._delay = delay

    def read1(self, amt=None, decode_content=None):
        import time as _time
        if self._delay:
            _time.sleep(self._delay)
        return self._buf.read(1)

    def close(self):
        pass


def _trickling_response(data: bytes, delay: float = 0.0):
    import requests
    from requests.structures import CaseInsensitiveDict

    resp = requests.Response()
    resp.status_code = 200
    resp.headers = CaseInsensitiveDict({"Content-Type": "text/html; charset=utf-8"})
    resp.raw = _TricklingRaw(data, delay=delay)
    resp.encoding = "utf-8"
    return resp


def test_time_limit_stops_a_byte_at_a_time_download(monkeypatch):
    from unittest.mock import patch
    from app.ingestion import url_ingest

    monkeypatch.setattr(url_ingest, "TOTAL_FETCH_SECONDS", 0.2)
    data = b"x" * 10_000
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest.requests, "get", return_value=_trickling_response(data, delay=0.05)):
        with pytest.raises(url_ingest.UrlValidationError, match="longer than"):
            url_ingest.safe_get("https://slow.example/")


def test_normal_page_still_downloads_via_read1():
    """The switch away from iter_content must not break the ordinary path."""
    from unittest.mock import patch
    from app.ingestion import url_ingest

    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest.requests, "get", return_value=_trickling_response(b"<html>ok</html>")):
        assert url_ingest.safe_get("https://fine.example/").text == "<html>ok</html>"


# ---------------------------------------------------------------------------
# 3. Export/backup no longer matched what the screen showed: the template
#    rebuilt the ingredient line from the parsed fields instead of using
#    raw_line, the same regression in both the recipe export and the PDF
#    backup bundle (same template).
# ---------------------------------------------------------------------------

def test_export_renders_the_line_as_typed(client):
    r = client.post("/api/recipes", json={
        "title": "Export Line Check",
        "source_type": "manual",
        "ingredients": [{"raw_line": "3 Tablespoons maple syrup"}],
        "steps": ["Mix it."],
    })
    assert r.status_code == 200
    recipe_id = r.json()["id"]

    html = client.get(f"/api/recipes/{recipe_id}/export.html").text
    assert "3 Tablespoons maple syrup" in html
    assert "3 tbsp maple syrup" not in html


# ---------------------------------------------------------------------------
# 6. Email settings: a misleading error, and a bare-domain allowlist entry
#    that matched nothing.
# ---------------------------------------------------------------------------

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
    from app.main import _parse_allowed_senders, _sender_allowed

    allowed = _parse_allowed_senders("example.com, someone@other.example")
    assert _sender_allowed("Person <cook@example.com>", allowed)
    assert _sender_allowed("someone@other.example", allowed)
    assert not _sender_allowed("cook@notexample.com", allowed)


# ---------------------------------------------------------------------------
# 8. Leftovers.
# ---------------------------------------------------------------------------

def test_recipe_list_loads_tags_in_one_query(client):
    """list_recipes read each recipe's tags lazily: one extra SELECT per
    recipe in the list."""
    from sqlalchemy import event
    from app.database import engine

    for i in range(6):
        client.post("/api/recipes", json={"title": f"Tagged {i}", "source_type": "manual",
                                          "tags": [{"name": "Dinner", "category": "meal_type"}], "steps": ["x"]})
    statements = []
    listener = lambda conn, cursor, stmt, *a: statements.append(stmt)
    event.listen(engine, "before_cursor_execute", listener)
    try:
        r = client.get("/api/recipes?sort=created&direction=desc")
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert r.status_code == 200 and len(r.json()) >= 6
    tag_selects = [s for s in statements if "FROM tags" in s or "JOIN tags" in s]
    assert len(tag_selects) <= 1, f"{len(tag_selects)} tag queries for one list"


def test_pdf_bundle_skips_a_traversal_image_path(client, data_dir, tmp_path):
    """build_pdf_bundle joined image_path with os.path.join; a hand-edited
    or restored database could point it outside uploads/."""
    import zipfile
    from app.database import SessionLocal
    from app import models, backup

    secret = tmp_path / "secret.png"
    from PIL import Image
    Image.new("RGB", (300, 300), "red").save(secret)
    db = SessionLocal()
    r = models.Recipe(title="Traversal Bundle", source_type="manual", image_path=str(secret))
    db.add(r); db.commit()
    try:
        out = tmp_path / "bundle.zip"
        backup.build_pdf_bundle([r], str(out))
        with zipfile.ZipFile(out) as z:
            pdf = z.read(next(n for n in z.namelist() if n.endswith(".pdf")))
        assert b"/Subtype /Image" not in pdf and b"/Subtype/Image" not in pdf
    finally:
        db.delete(r); db.commit(); db.close()


def test_rotated_photo_keeps_jpeg_quality(tmp_path):
    from PIL import Image
    from app.ingestion.image_ingest import _rotate_stored_file
    from app.file_validation import JPEG_QUALITY

    p = tmp_path / "photo.jpg"
    Image.effect_noise((600, 400), 60).convert("RGB").save(p, format="JPEG", quality=JPEG_QUALITY)
    before = p.stat().st_size
    _rotate_stored_file(str(p), 90)
    # At the old default (75) the re-save of a noisy image shrank by well
    # over a third; at the same quality it stays close to the original size.
    assert p.stat().st_size > before * 0.8
