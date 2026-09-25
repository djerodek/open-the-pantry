import os
from unittest.mock import patch, MagicMock

from .conftest import TINY_PNG


def test_healthz_no_auth_required(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_oversized_upload_rejected(client):
    big_payload = b"%PDF-1.4\n" + (b"A" * (21 * 1024 * 1024))
    r = client.post("/api/ingest/pdf", files={"file": ("big.pdf", big_payload, "application/pdf")})
    assert r.status_code == 413


def test_fake_pdf_bad_magic_bytes_rejected(client):
    r = client.post("/api/ingest/pdf", files={"file": ("fake.pdf", b"not a real pdf", "application/pdf")})
    assert r.status_code == 400


def test_fake_image_bad_magic_bytes_rejected(client):
    r = client.post("/api/ingest/image", files={"file": ("fake.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 400


def test_image_extension_forced_from_content_not_client_filename(client, data_dir):
    """A real PNG named with a .html extension must still be saved with a
    .png extension -- using the client-supplied extension would let it be
    served back by StaticFiles with a browser-executable content-type."""
    r = client.post("/api/upload-image", files={"file": ("evil.html", TINY_PNG, "image/png")})
    assert r.status_code == 200
    stored = r.json()["stored_file"]
    assert stored.endswith(".png")
    assert not stored.endswith(".html")


def test_image_reencode_strips_polyglot_payload(client):
    """A file with valid PNG magic bytes but an appended script payload
    should have that payload stripped by the post-validation re-encode."""
    polyglot = TINY_PNG + b"<script>alert(1)</script>" * 20
    r = client.post("/api/upload-image", files={"file": ("evil.png", polyglot, "image/png")})
    assert r.status_code == 200
    stored = r.json()["stored_file"]
    from app.database import TMP_DIR
    with open(os.path.join(TMP_DIR, stored), "rb") as f:
        content = f.read()
    assert b"<script>" not in content
    # cleanup
    client.delete(f"/api/ingest/draft/{stored}")


def test_reencode_handles_trailing_garbage_without_rejecting(client):
    """Regression test: a naive open()->save() re-encode (no getdata()/
    putdata() round trip) reliably raises 'broken data stream' on a file
    with bytes appended after a valid PNG stream, which would incorrectly
    reject an otherwise-fine upload. _reencode_image uses getdata()/
    putdata() specifically because it handles this correctly -- confirmed
    directly against PIL before adopting it. This locks that in."""
    polyglot = TINY_PNG + b"some trailing non-image bytes" * 5
    r = client.post("/api/upload-image", files={"file": ("trailer.png", polyglot, "image/png")})
    assert r.status_code == 200, "a valid image with trailing bytes should not be rejected"
    stored = r.json()["stored_file"]
    client.delete(f"/api/ingest/draft/{stored}")


def test_reencode_preserves_exif_orientation(client, data_dir):
    """EXIF orientation must be baked into the pixel data (not just
    dropped) before all other metadata is stripped -- otherwise a photo
    taken in portrait could come out sideways once EXIF is gone."""
    import io
    import piexif
    from PIL import Image

    img = Image.new("RGB", (4, 2), color="white")  # stored wide
    img.putpixel((0, 0), (255, 0, 0))
    exif_bytes = piexif.dump({"0th": {274: 6}})  # orientation 6 = rotate 90 CW to display
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif_bytes)

    r = client.post("/api/upload-image", files={"file": ("oriented.jpg", buf.getvalue(), "image/jpeg")})
    assert r.status_code == 200
    stored = r.json()["stored_file"]
    from app.database import TMP_DIR
    result = Image.open(os.path.join(TMP_DIR, stored))
    assert result.size == (2, 4), "orientation should be baked into pixel dimensions (swapped from stored 4x2)"
    assert dict(result.getexif()) == {}, "EXIF should be fully stripped after baking in orientation"
    client.delete(f"/api/ingest/draft/{stored}")


def test_export_html_escapes_injected_markup(client):
    payload = {
        "title": "<script>alert(1)</script>Evil Title",
        "source_type": "manual",
        "ingredients": [{"raw_line": "1 cup <img src=x onerror=alert(2)>", "name": "ingredient"}],
        "steps": ["<script>alert(3)</script>"],
        "tags": [],
    }
    r = client.post("/api/recipes", json=payload)
    recipe_id = r.json()["id"]
    try:
        r = client.get(f"/api/recipes/{recipe_id}/export.html")
        assert r.status_code == 200
        assert "<script>alert(1)</script>" not in r.text
        assert "&lt;script&gt;" in r.text
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_draft_file_promoted_on_save_and_removed_on_delete(client, data_dir):
    from app.database import TMP_DIR, UPLOADS_DIR

    r = client.post("/api/upload-image", files={"file": ("card.png", TINY_PNG, "image/png")})
    temp_name = r.json()["stored_file"]
    assert os.path.isfile(os.path.join(TMP_DIR, temp_name))

    payload = {
        "title": "Draft Promotion Test",
        "source_type": "manual", "image_path": temp_name,
        "ingredients": [], "steps": [], "tags": [],
    }
    r = client.post("/api/recipes", json=payload)
    recipe_id = r.json()["id"]

    assert not os.path.isfile(os.path.join(TMP_DIR, temp_name))
    assert os.path.isfile(os.path.join(UPLOADS_DIR, temp_name))

    client.delete(f"/api/recipes/{recipe_id}")
    assert not os.path.isfile(os.path.join(UPLOADS_DIR, temp_name)), "total deletion: file must be removed too"


def test_discard_draft_removes_temp_file(client, data_dir):
    from app.database import TMP_DIR

    r = client.post("/api/upload-image", files={"file": ("card2.png", TINY_PNG, "image/png")})
    temp_name = r.json()["stored_file"]
    assert os.path.isfile(os.path.join(TMP_DIR, temp_name))

    client.delete(f"/api/ingest/draft/{temp_name}")
    assert not os.path.isfile(os.path.join(TMP_DIR, temp_name))


def test_ssrf_guard_blocks_private_and_metadata_addresses(client):
    for bad_url in [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8080/api/recipes",
        "http://10.0.0.5/internal",
    ]:
        r = client.post("/api/ingest/url", json={"url": bad_url})
        assert r.status_code == 422


def test_ssrf_guard_blocks_redirect_to_private_address():
    """A public-looking URL that redirects to a private/metadata address
    must be blocked at the redirect hop too, not just the initial URL."""
    import socket
    from app.ingestion.url_ingest import safe_get, UrlValidationError

    real_getaddrinfo = socket.getaddrinfo

    def fake_get(url, headers=None, timeout=None, allow_redirects=None):
        resp = MagicMock()
        if "evil-redirect.example.com" in url:
            resp.is_redirect = True
            resp.is_permanent_redirect = False
            resp.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}
            return resp
        resp.is_redirect = False
        resp.is_permanent_redirect = False
        resp.raise_for_status = lambda: None
        return resp

    def fake_dns(host, *args, **kwargs):
        if host == "evil-redirect.example.com":
            return [(2, 1, 6, "", ("93.184.216.34", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    with patch("app.ingestion.url_ingest.requests.get", side_effect=fake_get), \
         patch("app.ingestion.url_ingest.socket.getaddrinfo", side_effect=fake_dns):
        try:
            safe_get("http://evil-redirect.example.com/recipe")
            assert False, "redirect to a private/metadata address should have been blocked"
        except UrlValidationError:
            pass


# ---------------------------------------------------------------------------
# Path traversal via image_path (regression tests for a real vulnerability)
# ---------------------------------------------------------------------------

def test_image_path_rejects_absolute_and_traversal(client, data_dir):
    """Regression: image_path was a bare string joined to a directory with
    os.path.join(), which DISCARDS the base dir when given an absolute
    path. That made it an arbitrary-file-read primitive (export embeds
    the file as a data URI) and an arbitrary-file-DELETE primitive
    (recipe deletion unlinks the resolved path) -- including the app's
    own live SQLite database, in two unauthenticated requests."""
    import os
    victim = os.path.join(str(data_dir), "victim-do-not-delete.txt")
    with open(victim, "w") as f:
        f.write("sensitive")

    attacks = [
        victim,                    # absolute path
        "../victim-do-not-delete.txt",   # traversal
        "../../etc/passwd",
        "recipes.db",              # bare name, but not an app-generated one
        "sub/dir/img-" + "a" * 32 + ".jpg",   # directory component
    ]
    for attack in attacks:
        r = client.post("/api/recipes", json={
            "title": "traversal probe", "source_type": "manual",
            "image_path": attack, "ingredients": [], "steps": [], "tags": [],
        })
        assert r.status_code == 422, f"{attack!r} should be rejected at the schema layer"

    assert os.path.isfile(victim), "no attack should have touched the victim file"
    os.remove(victim)


def test_image_patch_rejects_traversal(client, data_dir):
    """Same protection on the PATCH /image endpoint, which is a second
    way into the same file-handling code."""
    import os
    victim = os.path.join(str(data_dir), "victim2.txt")
    with open(victim, "w") as f:
        f.write("sensitive")

    r = client.post("/api/recipes", json={
        "title": "patch probe", "source_type": "manual",
        "ingredients": [], "steps": [], "tags": [],
    })
    recipe_id = r.json()["id"]
    try:
        r = client.patch(f"/api/recipes/{recipe_id}/image", json={"image_path": victim})
        assert r.status_code == 422
        assert os.path.isfile(victim)
    finally:
        client.delete(f"/api/recipes/{recipe_id}")
        if os.path.isfile(victim):
            os.remove(victim)


def test_safe_stored_filename_accepts_real_generated_names():
    """The validator must not be so strict it rejects names the app
    actually produces -- one per generating prefix."""
    import uuid
    from app.file_validation import is_safe_stored_filename

    for prefix, ext in [("img", "png"), ("manual", "jpg"), ("pdf", "pdf"),
                        ("url-img", "jpg"), ("pdf-img", "jpg"), ("email", "png")]:
        name = f"{prefix}-{uuid.uuid4().hex}.{ext}"
        assert is_safe_stored_filename(name), f"{name} should be accepted"


def test_safe_join_confines_to_base_dir(tmp_path):
    import uuid
    from app.file_validation import safe_join

    good = f"img-{uuid.uuid4().hex}.jpg"
    assert safe_join(str(tmp_path), good) is not None
    for bad in ["/etc/passwd", "../escape.jpg", "nope.txt"]:
        assert safe_join(str(tmp_path), bad) is None


def test_riff_container_that_is_not_webp_is_rejected(tmp_path):
    """RIFF is a container -- AVI and WAV share WebP's leading magic. The
    WEBP four-CC at offset 8 distinguishes them. Without the check an AVI
    was saved as .webp and failed later with a misleading error."""
    from app.file_validation import validate_and_save_image_bytes

    fake_avi = b"RIFF" + b"\x00\x00\x00\x00" + b"AVI " + b"\x00" * 64
    assert validate_and_save_image_bytes(fake_avi, str(tmp_path), "img-" + "a" * 32) is None
    assert not list(tmp_path.iterdir()), "nothing should be left on disk"


def test_recipe_scrapers_never_fetches_on_its_own():
    """Regression: URL ingestion called recipe_scrapers.scrape_me(url),
    which fetches with its own HTTP client and bypasses the SSRF-guarded
    safe_get (so a public URL redirecting to a LAN address was followed).
    Any request that doesn't go through safe_get now fails the test."""
    from unittest.mock import patch, MagicMock
    from app.ingestion import url_ingest

    html = """<html><head><script type="application/ld+json">
      {"@context":"https://schema.org","@type":"Recipe","name":"Guarded Stew",
       "recipeIngredient":["1 onion"],"recipeInstructions":[{"@type":"HowToStep","text":"Cook it"}]}
    </script></head><body></body></html>"""
    resp = MagicMock(text=html)

    # Recorded rather than raised: the old code wrapped scrape_me() in
    # `except Exception` and fell back, so an exception here would be
    # swallowed and the test would pass against the very bug it's for.
    # recipe_scrapers imports urlopen into its own namespace, so that name
    # is patched where it's used, not in urllib.
    unguarded_calls = []

    def unguarded(*a, **k):
        unguarded_calls.append(a)
        raise OSError("blocked in test")

    import recipe_scrapers
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest, "safe_get", return_value=resp) as guarded, \
         patch("requests.sessions.Session.request", side_effect=unguarded), \
         patch.object(recipe_scrapers, "urlopen", side_effect=unguarded, create=True), \
         patch.object(recipe_scrapers, "scrape_me", side_effect=unguarded):
        result = url_ingest.ingest_url("https://recipes.example.com/stew")
    assert unguarded_calls == [], "recipe_scrapers fetched a page itself"
    assert result.title == "Guarded Stew"
    guarded.assert_called_once()


def test_reencode_keeps_palette_colours(tmp_path):
    """Regression: re-encoding built a fresh "P" image without copying the
    palette, so palette PNGs/GIFs came out in the wrong colours (red
    pixels turned black)."""
    from PIL import Image
    from app.file_validation import reencode_image

    for fmt, name in (("PNG", "p.png"), ("GIF", "p.gif")):
        img = Image.new("P", (8, 8))
        img.putpalette([255, 0, 0] + [0, 0, 255] * 255)
        path = tmp_path / name
        img.save(path, format=fmt)
        reencode_image(str(path))
        with Image.open(path) as out:
            assert out.convert("RGB").getpixel((0, 0)) == (255, 0, 0), fmt


def test_streaming_upload_rejects_riff_that_isnt_webp(client):
    """The streaming upload path accepted any RIFF file as .webp; only the
    in-memory path checked for the WEBP four-CC. Both use one check now."""
    avi = b"RIFF" + (1000).to_bytes(4, "little") + b"AVI LIST" + b"\x00" * 200
    r = client.post("/api/upload-image", files={"file": ("x.webp", avi, "image/webp")})
    assert r.status_code == 400
    # Rejected by the magic-byte check, not later by Pillow. The old code
    # also returned 400 here, but only after saving it as .webp and failing
    # to decode it -- "could not be decoded" rather than this.
    assert r.json()["detail"] == "File does not look like a valid image."


def test_batch_url_ingest_is_capped(client):
    r = client.post("/api/ingest/url/batch", json={"urls": [f"https://example.com/{i}" for i in range(51)]})
    assert r.status_code == 400
    assert "max 50" in r.json()["detail"]


def test_sideways_photo_is_turned_upright_for_ocr_and_storage(tmp_path):
    """Regression: a recipe photographed on its side went to OCR at 90
    degrees and came back as gibberish (the reported "merce rene neste"),
    and the stored photo displayed sideways."""
    import pytest
    from PIL import Image, ImageDraw, ImageFont
    from app.ingestion.image_ingest import ingest_image

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 30)
    except OSError:
        pytest.skip("DejaVu font not available to draw the test card")
    lines = ["Spinach and Mushroom Quesadillas", "Ingredients", "8 oz. mushrooms", "1 Tbsp cooking oil",
             "1/4 tsp garlic powder", "1/2 lb. frozen chopped spinach", "8 oz. mozzarella, shredded",
             "1/4 cup sour cream", "5 7-inch flour tortillas", "Instructions",
             "1. Slice the mushrooms and add them to a skillet with the oil.",
             "2. Thaw the spinach and squeeze out the water.",
             "3. Spread onto tortillas and fold to close."]
    card = Image.new("RGB", (1400, 60 + 44 * len(lines)), "white")
    d = ImageDraw.Draw(card)
    for i, l in enumerate(lines):
        d.text((40, 30 + 44 * i), l, fill="black", font=font)
    path = tmp_path / "sideways.png"
    card.rotate(90, expand=True).save(path)          # page on its side: taller than wide

    result = ingest_image(str(path))
    assert "Quesadillas" in result.raw_text and "tortillas" in result.raw_text
    with Image.open(path) as stored:
        assert stored.width > stored.height, "stored photo should be upright (landscape, like the card)"
