"""GPT review of 0105 (owner's choices): a hard 50 MP limit on every image
the app decodes, and the entrypoint's backup-file match narrowed to the
names the app writes."""
import io
import os
import shutil
import subprocess

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

from app import file_validation
from app.ingestion import pdf_ingest

BACKEND = os.path.join(os.path.dirname(__file__), "..")


def _bilevel(tmp_path, w, h):
    p = tmp_path / f"{w}x{h}.png"
    Image.new("1", (w, h)).save(p)              # a few KB on disk
    return p.read_bytes()


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


def test_pdf_photos_use_the_same_limit(tmp_path):
    from weasyprint import HTML
    import base64
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (1, 2, 3)).save(buf, format="JPEG")
    pdf = HTML(string=f'<img src="data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}">').write_pdf()

    def declared(w, h):
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
        for page in writer.pages:
            for xobj in pdf_ingest._image_xobjects(page, 0, set()):
                xobj[NameObject("/Width")] = NumberObject(w)
                xobj[NameObject("/Height")] = NumberObject(h)
        path = tmp_path / f"{w}.pdf"
        out = io.BytesIO()
        writer.write(out)
        path.write_bytes(out.getvalue())
        return str(path)

    from unittest.mock import patch
    from pypdf.generic import _image_xobject
    for (w, h), expect_decode in (((10_000, 5_100), False), ((8_000, 6_000), True)):
        decoded = []
        with patch.object(_image_xobject, "_xobj_to_image", side_effect=lambda *a, **k: decoded.append(1)):
            pdf_ingest.extract_largest_embedded_image(declared(w, h))
        assert bool(decoded) is expect_decode, (w, h)


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("sh"),
                    reason="needs root to change owners")
def test_entrypoint_takes_only_the_apps_backup_names(tmp_path):
    folder = tmp_path / "share"
    folder.mkdir()
    ours = ["open-the-pantry-backup-20261023-130000.zip",
            "open-the-pantry-backup-20261023-130000-2.zip",
            "open-the-pantry-backup-20261023-130000.zip.partial"]
    alike = ["open-the-pantry-backup-notes.zip.bak",
             "open-the-pantry-backup-archive.zip.asc",
             "open-the-pantry-backup-20261023-130000.zip.bak",
             "open-the-pantry-backup-old.zip"]
    for n in ours + alike:
        (folder / n).write_text("x")
        os.chown(folder / n, 0, 0)
    with open(os.path.join(BACKEND, "docker-entrypoint.sh"), encoding="utf-8") as f:
        script = f.read()
    start = script.index('if [ -n "$RECIPE_APP_BACKUP_DIR" ]')
    block = script[start:script.index("\nfi\n", start) + 4].replace("appuser:appuser", "4321:4321")
    env = {"PATH": os.environ["PATH"], "PUID": "4321", "PGID": "4321", "RECIPE_APP_BACKUP_DIR": str(folder)}
    r = subprocess.run(["sh", "-e", "-c", block], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert {n: os.stat(folder / n).st_uid for n in ours} == {n: 4321 for n in ours}
    assert {n: os.stat(folder / n).st_uid for n in alike} == {n: 0 for n in alike}
