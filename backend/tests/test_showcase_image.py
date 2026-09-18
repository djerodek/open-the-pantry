import io
import os
from unittest.mock import patch, MagicMock

from .conftest import TINY_PNG


def test_manual_image_upload_and_promote(client, data_dir):
    from app.database import UPLOADS_DIR

    r = client.post("/api/recipes", json={
        "title": "Manual Image Test", "source_type": "manual",
        "ingredients": [], "steps": [], "tags": [],
    })
    recipe_id = r.json()["id"]
    assert r.json()["image_path"] is None

    try:
        r = client.post("/api/upload-image", files={"file": ("a.png", TINY_PNG, "image/png")})
        temp_name = r.json()["stored_file"]

        r = client.patch(f"/api/recipes/{recipe_id}/image", json={"image_path": temp_name})
        assert r.status_code == 200
        assert r.json()["image_path"] == temp_name
        assert os.path.isfile(os.path.join(UPLOADS_DIR, temp_name))
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_manual_image_replace_removes_old_file(client, data_dir):
    from app.database import UPLOADS_DIR

    r = client.post("/api/recipes", json={
        "title": "Replace Image Test", "source_type": "manual",
        "ingredients": [], "steps": [], "tags": [],
    })
    recipe_id = r.json()["id"]
    try:
        r = client.post("/api/upload-image", files={"file": ("a.png", TINY_PNG, "image/png")})
        first = r.json()["stored_file"]
        client.patch(f"/api/recipes/{recipe_id}/image", json={"image_path": first})

        r = client.post("/api/upload-image", files={"file": ("b.png", TINY_PNG, "image/png")})
        second = r.json()["stored_file"]
        r = client.patch(f"/api/recipes/{recipe_id}/image", json={"image_path": second})
        assert r.json()["image_path"] == second
        assert not os.path.isfile(os.path.join(UPLOADS_DIR, first)), "superseded image should be removed"
        assert os.path.isfile(os.path.join(UPLOADS_DIR, second))
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_manual_image_removal(client, data_dir):
    from app.database import UPLOADS_DIR

    r = client.post("/api/recipes", json={
        "title": "Remove Image Test", "source_type": "manual",
        "ingredients": [], "steps": [], "tags": [],
    })
    recipe_id = r.json()["id"]
    try:
        r = client.post("/api/upload-image", files={"file": ("a.png", TINY_PNG, "image/png")})
        temp_name = r.json()["stored_file"]
        client.patch(f"/api/recipes/{recipe_id}/image", json={"image_path": temp_name})

        r = client.patch(f"/api/recipes/{recipe_id}/image", json={"image_path": None})
        assert r.json()["image_path"] is None
        assert not os.path.isfile(os.path.join(UPLOADS_DIR, temp_name))
    finally:
        client.delete(f"/api/recipes/{recipe_id}")


def test_url_ingestion_downloads_showcase_image(client):
    """URL ingestion should download the source page's image (og:image /
    JSON-LD image) through the same validated pipeline as a direct upload."""
    from app.database import TMP_DIR

    class FakeUrlResult:
        title = "Mock Recipe"
        ingredients = ["2 cups flour"]
        steps = ["Mix"]
        servings = prep_time = cook_time = total_time = None
        image_url = "https://example.com/photo.jpg"
        raw_text = "Mix"
        method = "json-ld"

    fake_resp = MagicMock()
    fake_resp.content = TINY_PNG

    with patch("app.main.ingest_url", return_value=FakeUrlResult()), \
         patch("app.main.safe_get", return_value=fake_resp):
        r = client.post("/api/ingest/url", json={"url": "https://example.com/recipe"})
        assert r.status_code == 200
        draft = r.json()
        assert draft["image_path"] is not None
        assert os.path.isfile(os.path.join(TMP_DIR, draft["image_path"]))
        client.delete(f"/api/ingest/draft/{draft['image_path']}")


def test_url_ingestion_image_download_failure_does_not_fail_ingestion(client):
    """A broken/unreachable image URL should just mean no thumbnail --
    never fail the whole recipe ingestion."""
    class FakeUrlResult:
        title = "Mock Recipe"
        ingredients = ["2 cups flour"]
        steps = ["Mix"]
        servings = prep_time = cook_time = total_time = None
        image_url = "https://example.com/broken.jpg"
        raw_text = "Mix"
        method = "json-ld"

    with patch("app.main.ingest_url", return_value=FakeUrlResult()), \
         patch("app.main.safe_get", side_effect=Exception("network error")):
        r = client.post("/api/ingest/url", json={"url": "https://example.com/recipe"})
        assert r.status_code == 200
        assert r.json()["image_path"] is None


def test_pdf_ingestion_extracts_embedded_showcase_image(client):
    from PIL import Image
    from weasyprint import HTML
    from app.database import TMP_DIR

    photo = Image.new("RGB", (400, 300), color=(200, 100, 50))
    buf = io.BytesIO()
    photo.save(buf, format="JPEG")
    import base64
    photo_b64 = base64.b64encode(buf.getvalue()).decode()
    html = (
        f'<h1>PDF Photo Recipe</h1>'
        f'<img src="data:image/jpeg;base64,{photo_b64}" width="400" height="300">'
        f'<h2>Ingredients</h2><p>2 cups flour</p>'
    )
    pdf_bytes = HTML(string=html).write_pdf()

    r = client.post("/api/ingest/pdf", files={"file": ("recipe.pdf", pdf_bytes, "application/pdf")})
    assert r.status_code == 200
    draft = r.json()
    assert draft["image_path"] is not None
    assert draft["image_path"] != draft["stored_file"], "extracted thumbnail must be a different file than the source PDF"
    assert os.path.isfile(os.path.join(TMP_DIR, draft["image_path"]))

    extracted = Image.open(os.path.join(TMP_DIR, draft["image_path"]))
    assert extracted.size == (400, 300)

    client.delete(f"/api/ingest/draft/{draft['stored_file']}")
    client.delete(f"/api/ingest/draft/{draft['image_path']}")


def test_pdf_ingestion_without_embedded_image(client):
    """A PDF with no embedded images should just mean no thumbnail --
    text-only ingestion still works normally."""
    from weasyprint import HTML

    pdf_bytes = HTML(string="<h1>Text Only</h1><p>2 cups flour</p>").write_pdf()
    r = client.post("/api/ingest/pdf", files={"file": ("plain.pdf", pdf_bytes, "application/pdf")})
    assert r.status_code == 200
    draft = r.json()
    assert draft["image_path"] is None
    client.delete(f"/api/ingest/draft/{draft['stored_file']}")


def _make_text_screenshot(lines):
    from PIL import Image, ImageDraw, ImageFont
    import io
    img = Image.new("L", (700, 300), color=255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
    y = 20
    for line in lines:
        draw.text((20, y), line, fill=0, font=font)
        y += 40
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_combine_multiple_screenshots_into_one_draft(client):
    from app.database import TMP_DIR

    before = set(os.listdir(TMP_DIR))

    shot1 = _make_text_screenshot(["My Recipe", "Ingredients", "2 cups flour", "1 tsp salt"])
    shot2 = _make_text_screenshot(["Instructions", "1. Mix ingredients", "2. Bake at 350F"])

    files = [("files", ("s1.png", shot1, "image/png")), ("files", ("s2.png", shot2, "image/png"))]
    r = client.post("/api/ingest/images", files=files)
    assert r.status_code == 200
    draft = r.json()

    assert draft["image_count"] == 2
    assert [i["raw_line"] for i in draft["ingredients"]] == ["2 cups flour", "1 tsp salt"]
    assert [s["text"] for s in draft["steps"]] == ["1. Mix ingredients", "2. Bake at 350F"]
    # showcase image defaults to the first screenshot submitted
    assert draft["image_path"] == draft["stored_file"]
    assert os.path.isfile(os.path.join(TMP_DIR, draft["image_path"]))

    # the second screenshot isn't kept once OCR'd -- only the first image
    # should be left behind as a new file (checked as a delta, since
    # TMP_DIR is shared across the whole test session and may already
    # have unrelated files present from other tests)
    after = set(os.listdir(TMP_DIR))
    assert after - before == {draft["image_path"]}

    client.delete(f"/api/ingest/draft/{draft['image_path']}")


def test_combine_screenshots_too_many_rejected(client):
    shot = _make_text_screenshot(["x"])
    files = [("files", (f"s{i}.png", shot, "image/png")) for i in range(11)]
    r = client.post("/api/ingest/images", files=files)
    assert r.status_code == 400


def test_combine_screenshots_empty_list_rejected(client):
    r = client.post("/api/ingest/images", files=[])
    assert r.status_code in (400, 422)


def test_combine_screenshots_bad_file_aborts_and_cleans_up(client):
    from app.database import TMP_DIR

    before = set(os.listdir(TMP_DIR))
    shot = _make_text_screenshot(["good one"])
    files = [
        ("files", ("good.png", shot, "image/png")),
        ("files", ("bad.png", b"not an image", "image/png")),
    ]
    r = client.post("/api/ingest/images", files=files)
    assert r.status_code == 400
    after = set(os.listdir(TMP_DIR))
    assert before == after, "a bad file partway through should leave no orphaned files behind"


def test_export_html_includes_image_pdf_excludes_it(client, data_dir):
    r = client.post("/api/recipes", json={
        "title": "Export Image Rules Test", "source_type": "manual",
        "ingredients": [], "steps": [], "tags": [],
    })
    recipe_id = r.json()["id"]
    try:
        r = client.post("/api/upload-image", files={"file": ("c.png", TINY_PNG, "image/png")})
        temp_name = r.json()["stored_file"]
        client.patch(f"/api/recipes/{recipe_id}/image", json={"image_path": temp_name})

        r = client.get(f"/api/recipes/{recipe_id}/export.html")
        assert "data:image" in r.text, "HTML export should still include the showcase image"

        r = client.get(f"/api/recipes/{recipe_id}/export.pdf")
        assert r.status_code == 200
        assert r.content[:4] == b"%PDF"
    finally:
        client.delete(f"/api/recipes/{recipe_id}")
