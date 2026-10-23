"""Patch 0104: Pillow 13.0.0 / pypdf 6.21.1 advisories.

The PDF showcase-image extraction used pypdf's page.images, which
decoded every embedded image (and every inline image) with whatever
Pillow parser the PDF's filter selected -- TIFF, JPEG 2000, PNG/PPM --
before the app's own JPEG/PNG/GIF/WEBP check ran. It now reads sizes from
the image dictionaries and decodes only the chosen image, and only if
its filters keep it out of those parsers."""
import base64
import io
import os
import re
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject

from app.ingestion import pdf_ingest
from app.ingestion.pdf_ingest import extract_largest_embedded_image

APP_DIR = os.path.join(os.path.dirname(__file__), "..", "app")


def _pdf_with_images(specs) -> bytes:
    """specs: [(format, (w, h), rgb)] -> a PDF showing those images."""
    from weasyprint import HTML
    tags = []
    for fmt, (w, h), color in specs:
        buf = io.BytesIO()
        Image.new("RGB", (w, h), color=color).save(buf, format=fmt)
        mime = "image/jpeg" if fmt == "JPEG" else "image/png"
        b64 = base64.b64encode(buf.getvalue()).decode()
        tags.append(f'<img src="data:{mime};base64,{b64}" width="{w}" height="{h}">')
    return HTML(string="<h1>Recipe</h1>" + "".join(tags)).write_pdf()


def _edit_images(pdf_bytes: bytes, edit) -> bytes:
    """Run edit(xobj) on every image XObject; return the rewritten PDF."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_bytes)))
    seen = set()
    for page in writer.pages:
        for xobj in pdf_ingest._image_xobjects(page, 0, seen):
            edit(xobj)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _write(tmp_path, data: bytes) -> str:
    path = tmp_path / "in.pdf"
    path.write_bytes(data)
    return str(path)


def _size_of(data: bytes):
    with Image.open(io.BytesIO(data)) as im:
        return im.size


def test_every_image_open_in_the_app_names_its_formats():
    """A bare Image.open() lets the file pick among all of Pillow's
    decoders; image_ingest.py had two."""
    bare = []
    for root, _, files in os.walk(APP_DIR):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    code = line.split("#", 1)[0]
                    if re.search(r"\bImage\.open\(", code) and "formats=" not in code:
                        bare.append(f"{os.path.relpath(path, APP_DIR)}:{n}")
    assert bare == []


def test_jpeg_and_flate_images_are_still_extracted(tmp_path):
    jpeg = _write(tmp_path, _pdf_with_images([("JPEG", (400, 300), (200, 100, 50))]))
    assert _size_of(extract_largest_embedded_image(jpeg)) == (400, 300)
    png = _write(tmp_path, _pdf_with_images([("PNG", (320, 240), (20, 120, 50))]))
    assert _size_of(extract_largest_embedded_image(png)) == (320, 240)


def test_only_the_chosen_image_is_decoded(tmp_path):
    path = _write(tmp_path, _pdf_with_images([
        ("JPEG", (300, 220), (200, 0, 0)),
        ("JPEG", (500, 400), (0, 200, 0)),
        ("PNG", (260, 210), (0, 0, 200)),
    ]))
    from pypdf.generic import _image_xobject
    calls = []
    original = _image_xobject._xobj_to_image

    def counting(xobj, *a, **kw):
        calls.append(int(xobj["/Width"]))
        return original(xobj, *a, **kw)

    with patch.object(_image_xobject, "_xobj_to_image", counting):
        data = extract_largest_embedded_image(path)
    assert _size_of(data) == (500, 400)
    assert calls == [500]


def test_an_image_with_a_parser_filter_is_never_decoded(tmp_path):
    """JPXDecode would go to Pillow's JPEG 2000 parser; the next largest
    usable image is taken instead."""
    pdf = _pdf_with_images([("JPEG", (600, 500), (9, 9, 9)), ("JPEG", (300, 250), (90, 90, 90))])

    def to_jpx(xobj):
        if int(xobj["/Width"]) == 600:
            xobj[NameObject("/Filter")] = NameObject("/JPXDecode")

    path = _write(tmp_path, _edit_images(pdf, to_jpx))
    from pypdf.generic import _image_xobject
    decoded = []
    original = _image_xobject._xobj_to_image

    def recording(xobj, *a, **kw):
        decoded.append(int(xobj["/Width"]))
        return original(xobj, *a, **kw)

    with patch.object(_image_xobject, "_xobj_to_image", recording):
        data = extract_largest_embedded_image(path)
    assert _size_of(data) == (300, 250)
    assert 600 not in decoded


def test_filters_that_reach_pillow_parsers_are_refused():
    class X(dict):
        def get_object(self):  # as pypdf objects provide
            return self

    def xobj(filters, smask=None):
        x = X()
        if filters is not None:
            x["/Filter"] = filters
        if smask is not None:
            x["/SMask"] = smask
        return x

    from pypdf.generic import ArrayObject

    def arr(*names):
        return ArrayObject(NameObject(n) for n in names)

    ok = pdf_ingest._filters_allowed
    assert ok(xobj(None))
    assert ok(xobj(NameObject("/DCTDecode")))
    assert ok(xobj(NameObject("/FlateDecode")))
    assert ok(xobj(arr("/ASCII85Decode", "/DCTDecode")))
    for bad in ("/JPXDecode", "/CCITTFaxDecode", "/JBIG2Decode", "/LZWDecode", "/ASCII85Decode"):
        assert not ok(xobj(NameObject(bad))), bad
    assert not ok(xobj(arr("/DCTDecode", "/FlateDecode")))      # DCT output re-filtered
    assert not ok(xobj(NameObject("/DCTDecode"), smask=xobj(NameObject("/JPXDecode"))))
    assert ok(xobj(NameObject("/DCTDecode"), smask=xobj(NameObject("/FlateDecode"))))


def test_an_oversized_declared_image_is_skipped_without_decoding(tmp_path):
    pdf = _pdf_with_images([("JPEG", (400, 300), (1, 2, 3))])

    def huge(xobj):
        xobj[NameObject("/Width")] = NumberObject(20000)
        xobj[NameObject("/Height")] = NumberObject(20000)

    path = _write(tmp_path, _edit_images(pdf, huge))
    from pypdf.generic import _image_xobject
    with patch.object(_image_xobject, "_xobj_to_image", side_effect=AssertionError("decoded")):
        assert extract_largest_embedded_image(path) is None


def test_page_images_is_not_used(tmp_path):
    """pypdf's page.images decodes inline images while listing them."""
    from pypdf import PageObject
    path = _write(tmp_path, _pdf_with_images([("JPEG", (400, 300), (5, 5, 5))]))
    with patch.object(PageObject, "images", property(lambda self: (_ for _ in ()).throw(AssertionError("page.images")))):
        assert _size_of(extract_largest_embedded_image(path)) == (400, 300)


def test_a_broken_pdf_gives_no_image(tmp_path):
    assert extract_largest_embedded_image(_write(tmp_path, b"%PDF-1.4\nnot really")) is None


def test_patched_library_versions():
    import PIL
    import pypdf

    def v(s):
        return tuple(int(p) for p in re.findall(r"\d+", s)[:3])

    assert v(PIL.__version__) >= (13, 0, 0)
    assert v(pypdf.__version__) >= (6, 21, 1)
