"""GPT review of 0147: the PDF export's data:-only fetcher (0142) was
reported failing on an older WeasyPrint; checked here with the pinned one,
on a real PDF with the recipe's photo embedded. (The restore-recovery and
backup-mark findings are in test_deepseek_review_0143.py and
test_backup_settings_0143.py.)"""
import io
import zipfile

import pytest

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_the_pdf_bundle_embeds_the_photo_through_a_data_url(client):
    pypdf = pytest.importorskip("pypdf")
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(buf, "PNG")
    stored = client.post("/api/upload-image", headers=H,
                         files={"file": ("p.png", buf.getvalue(), "image/png")}).json()["stored_file"]
    r = client.post("/api/recipes", headers=H, json={"title": "PDF photo check", "source_type": "manual",
                                                     "image_path": stored, "ingredients": [{"raw_line": "1 egg"}],
                                                     "steps": ["Boil."]}).json()
    try:
        z = zipfile.ZipFile(io.BytesIO(client.get("/api/backup/pdfs.zip").content))
        name = next(n for n in z.namelist() if n.endswith(".pdf") and "PDF_photo_check" in n.replace(" ", "_"))
        page = pypdf.PdfReader(io.BytesIO(z.read(name))).pages[0]
        assert len(page.images) >= 1
        assert "PDF photo check" in page.extract_text()
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{r['id']}", headers=H)


def test_opencv_is_the_patched_release():
    """4.14.0.94 updates the bundled FFmpeg (CVE-2026-8461); the app reads
    still images only, so this is upkeep."""
    import cv2
    assert tuple(int(x) for x in cv2.__version__.split(".")[:2]) >= (4, 14)
