"""GPT review of 0132: a bare "©" or "Copyright" line crashed the PDF/photo
reader (IndexError); a Duplicate whose photo copy failed part way left a
partial file."""
import os

import pytest

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.mark.parametrize("footer", ["©", "Copyright", "  ©  ", "COPYRIGHT"])
def test_a_bare_copyright_line_ends_the_steps(footer):
    from app.ingestion.pdf_ingest import rule_segment_raw_text
    s = rule_segment_raw_text("\n".join(["Bread", "Ingredients", "2 cups flour", "Method",
                                         "Mix the flour.", "Bake 30 minutes.", footer, "Breads (7)"]))
    assert s["steps"] == ["Mix the flour.", "Bake 30 minutes."]


def test_a_failed_duplicate_photo_copy_leaves_nothing(client, monkeypatch):
    from app import main
    photo = client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]
    r = client.post("/api/recipes", headers=H, json={"title": "With photo", "source_type": "manual",
                                                     "image_path": photo}).json()
    try:
        before = set(os.listdir(main.UPLOADS_DIR))
        titles = {x["title"] for x in client.get("/api/recipes").json()}

        def half_copy(src, dst):
            with open(dst, "wb") as f:
                f.write(b"partial")
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(main.shutil, "copyfile", half_copy)
        res = client.post(f"/api/recipes/{r['id']}/duplicate", headers=H)
        assert res.status_code == 500 and "Nothing was changed" in res.json()["detail"]
        assert set(os.listdir(main.UPLOADS_DIR)) == before
        assert {x["title"] for x in client.get("/api/recipes").json()} == titles
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{r['id']}", headers=H)
