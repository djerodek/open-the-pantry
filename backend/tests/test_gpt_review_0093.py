"""GPT review of 0093.

1. A zip's JSON file with more recipes than an import takes was read in
   full, then cut: the walk now stops at the limit.
2. The restore check read manifest.json whole, whatever its size; one
   larger than the app writes is now ignored.
3. Restored photos were checked by their first bytes only; now to the
   upload standard (type matches the name, PDFs only as .pdf, full decode,
   anything after the image data re-encoded away).
5. http://x:443 and https://x:80 counted as the plain address.
7. A deeply nested JSON file and a Paprika record with a number as its
   difficulty gave a 500.
(6, the duplicate warning's race, is in app.js; checked in a browser.)
"""
import io
import json
import os
import zipfile

import pytest

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _zip(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return out.getvalue()


def test_a_big_json_file_is_read_only_up_to_the_limit(tmp_path, monkeypatch):
    from app import interchange
    monkeypatch.setattr(interchange, "MAX_RECIPES", 3)
    calls = []
    real = interchange.from_jsonld
    monkeypatch.setattr(interchange, "from_jsonld", lambda *a, **k: calls.append(1) or real(*a, **k))
    many = [{"@type": "Recipe", "name": f"R{i}", "recipeIngredient": ["1 egg"], "recipeInstructions": ["Boil."]}
            for i in range(50)]
    path = tmp_path / "big.zip"
    path.write_bytes(_zip({"recipes/a.json": json.dumps(many)}))
    recipes, problems = interchange.read_file(str(path), "big.zip")
    assert len(recipes) == 3 and len(calls) == 3
    assert any("More than 3 recipes" in p["error"] for p in problems)
    assert len(interchange._recipe_items(many, 4)) == 4
    # A plain .json file the same way.
    path = tmp_path / "big.json"
    path.write_text(json.dumps(many))
    calls.clear()
    recipes, problems = interchange.read_file(str(path), "big.json")
    assert len(recipes) == 3 and len(calls) == 3 and problems


def test_malformed_import_files_are_refused_not_500(client):
    import gzip
    deep = "[" * 10_000 + "]" * 10_000
    r = client.post("/api/import/recipes", headers=H, files={"file": ("deep.json", deep.encode(), "application/json")})
    assert r.status_code == 422
    paprika = _zip({"x.paprikarecipe": gzip.compress(json.dumps({
        "name": "GPT difficulty number", "ingredients": "1 egg", "directions": "Boil.", "difficulty": 3,
        "categories": "not a list", "rating": "five"}).encode())})
    body = client.post("/api/import/recipes", headers=H,
                       files={"file": ("p.paprikarecipes", paprika, "application/zip")}).json()
    try:
        assert len(body["succeeded"]) == 1 and body["failed"] == []
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [s["recipe_id"] for s in body["succeeded"]]})


def test_one_bad_record_is_that_records_failure(tmp_path, monkeypatch):
    from app import interchange
    real = interchange.from_jsonld

    def picky(item, *a, **k):
        if item.get("name") == "bad":
            raise TypeError("unexpected field")
        return real(item, *a, **k)
    monkeypatch.setattr(interchange, "from_jsonld", picky)
    path = tmp_path / "two.json"
    path.write_text(json.dumps([{"@type": "Recipe", "name": "bad"}, {"@type": "Recipe", "name": "good"}]))
    recipes, problems = interchange.read_file(str(path), "two.json")
    assert [r.title for r in recipes] == ["good"] and len(problems) == 1


def test_an_oversized_manifest_is_not_read(client, tmp_path, monkeypatch):
    from app import restore
    backup = client.get("/api/backup/database.zip").content
    with zipfile.ZipFile(io.BytesIO(backup)) as zf:
        db = zf.read("recipes.db")
    monkeypatch.setattr(restore, "MAX_MANIFEST_BYTES", 100)
    path = tmp_path / "b.zip"
    path.write_bytes(_zip({"recipes.db": db, "manifest.json": json.dumps({"created_at": "x", "pad": "y" * 500})}))
    read = []
    real = zipfile.ZipFile.read
    monkeypatch.setattr(zipfile.ZipFile, "read", lambda self, n, *a: read.append(getattr(n, "filename", n)) or real(self, n, *a))
    info = restore.inspect(str(path), str(tmp_path / "b.db"))
    assert info["created_at"] is None and "manifest.json" not in read
    path.write_bytes(_zip({"recipes.db": db, "manifest.json": json.dumps({"created_at": "2026-10-17"})}))
    assert restore.inspect(str(path), str(tmp_path / "c.db"))["created_at"] == "2026-10-17"


def _png(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def test_restored_files_meet_the_upload_standard(tmp_path):
    from app import restore
    from PIL import Image
    clean = _png(tmp_path, "img-a.png", TINY_PNG)
    assert restore._content_ok(clean, "img-a.png") and open(clean, "rb").read() == TINY_PNG   # kept as is
    # A JPEG made the way uploads are, kept byte for byte.
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 100, 50)).save(buf, "JPEG", quality=90)
    jpg = _png(tmp_path, "img-b.jpg", buf.getvalue())
    assert restore._content_ok(jpg, "img-b.jpg") and open(jpg, "rb").read() == buf.getvalue()
    # Trailing content after the image: re-encoded, which drops it.
    poly = _png(tmp_path, "img-c.png", TINY_PNG + b"<script>alert(1)</script>")
    assert restore._content_ok(poly, "img-c.png")
    data = open(poly, "rb").read()
    assert b"<script>" not in data and data.endswith(b"IEND\xaeB`\x82")
    # Wrong type for the name, or not an image at all.
    assert not restore._content_ok(_png(tmp_path, "img-d.jpg", TINY_PNG), "img-d.jpg")
    assert not restore._content_ok(_png(tmp_path, "img-e.jpg", b"%PDF-1.4 x"), "img-e.jpg")
    assert restore._content_ok(_png(tmp_path, "doc-f.pdf", b"%PDF-1.4 x"), "doc-f.pdf")
    assert not restore._content_ok(_png(tmp_path, "img-g.png", TINY_PNG[:30] + b"\x00" * 40), "img-g.png")


def test_only_the_schemes_own_port_is_dropped():
    from app.duplicates import normalize_url
    assert normalize_url("http://example.test:80/r") == normalize_url("http://example.test/r") == "example.test/r"
    assert normalize_url("https://example.test:443/r") == "example.test/r"
    assert normalize_url("http://example.test:443/r") == "example.test:443/r"
    assert normalize_url("https://example.test:80/r") == "example.test:80/r"


def test_restored_photos_with_metadata_are_cleaned(tmp_path):
    """GPT review of 0095: a JPEG ending cleanly kept its EXIF (camera,
    possibly location), which an upload would have dropped."""
    from app import restore
    from PIL import Image, PngImagePlugin
    exif = Image.Exif()
    exif[0x010E] = "kitchen photo"
    exif[0x013B] = "Someone"
    buf = io.BytesIO()
    Image.new("RGB", (16, 16), (200, 100, 50)).save(buf, "JPEG", exif=exif.tobytes(), comment=b"hello")
    jpg = _png(tmp_path, "img-h.jpg", buf.getvalue())
    assert restore._content_ok(jpg, "img-h.jpg")
    with Image.open(jpg) as im:
        assert "exif" not in im.info and "comment" not in im.info and restore._metadata(im) == []
    meta = PngImagePlugin.PngInfo()
    meta.add_text("Author", "Someone")
    buf = io.BytesIO()
    Image.new("RGB", (16, 16)).save(buf, "PNG", pnginfo=meta, icc_profile=b"\0" * 128)
    png = _png(tmp_path, "img-i.png", buf.getvalue())
    with Image.open(png) as im:
        im.load()
        assert set(restore._metadata(im)) >= {"icc_profile", "text"}
    assert restore._content_ok(png, "img-i.png")
    with Image.open(png) as im:
        im.load()
        assert restore._metadata(im) == []
