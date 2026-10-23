"""GPT review of 0104: soft-mask size, re-encoding memory, pypdf 6.21.2
(test_dependency_cves_0104.py), backup folder ownership, backup names in
the same second."""
import io
import os
import shutil
import subprocess
import threading
import time
import zlib
from datetime import datetime
from unittest.mock import patch

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject, StreamObject

from app.ingestion import pdf_ingest
from app.ingestion.pdf_ingest import extract_largest_embedded_image

BACKEND = os.path.join(os.path.dirname(__file__), "..")


# --- 1. A soft mask is decoded at its own declared size ---------------------

def _pdf_with_mask(mask_w: int, mask_h: int, mask_data_len: int, fmt: str = "PNG") -> bytes:
    from weasyprint import HTML
    import base64
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 100, 50)).save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode()
    mime = "image/jpeg" if fmt == "JPEG" else "image/png"
    pdf = HTML(string=f'<img src="data:{mime};base64,{b64}" width="400" height="300">').write_pdf()
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    mask = StreamObject()
    mask.set_data(zlib.compress(b"\xff" * mask_data_len))
    mask.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(mask_w), NameObject("/Height"): NumberObject(mask_h),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
    })
    ref = writer._add_object(mask)
    for page in writer.pages:
        for xobj in pdf_ingest._image_xobjects(page, 0, set()):
            xobj[NameObject("/SMask")] = ref
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_an_oversized_soft_mask_is_not_decoded(tmp_path):
    """A 400 x 300 photo with a 20,000 x 20,000 soft mask passed 0104's
    check: only the mask's filter was looked at."""
    path = tmp_path / "m.pdf"
    path.write_bytes(_pdf_with_mask(20000, 20000, 1000))
    from pypdf.generic import _image_xobject
    decoded = []
    with patch.object(_image_xobject, "_xobj_to_image", side_effect=lambda *a, **k: decoded.append(1)):
        assert extract_largest_embedded_image(str(path)) is None
    assert decoded == []


def test_a_jpeg_with_a_soft_mask_is_skipped_before_decoding(tmp_path):
    """pypdf returns it as JPEG 2000, which the app doesn't accept."""
    path = tmp_path / "j.pdf"
    path.write_bytes(_pdf_with_mask(400, 300, 400 * 300, fmt="JPEG"))
    from pypdf.generic import _image_xobject
    decoded = []
    with patch.object(_image_xobject, "_xobj_to_image", side_effect=lambda *a, **k: decoded.append(1)):
        assert extract_largest_embedded_image(str(path)) is None
    assert decoded == []


def test_a_soft_mask_without_a_size_is_refused():
    class X(dict):
        def get_object(self):
            return self
    image = X({"/Filter": NameObject("/FlateDecode"), "/Width": 400, "/Height": 300,
               "/SMask": X({"/Filter": NameObject("/FlateDecode")})})
    assert not pdf_ingest._decode_allowed(image, 40_000_000)
    image["/SMask"]["/Width"], image["/SMask"]["/Height"] = 400, 300
    assert pdf_ingest._decode_allowed(image, 40_000_000)


def test_a_photo_with_a_normal_soft_mask_is_still_extracted(tmp_path):
    path = tmp_path / "ok.pdf"
    path.write_bytes(_pdf_with_mask(400, 300, 400 * 300))
    data = extract_largest_embedded_image(str(path))
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (400, 300)


# --- 2. Re-encoding: two copies of the pixels, two at a time ----------------

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
    from app import file_validation
    from PIL import ImageOps
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


# --- 4. Backups made in the same second ------------------------------------

def test_a_second_backup_in_the_same_second_gets_its_own_name(tmp_path, monkeypatch, data_dir):
    from app import scheduled_backup
    monkeypatch.setattr(scheduled_backup, "BACKUP_DIR", str(tmp_path / "b"))

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 23, 13, 0, 0)

    monkeypatch.setattr(scheduled_backup, "datetime", Frozen)
    contents = iter([b"first", b"second", b"third"])

    def build(dest):
        with open(dest, "wb") as f:
            f.write(next(contents))
        return {}

    names = [scheduled_backup.make(10, build)["name"] for _ in range(3)]
    assert names == ["open-the-pantry-backup-20261023-130000.zip",
                     "open-the-pantry-backup-20261023-130000-2.zip",
                     "open-the-pantry-backup-20261023-130000-3.zip"]
    with open(tmp_path / "b" / names[0], "rb") as f:
        assert f.read() == b"first"                          # not replaced
    assert [b["name"] for b in scheduled_backup.listing()] == names[::-1]   # newest first
    assert scheduled_backup.prune(1) == names[1::-1]
    assert os.listdir(tmp_path / "b") == [names[2]]


# --- 3. The backup folder's other files keep their owner --------------------

def _backup_block() -> str:
    with open(os.path.join(BACKEND, "docker-entrypoint.sh"), encoding="utf-8") as f:
        script = f.read()
    start = script.index('if [ -n "$RECIPE_APP_BACKUP_DIR" ]')
    end = script.index("\nfi\n", start) + len("\nfi\n")
    return script[start:end]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("sh"),
                    reason="needs root to change owners")
def test_entrypoint_leaves_other_files_in_the_backup_folder_alone(tmp_path):
    """It ran chown -R over the whole folder, which can be a share."""
    folder = tmp_path / "share"
    (folder / "photos").mkdir(parents=True)
    other = folder / "photos" / "holiday.jpg"
    other.write_text("x")
    notes = folder / "notes.txt"
    notes.write_text("x")
    ours = folder / "open-the-pantry-backup-20261023-130000.zip"
    ours.write_text("x")
    for p in (folder, folder / "photos", other, notes, ours):
        os.chown(p, 0, 0)
    block = _backup_block().replace("appuser:appuser", "4321:4321")
    env = {"PATH": os.environ["PATH"], "PUID": "4321", "PGID": "4321", "RECIPE_APP_BACKUP_DIR": str(folder)}
    r = subprocess.run(["sh", "-e", "-c", block + "\necho reached\n"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "reached" in r.stdout, r.stderr
    assert os.stat(folder).st_uid == 4321 and os.stat(ours).st_uid == 4321
    assert os.stat(other).st_uid == 0 and os.stat(notes).st_uid == 0 and os.stat(folder / "photos").st_uid == 0


@pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
def test_entrypoint_starts_when_the_owner_cannot_be_changed(tmp_path):
    """With set -e, a share refusing chown stopped the whole app."""
    folder = tmp_path / "share"
    folder.mkdir()
    block = _backup_block().replace("appuser:appuser", "no-such-user-0104:no-such-user-0104")
    env = {"PATH": os.environ["PATH"], "PUID": "4321", "PGID": "4321", "RECIPE_APP_BACKUP_DIR": str(folder)}
    r = subprocess.run(["sh", "-e", "-c", block + "\necho reached\n"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "reached" in r.stdout
    assert "couldn't change the owner" in r.stderr
