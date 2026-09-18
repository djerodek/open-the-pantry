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
