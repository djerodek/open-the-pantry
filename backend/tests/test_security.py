import asyncio
import http.server
import io
import json
import os
import socket
import struct
import threading
import time
import uuid
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from app import file_validation

from . import _zip_directory
from ._zip_directory import make as make_directory_zip
from .conftest import TINY_PNG
from .test_restore import H


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
    """Regression test: a naive open()->save() re-encode reliably raises
    'broken data stream' on a file with bytes appended after a valid PNG
    stream, which would incorrectly reject an otherwise-fine upload.
    reencode_image copies the decoded pixels into a fresh image (now via
    Image.copy(); see its docstring for why not getdata()/putdata() or
    tobytes()/frombytes())
    specifically because that handles this correctly. This locks that in."""
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
    # Recently deleted (0109) keeps the file until the recipe goes for good.
    assert os.path.isfile(os.path.join(UPLOADS_DIR, temp_name))
    client.delete(f"/api/recently-deleted/{recipe_id}")
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

    from app.ingestion.url_ingest import UrlValidationError, safe_get

    real_getaddrinfo = socket.getaddrinfo

    def fake_get(url, headers=None, timeout=None, allow_redirects=None, **kwargs):
        import io
        resp = MagicMock()
        # A real (empty) body: _read_limited reads resp.raw.read1(), and a
        # bare MagicMock never returns EOF -- the fetch would run to the 30 s
        # deadline and raise UrlValidationError, passing this test for the
        # wrong reason.
        resp.raw = io.BytesIO(b"")
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

    with patch("app.ingestion.url_ingest.http_get", side_effect=fake_get), \
         patch("app.ingestion.url_ingest.socket.getaddrinfo", side_effect=fake_dns):
        try:
            safe_get("http://evil-redirect.example.com/recipe")
            assert False, "redirect to a private/metadata address should have been blocked"
        except UrlValidationError as e:
            assert "private" in str(e), f"blocked for the wrong reason: {e}"


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
    from unittest.mock import MagicMock, patch

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

    from .conftest import text_font
    font = text_font(30)
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


def _lying_zip(name: str, real: int, stated: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, b"\0" * real)
    data = bytearray(buf.getvalue())
    struct.pack_into("<I", data, data.find(b"PK\x03\x04") + 22, stated)
    struct.pack_into("<I", data, data.find(b"PK\x01\x02") + 24, stated)
    return bytes(data)


def _bilevel(tmp_path, w, h):
    p = tmp_path / f"{w}x{h}.png"
    Image.new("1", (w, h)).save(p)              # a few KB on disk
    return p.read_bytes()


def _multipart(name, content, boundary="b0119"):
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _send_chunked(path, chunks, content_type, cookie=None):
    """Drives BodySizeLimit directly, the body in chunks with no
    Content-Length: returns (status, body, bytes the inner app received)."""
    from app.main import BodySizeLimit
    received = {"n": 0}

    async def inner(scope, receive, send):
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                break
            received["n"] += len(m.get("body", b""))
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})

    pending = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    out = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(m):
        out.append(m)

    headers = [(b"content-type", content_type.encode()), (b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b"",
             "client": ("127.0.0.1", 1), "server": ("testserver", 80), "scheme": "http", "root_path": ""}
    asyncio.run(BodySizeLimit(inner)(scope, receive, send))
    status = next(m["status"] for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return status, body, received["n"]


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


@pytest.fixture
def local_server():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Host")))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], seen
    httpd.shutdown()


def test_no_route_map_on_the_main_port(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


@pytest.mark.parametrize("entries,zip64,lie", [(30_000, False, None), (60_000, True, None), (400_000, False, 10)])
def test_an_oversized_zip_directory_is_refused_before_parsing(tmp_path, monkeypatch, entries, zip64, lie):
    """Refused from the end record alone: zipfile.ZipFile is never called."""
    from app import interchange, restore
    path = tmp_path / "big.zip"
    make_directory_zip(str(path), entries, zip64, lie)

    def no_parse(*a, **k):
        raise AssertionError("the directory was parsed")
    monkeypatch.setattr(zipfile, "ZipFile", no_parse)
    with pytest.raises(interchange.ImportFileError, match="files; the limit|too large"):
        with interchange.opened(str(path), "big.zip"):
            pass
    if entries > restore.MAX_ENTRIES or lie:
        with pytest.raises(restore.RestoreError):
            restore.kind_of(str(path))
        with pytest.raises(restore.RestoreError):
            restore.inspect(str(path), str(tmp_path / "x.db"))


def test_the_open_endpoints_refuse_it(client, tmp_path):
    path = tmp_path / "big.zip"
    make_directory_zip(str(path), 30_000)
    data = path.read_bytes()
    r = client.post("/api/import/recipes", headers=H, files={"file": ("big.zip", data, "application/zip")})
    assert r.status_code == 422 and "30000" in r.json()["detail"]


def test_ordinary_zips_still_open(tmp_path):
    from app.file_validation import check_zip_size, zip_directory_size
    path = tmp_path / "ok.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(10):
            zf.writestr(f"uploads/{uuid.uuid4().hex}.jpg", b"x" * 10)
    assert zip_directory_size(str(path))[0] == 10
    check_zip_size(str(path), 20, ValueError)
    with pytest.raises(ValueError):
        check_zip_size(str(path), 5, ValueError)


def test_huge_images_are_refused(tmp_path):
    from PIL import Image

    from app import file_validation
    assert Image.MAX_IMAGE_PIXELS * 2 == file_validation.MAX_DECODED_PIXELS   # refused above 50 MP from 0106
    big = tmp_path / "big.png"
    Image.new("1", (10_000, 9_000)).save(big)          # 90 MP, a few KB on disk
    assert os.path.getsize(big) < 100_000
    assert file_validation.validate_and_save_image_bytes(big.read_bytes(), str(tmp_path), "img-x") is None


def test_only_draft_names_can_be_discarded(client):
    from app.database import TMP_DIR
    other = os.path.join(TMP_DIR, "keep-me.txt")
    with open(other, "w") as f:
        f.write("x")
    try:
        r = client.delete("/api/ingest/draft/keep-me.txt")
        assert r.status_code == 200 and os.path.exists(other)
    finally:
        os.remove(other)


def test_a_zip_entry_never_unpacks_past_its_stated_size(tmp_path):
    from app import restore
    path = tmp_path / "b.zip"
    path.write_bytes(_lying_zip("uploads/img-" + "a" * 32 + ".png", real=20 * 1024 * 1024, stated=1024))
    out = tmp_path / "out"
    kept, skipped = restore.unpack_uploads(str(path), str(out))
    assert (kept, skipped) == (0, 1)
    assert list(out.iterdir()) == []


def test_copy_upload_refuses_a_name_outside_uploads(tmp_path):
    from fastapi import HTTPException

    from app import main, routes
    src = tmp_path / "a.jpg"
    src.write_bytes(b"x")
    with pytest.raises(HTTPException):
        routes.common._copy_upload(str(src), "../escape.jpg", "test")
    assert not (Path(main.UPLOADS_DIR).parent / "escape.jpg").exists()


def test_a_wildcard_host_list_is_warned_about():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "main.py"), encoding="utf-8").read()
    assert 'if "*" in ALLOWED_HOSTS:' in src and "DNS rebinding" in src


@pytest.mark.parametrize("address,blocked", [("64:ff9b::7f00:1", True), ("64:ff9b::c0a8:101", True),
                                              ("64:ff9b::5db8:d822", False)])
def test_nat64_addresses_are_judged_by_the_ipv4_inside(address, blocked):
    from app.ingestion.url_ingest import UrlValidationError, validate_public_url
    answer = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))]
    with patch("app.ingestion.url_ingest.socket.getaddrinfo", return_value=answer):
        if blocked:
            with pytest.raises(UrlValidationError):
                validate_public_url("http://nat64.example.com/")
        else:
            assert validate_public_url("http://nat64.example.com/") == "64:ff9b::5db8:d822"


def test_reencode_copies_the_image_once(tmp_path):
    """tobytes() + frombytes() held about four copies of the pixels."""
    from app.file_validation import reencode_image
    path = tmp_path / "p.jpg"
    Image.new("RGB", (64, 48), (1, 2, 3)).save(path)
    with patch.object(Image.Image, "tobytes", side_effect=AssertionError("tobytes")):
        reencode_image(str(path))
    with Image.open(path) as im:
        assert im.size == (64, 48)


def test_reencode_still_drops_metadata_and_keeps_palette_transparency(tmp_path):
    from app.file_validation import reencode_image
    jpg = tmp_path / "m.jpg"
    exif = Image.Exif()
    exif[0x0110] = "Camera"
    exif[0x0112] = 6                                  # turned: stored 60 x 40
    Image.new("RGB", (60, 40), (9, 9, 9)).save(jpg, exif=exif.tobytes(), icc_profile=b"\0" * 200,
                                                comment=b"hello")
    reencode_image(str(jpg))
    with Image.open(jpg) as im:
        assert im.size == (40, 60)
        assert "exif" not in im.info and "icc_profile" not in im.info and "comment" not in im.info
    gif = tmp_path / "p.gif"
    pal = Image.new("P", (10, 10), 3)
    pal.paste(1, (0, 0, 5, 10))
    pal.putpalette([0, 0, 0, 255, 0, 0, 0, 255, 0, 0, 0, 255] + [0] * (256 * 3 - 12))
    pal.save(gif, transparency=3, comment=b"note")
    with Image.open(gif) as im:
        before = (im.info["transparency"], im.getpalette()[:12], im.tobytes())
        assert im.info.get("comment")
    reencode_image(str(gif))
    with Image.open(gif) as im:
        assert im.mode == "P"
        assert (im.info.get("transparency"), im.getpalette()[:12], im.tobytes()) == before
        assert not im.info.get("comment")


def test_at_most_two_reencodes_at_once(tmp_path):
    from PIL import ImageOps

    from app import file_validation
    paths = []
    for i in range(5):
        p = tmp_path / f"{i}.png"
        Image.new("RGB", (20, 20), (i, i, i)).save(p)
        paths.append(str(p))
    lock, running, most = threading.Lock(), [0], [0]
    real = ImageOps.exif_transpose

    def slow(img, **kw):
        with lock:
            running[0] += 1
            most[0] = max(most[0], running[0])
        time.sleep(0.15)
        with lock:
            running[0] -= 1
        return real(img, **kw)

    with patch.object(ImageOps, "exif_transpose", slow):
        threads = [threading.Thread(target=file_validation.reencode_image, args=(p,)) for p in paths]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert most[0] == 2


def test_50_megapixels_is_a_hard_limit(tmp_path):
    """0099's 40 MP setting refused only above 80 MP: Pillow refuses above
    twice MAX_IMAGE_PIXELS and just warns in between."""
    assert file_validation.MAX_DECODED_PIXELS == 50_000_000
    over = _bilevel(tmp_path, 10_000, 5_100)    # 51 MP: 0105 accepted it
    assert file_validation.validate_and_save_image_bytes(over, str(tmp_path), "img-over") is None
    under = _bilevel(tmp_path, 8_000, 6_000)    # 48 MP, a large phone photo
    name = file_validation.validate_and_save_image_bytes(under, str(tmp_path), "img-under")
    assert name is not None
    with Image.open(tmp_path / name) as im:
        assert im.size == (8_000, 6_000)


def test_an_upload_stops_at_its_routes_limit(client):
    """The reviewer's check: multipart went through whatever its size."""
    from app import main, routes
    mb = 1024 * 1024
    body, ctype = _multipart("big.png", b"x" * (22 * mb))
    chunks = [body[i:i + mb] for i in range(0, len(body), mb)]
    status, answer, seen = _send_chunked("/api/upload-image", chunks, ctype)
    assert status == 413 and json.loads(answer)["detail"] == "File too large (max 20 MB)."
    assert seen <= main.MAX_UPLOAD_BYTES + main.MULTIPART_MARGIN_BYTES        # stopped as it arrived
    # Under the limit: through to the route.
    small, ctype = _multipart("p.png", b"x" * mb)
    assert _send_chunked("/api/upload-image", [small], ctype)[0] == 200
    # A multipart body to any other route: the 32 MB of other bodies.
    assert main._multipart_limit({"path": "/api/recipes"})[0] == main.MAX_JSON_BODY_BYTES + main.MULTIPART_MARGIN_BYTES


def test_a_declared_length_over_the_limit_is_refused_unread(client):
    body, ctype = _multipart("big.pdf", b"%PDF" + b"x" * (21 * 1024 * 1024))
    r = client.post("/api/ingest/pdf", content=body, headers={"Content-Type": ctype})
    assert r.status_code == 413
    fr = client.post("/api/ingest/pdf", content=body, headers={"Content-Type": ctype, "X-App-Lang": "fr"})
    assert fr.json()["detail"] == "Fichier trop volumineux (maximum 20 Mo)."
    # The limits per route, as the routes have them.
    from app import main, routes
    m = main.MULTIPART_MARGIN_BYTES
    assert main._multipart_limit({"path": "/api/import/recipes"})[0] == routes.backup.MAX_IMPORT_BYTES + m
    assert main._multipart_limit({"path": "/api/ingest/pdf/batch"})[0] == routes.ingest.MAX_BATCH_PDF_BYTES + m
    assert main._multipart_limit({"path": "/api/ingest/images"})[0] == routes.ingest.MAX_COMBINED_IMAGES * main.MAX_UPLOAD_BYTES + m
    # Uploads under the limits still work.
    assert client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).status_code == 200


def test_a_lying_entry_count_under_the_size_bound_is_refused(tmp_path, monkeypatch):
    """100,000 real entries listed as 10: 4.7 MB of directory, under the
    5.12 MB bound for 20,000; refused by counting, before zipfile parses."""
    from app import file_validation
    path = tmp_path / "liar.zip"
    _zip_directory.make(str(path), 100_000, lie=10)
    assert os.path.getsize(path) < 20_000 * file_validation.ZIP_DIR_BYTES_PER_ENTRY

    def no_parse(*a, **k):
        raise AssertionError("zipfile parsed it")
    monkeypatch.setattr(zipfile, "ZipFile", no_parse)
    with pytest.raises(ValueError, match="more than 20000"):
        file_validation.check_zip_size(str(path), 20_000, ValueError)


def test_a_true_count_at_the_limit_still_opens(tmp_path):
    from app import file_validation
    path = tmp_path / "ok.zip"
    _zip_directory.make(str(path), 20_000)
    file_validation.check_zip_size(str(path), 20_000, ValueError)
    real = tmp_path / "real.zip"
    with zipfile.ZipFile(real, "w") as zf:
        for i in range(3):
            zf.writestr(f"f{i}.txt", "x" * i, compress_type=zipfile.ZIP_DEFLATED)
        zf.comment = b"PK\x05\x06 in a comment"
    file_validation.check_zip_size(str(real), 3, ValueError)


def test_image_bytes_over_the_cap_are_refused(tmp_path):
    from PIL import Image

    from app.file_validation import validate_and_save_image_bytes
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(buf, format="PNG")
    data = buf.getvalue()
    assert validate_and_save_image_bytes(data, str(tmp_path), "img-a", max_bytes=len(data) - 1) is None
    assert list(tmp_path.iterdir()) == []
    assert validate_and_save_image_bytes(data, str(tmp_path), "img-b") is not None


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8099\\@example.com/?action=x", "http://example.com\\@127.0.0.1/",
    "http://example.com/\\..\\x", "http://exa\tmple.com/", "http://example.com/a\nb", "http://example.com/\x00",
])
def test_a_backslash_or_control_character_is_refused(url):
    from app.ingestion.url_ingest import UrlValidationError, validate_public_url
    with patch("app.ingestion.url_ingest.socket.getaddrinfo", return_value=PUBLIC):
        with pytest.raises(UrlValidationError):
            validate_public_url(url)


def test_the_checked_address_is_returned():
    from app.ingestion.url_ingest import validate_public_url
    with patch("app.ingestion.url_ingest.socket.getaddrinfo", return_value=PUBLIC):
        assert validate_public_url("https://www.example.com/recipe?x=1") == "93.184.216.34"


def test_safe_get_connects_to_the_checked_address():
    from app.ingestion import url_ingest
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        raise url_ingest.requests.exceptions.ConnectionError("stop here")
    with patch.object(url_ingest.socket, "getaddrinfo", return_value=PUBLIC), \
         patch.object(url_ingest, "http_get", side_effect=fake_get):
        with pytest.raises(url_ingest.SiteUnreachableError):
            url_ingest.safe_get("http://rebind.example.com/recipe")
    assert seen["pin"] == "93.184.216.34"


def test_a_pinned_request_doesnt_look_the_name_up_again(local_server):
    """The name below doesn't resolve at all: the request still reaches the
    pinned address, with the name in the Host header."""
    from app import tls
    port, seen = local_server
    with patch("socket.getaddrinfo", wraps=socket.getaddrinfo) as lookups:
        r = tls.get(f"http://never-resolves.invalid:{port}/x?y=1", pin="127.0.0.1", timeout=5)
    assert r.text == "ok" and seen == [("/x?y=1", f"never-resolves.invalid:{port}")]
    assert not [c for c in lookups.call_args_list if c.args and c.args[0] == "never-resolves.invalid"]


def test_a_pinned_session_refuses_another_host(local_server):
    import requests

    from app import tls
    port, _ = local_server
    adapter = tls.PinnedAdapter("one.example", "127.0.0.1")
    with requests.Session() as s:
        s.trust_env = False
        s.mount("http://", adapter)
        with pytest.raises(requests.exceptions.InvalidURL):
            s.get(f"http://two.example:{port}/", timeout=5)


def test_the_original_bypass_reaches_nothing(local_server):
    """The audit's reproduction, end to end through safe_get."""
    from app.ingestion import url_ingest
    port, seen = local_server
    real = socket.getaddrinfo
    # example.com answers with a public address; everything else as usual
    # (socket is one module: a blanket patch would move 127.0.0.1 too).
    lookup = lambda host, *a, **k: PUBLIC if host == "example.com" else real(host, *a, **k)  # noqa: E731
    with patch.object(url_ingest.socket, "getaddrinfo", side_effect=lookup):
        with pytest.raises(url_ingest.UrlValidationError):
            url_ingest.safe_get(f"http://127.0.0.1:{port}\\@example.com/?action=x")
    assert seen == []


def test_a_pdf_draft_cant_be_a_recipe_photo(client):
    """(10) A PDF draft's name was accepted as image_path and served from
    /uploads."""
    from pydantic import ValidationError

    from app import schemas
    name = "pdf-" + "a" * 32 + ".pdf"
    with pytest.raises(ValidationError):
        schemas.RecipeCreate(title="x", source_type="manual", image_path=name)
    assert schemas.RecipeCreate(title="x", source_type="manual", image_path="img-" + "a" * 32 + ".jpg").image_path


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.100.100.100", "192.0.0.8", "198.18.0.1", "::ffff:10.0.0.1"])
def test_ssrf_guard_blocks_non_public_ranges(ip):
    from app.ingestion import url_ingest
    fam = 10 if ":" in ip else 2
    with patch.object(url_ingest.socket, "getaddrinfo", return_value=[(fam, 1, 6, "", (ip, 0))]):
        with pytest.raises(url_ingest.UrlValidationError):
            url_ingest.validate_public_url("http://looks-public.example/recipe")


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
    import app.routes.email as m_email
    bare = TestClient(m.app)   # no default X-Requested-With
    assert bare.post("/api/email-settings/scan").status_code == 403
    assert bare.delete("/api/recipes/999999").status_code == 403
    assert bare.get("/api/recipes").status_code == 200      # reads unaffected


def test_a_recipe_cannot_claim_another_recipes_photo(client):
    """Recipe B pointed at A's photo; deleting B deleted A's file."""
    from app import models
    from app.database import UPLOADS_DIR, SessionLocal

    img = f"img-{uuid.uuid4().hex}.png"
    path = os.path.join(UPLOADS_DIR, img)
    open(path, "wb").write(b"x")
    db = SessionLocal()
    a = models.Recipe(title="Photo owner", source_type="manual", image_path=img)
    db.add(a); db.commit(); aid = a.id; db.close()
    try:
        r = client.post("/api/recipes", json={"title": "Thief", "source_type": "manual", "ingredients": [],
                                             "steps": [], "tags": [], "image_path": img})
        assert r.status_code == 400
        assert os.path.exists(path)
        # A itself can still be saved with its own photo
        body = client.get(f"/api/recipes/{aid}").json()
        payload = {k: body[k] for k in ("title", "source_type")}
        payload.update(ingredients=[], steps=[], tags=[], image_path=img)
        assert client.put(f"/api/recipes/{aid}", json=payload).status_code == 200
        assert os.path.exists(path)
    finally:
        client.delete(f"/api/recipes/{aid}")
