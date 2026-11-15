"""Reading a PDF: its text layer page by page (a page without one goes to
OCR) and its largest embedded image. Split out of pdf_ingest.py (0186)."""
import time

import pdfplumber

from ..logging_setup import get_logger
# Private to ocr, but this module's to use: rendering a scanned page at the
# OCR budget and reading it are one job split across the two (0186).
from .ocr import _ocr_image, _ocr_resolution

log = get_logger("pdf")

MIN_CHARS_PER_PAGE = 20
MAX_PDF_PAGES = 60  # generous for even a multi-recipe scanned chapter; bounds worst-case OCR time


# OCR time for one PDF, all its scanned pages together. Each Tesseract run
# has its own limit (OCR_TIMEOUT_SECONDS, up to two runs a page), so 60
# scanned pages could hold an OCR slot for hours (GPT review of 0190). A
# recipe's few pages take seconds each.
MAX_PDF_OCR_SECONDS = 10 * 60


class PdfTooLargeError(ValueError):
    pass


MIN_SHOWCASE_IMAGE_DIMENSION = 200  # px -- avoids picking tiny icons/bullets/logos as the showcase image


# PDF image filters whose decoding stays out of Pillow's file parsers.
# The last filter decides how pypdf turns the stream into an image:
# DCTDecode goes to the JPEG decoder; Flate and RunLength output (and no
# filter at all) is raw pixel data that Pillow only copies. LZWDecode and
# ASCII85Decode as the last filter, CCITTFaxDecode, JBIG2Decode and
# JPXDecode send the bytes to Pillow's TIFF, PNG/PPM and JPEG 2000
# parsers, which are skipped: recipe photos in a PDF are almost always
# JPEG or Flate, and those parsers are where Pillow's decoder bugs have
# been (CVE-2026-112896, CVE-2026-113786 and others). Earlier filters in
# a chain only unwrap the stream and never reach Pillow.
_SHOWCASE_LAST_FILTERS = {"/DCTDecode", "/FlateDecode", "/RunLengthDecode"}
_STREAM_ONLY_FILTERS = {"/ASCII85Decode", "/ASCIIHexDecode", "/FlateDecode", "/RunLengthDecode", "/LZWDecode"}
_MAX_FORM_DEPTH = 4         # Form XObjects nested inside each other
_MAX_DECODE_ATTEMPTS = 5    # candidates decoded, largest first, before giving up


def _decode_allowed(xobj, max_pixels, depth: int = 0) -> bool:
    """True if this image, and any soft mask pypdf would decode with it,
    uses only the filters above. A soft mask is decoded at its own
    /Width x /Height whatever the image's size, so it has to declare a
    size within max_pixels too (the image's own size is checked by the
    caller)."""
    if depth > 2:
        return False
    if depth > 0:
        size = _declared_size(xobj)
        if size is None or (max_pixels and size[0] * size[1] > max_pixels):
            return False
    filters = xobj.get("/Filter")
    chain = []
    if filters is not None:
        filters = filters.get_object()
        chain = [str(f) for f in filters] if isinstance(filters, list) else [str(filters)]
        if chain:
            if chain[-1] not in _SHOWCASE_LAST_FILTERS:
                return False
            if any(f not in _STREAM_ONLY_FILTERS for f in chain[:-1]):
                return False
    smask = xobj.get("/SMask")
    if smask is not None:
        # pypdf gives a JPEG with a soft mask back as JPEG 2000, which the
        # app doesn't accept: skipped before the decode and the round trip
        # through OpenJPEG.
        if chain and chain[-1] == "/DCTDecode":
            return False
        smask = smask.get_object()
        if not hasattr(smask, "get") or not _decode_allowed(smask, max_pixels, depth + 1):
            return False
    return True


def _image_xobjects(resources_owner, depth: int, seen: set):
    """Yield the image XObjects in a page's (or form's) resources,
    descending into Form XObjects. Reads dictionaries only; nothing is
    decoded here."""
    if depth > _MAX_FORM_DEPTH:
        return
    try:
        resources = resources_owner.get("/Resources")
        xobjects = resources.get_object().get("/XObject") if resources is not None else None
        xobjects = xobjects.get_object() if xobjects is not None else None
        items = list(xobjects.values()) if hasattr(xobjects, "values") else []
    except Exception:
        log.debug("_image_xobjects: unreadable resources, skipping", exc_info=True)
        return
    for ref in items:
        try:
            xobj = ref.get_object()
            key = getattr(ref, "idnum", None) or id(xobj)
            if key in seen:
                continue
            seen.add(key)
            subtype = xobj.get("/Subtype")
        except Exception:
            log.debug("_image_xobjects: unreadable XObject, skipping", exc_info=True)
            continue
        if subtype == "/Image":
            yield xobj
        elif subtype == "/Form":
            yield from _image_xobjects(xobj, depth + 1, seen)


def _declared_size(xobj) -> tuple[int, int] | None:
    try:
        width, height = xobj.get("/Width"), xobj.get("/Height")
        width, height = int(width), int(height)
    except Exception:
        log.debug("_declared_size: no usable /Width or /Height", exc_info=True)
        return None
    return (width, height) if width > 0 and height > 0 else None


def extract_largest_embedded_image(pdf_path: str) -> bytes | None:
    """Best-effort extraction of the largest embedded raster image across
    the PDF's pages, as a heuristic for "the recipe's featured photo" --
    a real thumbnail beats a generic placeholder for PDF-ingested recipes.

    Sizes come from each image's /Width and /Height, so only the image
    that is picked gets decoded, and only if it and its soft mask use the
    filters in _SHOWCASE_LAST_FILTERS (see there) and declare at most
    the app's 50 MP limit. pypdf's page.images is not used:
    it decodes inline images while listing the page's images.

    Returns raw image bytes, still unvalidated -- the caller must run
    these through the same magic-byte-check + re-encode pipeline as any
    other image before trusting them; embedded PDF images are exactly as
    untrusted as the PDF itself. Returns None if nothing suitable is
    found or the PDF can't be parsed for images at all (never raises --
    a failed thumbnail extraction should never fail the whole ingestion).
    """
    try:
        from pypdf import PdfReader
        import io
    except Exception:
        log.debug("extract_largest_embedded_image: caught error, continuing", exc_info=True)
        return None

    try:
        reader = PdfReader(pdf_path)
        pages = list(reader.pages)[:MAX_PDF_PAGES]
    except Exception:
        log.debug("extract_largest_embedded_image: caught error, continuing", exc_info=True)
        return None

    from ..file_validation import MAX_DECODED_PIXELS
    max_pixels = MAX_DECODED_PIXELS
    candidates = []
    seen: set = set()
    for page in pages:
        try:
            for xobj in _image_xobjects(page, 0, seen):
                size = _declared_size(xobj)
                if size is None:
                    continue
                width, height = size
                if width < MIN_SHOWCASE_IMAGE_DIMENSION or height < MIN_SHOWCASE_IMAGE_DIMENSION:
                    continue
                if max_pixels and width * height > max_pixels:
                    continue
                if not _decode_allowed(xobj, max_pixels):
                    continue
                candidates.append((width * height, len(candidates), xobj))
        except Exception:
            log.debug("extract_largest_embedded_image: caught error, continuing", exc_info=True)
            continue

    candidates.sort(key=lambda c: (-c[0], c[1]))
    for _, _, xobj in candidates[:_MAX_DECODE_ATTEMPTS]:
        try:
            img = xobj.decode_as_image()
            if img is None:
                continue
            # pypdf re-encodes what it decoded: JPEG for a DCTDecode image
            # (quality kept), PNG otherwise. Any other format means the
            # decode took a path the filter check doesn't expect.
            if img.format not in ("JPEG", "PNG"):
                continue
            if img.width < MIN_SHOWCASE_IMAGE_DIMENSION or img.height < MIN_SHOWCASE_IMAGE_DIMENSION:
                continue
            out = io.BytesIO()
            if img.format == "JPEG":
                img.save(out, format="JPEG", quality="keep")
            else:
                img.save(out, format="PNG")
            return out.getvalue()
        except Exception:
            log.debug("extract_largest_embedded_image: caught error, continuing", exc_info=True)
            continue
    return None


class PdfIngestResult:
    def __init__(self, raw_text: str, ocr_used_on_pages: list[int], avg_ocr_confidence, page_count: int = 0):
        self.raw_text = raw_text
        self.ocr_used_on_pages = ocr_used_on_pages
        self.page_count = page_count
        # None if no OCR was needed at all (pure text-layer extraction)
        self.avg_ocr_confidence = avg_ocr_confidence


def extract_pdf_text(pdf_path: str) -> PdfIngestResult:
    """
    Extract text page-by-page. Pages with a usable embedded text layer are read
    directly (fast, accurate, no OCR). Pages with no/negligible text layer
    (scanned images) are rendered and passed through Tesseract OCR individually.
    Mixed documents (some real-text pages, some scanned pages) are handled
    per-page rather than as an all-or-nothing decision.
    """
    page_texts = []
    ocr_pages = []
    ocr_confidences = []
    started = time.monotonic()

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        if len(pdf.pages) > MAX_PDF_PAGES:
            raise PdfTooLargeError(
                f"PDF has {len(pdf.pages)} pages (limit {MAX_PDF_PAGES}) -- "
                "too large to process. Try splitting it first."
            )
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if len(text.strip()) >= MIN_CHARS_PER_PAGE:
                page_texts.append(text)
                continue

            # No usable text layer on this page -- render to image and OCR it.
            if ocr_pages and time.monotonic() - started > MAX_PDF_OCR_SECONDS:
                raise PdfTooLargeError(
                    f"Reading this PDF's scanned pages took more than {MAX_PDF_OCR_SECONDS // 60} minutes "
                    f"(stopped before page {i + 1}). Try splitting it."
                )
            ocr_pages.append(i + 1)
            pil_image = page.to_image(resolution=_ocr_resolution(page.width, page.height)).original
            ocr_text, confidence = _ocr_image(pil_image)
            page_texts.append(ocr_text)
            if confidence is not None:
                ocr_confidences.append(confidence)

    avg_confidence = (
        sum(ocr_confidences) / len(ocr_confidences) if ocr_confidences else None
    )

    return PdfIngestResult(
        raw_text="\n\n".join(page_texts),
        ocr_used_on_pages=ocr_pages,
        avg_ocr_confidence=avg_confidence,
        page_count=page_count,
    )
