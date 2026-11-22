"""Reading a PDF: its text layer page by page (a page without one goes to
OCR) and its largest embedded image. Split out of pdf_ingest.py (0186)."""
import time
import zlib

import pdfplumber
from pdfminer.pdfdevice import PDFTextDevice
from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager

from ..logging_setup import get_logger
# Private to ocr, but this module's to use: rendering a scanned page at the
# OCR budget and reading it are one job split across the two (0186).
from .ocr import MAX_IMPORT_OCR_SECONDS, OcrTimeUp, _ocr_image, _ocr_resolution, end_deadline, start_deadline

log = get_logger("pdf")

MIN_CHARS_PER_PAGE = 20
MAX_PDF_PAGES = 60  # generous for even a multi-recipe scanned chapter; bounds worst-case OCR time


# OCR time for one PDF, all its scanned pages together. Each Tesseract run
# has its own limit (OCR_TIMEOUT_SECONDS, up to two runs a page), so 60
# scanned pages could hold an OCR slot for hours (GPT review of 0190). A
# recipe's few pages take seconds each. Each run is cut to what is left
# (ocr.start_deadline), so a page under way stops at it too (GPT review of
# 0204).
MAX_PDF_OCR_SECONDS = MAX_IMPORT_OCR_SECONDS


class PdfTooLargeError(ValueError):
    pass


# A page's text layer, bounded before pdfplumber reads it (GPT review of
# 0221). pdfplumber builds a layout object for every character, about
# 3 KB each, and a compressed content stream is tiny for its text: a
# 1.6 KB PDF of 1,000,000 characters took 28 s and 2.2 GB. A dense recipe
# page holds a few thousand characters. Each page's streams pdfminer will
# decode are measured first with pypdf: content, form XObjects, embedded
# font files and Type 3 glyphs (MAX_PDF_PAGE_CONTENT_BYTES), and the fonts'
# /ToUnicode maps, which pdfminer parses into a table before any text is
# drawn (MAX_PDF_PAGE_CMAP_BYTES: a 10 KB PDF with a 10 MB map took 120 MB;
# GPT review of 0222). Flate streams are inflated only as far as the limit;
# other filters go through pypdf, whose decoders stop at 75 MB. Then the
# characters are counted by pdfminer with a device that keeps nothing,
# stopping at the limit.
MAX_PDF_PAGE_CHARS = 30_000
MAX_PDF_TEXT_CHARS = 400_000          # all pages; segment reads no more (SEGMENT_MAX_CHARS)
MAX_PDF_PAGE_CONTENT_BYTES = 20 * 1024 * 1024
MAX_PDF_PAGE_CMAP_BYTES = 4 * 1024 * 1024


class _TooMuchText(Exception):
    pass


class _CharCounter(PDFTextDevice):
    def __init__(self, rsrcmgr, limit: int):
        super().__init__(rsrcmgr)
        self.count = 0
        self.limit = limit

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate) -> float:
        self.count += 1
        if self.count > self.limit:
            raise _TooMuchText
        return 0.0


class _PageTooBig(Exception):
    pass


def _inflate(data: bytes, limit: int) -> bytes:
    """Flate, at most limit + 1 bytes out. Data that stops inflating part
    way gives what came out before the error, as pdfminer's own recovery
    does (GPT review of 0227: an error was measured as nothing while
    pdfminer recovered the whole stream). In the chunk with the error, the
    longest prefix that inflates is found by halving, from a copy taken
    before the chunk: a few passes, not one per byte (DeepSeek review of
    0228: a byte at a time could cost seconds over many broken streams)."""
    out = bytearray()
    inflater = zlib.decompressobj()
    step = 16 * 1024

    def inflates(state, part):
        try:
            state.copy().decompress(part)
            return True
        except zlib.error:
            return False

    for start in range(0, len(data), step):
        chunk = data[start:start + step]
        before = inflater.copy()
        try:
            out += inflater.decompress(chunk, limit + 1 - len(out))
        except zlib.error:
            lo, hi = 0, len(chunk) - 1           # chunk[:lo] inflates; chunk[:hi + 1] doesn't
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if inflates(before, chunk[:mid]):
                    lo = mid
                else:
                    hi = mid - 1
            out += before.decompress(chunk[:lo], limit + 1 - len(out))
            log.debug("_inflate: Flate data broken after %d bytes out", len(out))
            return bytes(out[:limit + 1])
        if len(out) > limit or inflater.unconsumed_tail:
            return bytes(out)
        if inflater.eof:
            break
    return bytes(out)


def _run_length(data: bytes, limit: int) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data) and len(out) <= limit:
        n = data[i]
        if n == 128:
            break
        if n < 128:
            out += data[i + 1:i + n + 2]
            i += n + 2
        else:
            out += data[i + 1:i + 2] * (257 - n)
            i += 2
    return bytes(out[:limit + 1])


def _lzw(data: bytes, limit: int) -> bytes:
    """PDF LZW, at most limit + 1 bytes out, step for step as pdfminer's
    LZWDecoder: an end-of-data code is passed over, not the end (Claude
    review of 0228: stopping there measured "AB" where pdfminer reads
    "ABCD"), and a code it can't read ends it."""
    out = bytearray()
    table: list = [bytes([n]) for n in range(256)] + [None, None]
    width, prev, buf, nbits = 9, b"", 0, 0
    for byte in data:
        buf = (buf << 8) | byte
        nbits += 8
        while nbits >= width:
            nbits -= width
            code = (buf >> nbits) & ((1 << width) - 1)
            buf &= (1 << nbits) - 1
            if code == 256:
                table, width, prev = table[:258], 9, b""
                continue
            if code == 257:
                continue
            if not prev:
                if code >= len(table) or table[code] is None:
                    return bytes(out)
                entry = prev = table[code]
            else:
                if code < len(table) and table[code] is not None:
                    entry = table[code]
                    table.append(prev + entry[:1])
                elif code == len(table):
                    entry = prev + prev[:1]
                    table.append(entry)
                else:
                    return bytes(out)
                if len(table) in (511, 1023, 2047):
                    width = {511: 10, 1023: 11, 2047: 12}[len(table)]
                prev = entry
            out += entry
            if len(out) > limit:
                return bytes(out[:limit + 1])
    return bytes(out)


def _ascii85_size(data: bytes) -> int:
    """What ASCII85 decodes to, counted without decoding: four bytes a
    group of five, four for each "z" (Claude review of 0228: one "z" is
    four zero bytes, so the output can outgrow the input)."""
    data = data.split(b"~>")[0].translate(None, b" \t\r\n\x00\x0c")
    if data.startswith(b"<~"):
        data = data[2:]
    zs = data.count(b"z")
    rest = len(data) - zs
    return 4 * zs + 4 * (rest // 5) + max(0, rest % 5 - 1)


def _asciihex_size(data: bytes) -> int:
    data = data.split(b">")[0].translate(None, b" \t\r\n\x00\x0c")
    return (len(data) + 1) // 2


def _ccitt_bound(data: bytes, params) -> int:
    """At most what pdfminer's CCITT decoder can write: a row of
    (Columns + 7) // 8 bytes for every bit of input, a row taking at least
    one (GPT review of 0228: the stream was counted at its compressed size,
    1 KB that pdfminer turned into 1.77 MB). An upper bound: CCITT belongs
    in scanned images, which aren't measured here, not in text or fonts."""
    columns = 1728
    try:
        get = getattr(params, "get", None)
        columns = int(get("/Columns", 1728)) if get is not None else 1728
    except (TypeError, ValueError):
        log.debug("_ccitt_bound: unreadable /Columns", exc_info=True)
    return 8 * len(data) * ((max(columns, 1) + 7) // 8)


class _Unmeasurable(Exception):
    pass


_PASSED_THROUGH = {"/DCTDecode", "/DCT", "/JBIG2Decode", "/JPXDecode"}   # pdfminer keeps these as they are


def _decoded_size(stream, limit: int) -> int:
    """A pypdf stream's decoded length, or more than limit once it's past
    limit, decoding no further. Each filter pdfminer decodes is handled
    here (GPT review of 0228: the list is pdfminer's, not a guess): Flate,
    LZW and run-length decoded with the same bound, so no stage holds more
    than limit + 1 bytes; ASCII85 and ASCIIHex counted before they're
    decoded, and decoded only within the limit; CCITT by an upper bound.
    pdfminer leaves DCT, JBIG2 and JPX as they are, so the measure stops
    there. Anything else (Crypt, unknown names) raises _Unmeasurable: the
    page is refused rather than counted at its compressed size."""
    # stream._data is pypdf's private copy of the raw bytes: the only way to
    # decode them part way (get_data() decodes all). Renamed some day, the
    # AttributeError refuses the PDF as damaged, never skips the limit.
    from pdfminer.ascii85 import ascii85decode, asciihexdecode
    data = stream._data
    filters = stream.get("/Filter")
    names = [] if filters is None else [str(f) for f in filters] if isinstance(filters, list) else [str(filters)]
    params = stream.get("/DecodeParms")
    params = list(params) if isinstance(params, list) else [params] * len(names)
    for n, name in enumerate(names):
        if name in ("/FlateDecode", "/Fl"):
            data = _inflate(data, limit)
        elif name in ("/LZWDecode", "/LZW"):
            data = _lzw(data, limit)
        elif name in ("/RunLengthDecode", "/RL"):
            data = _run_length(data, limit)
        elif name in ("/ASCII85Decode", "/A85"):
            if _ascii85_size(data) > limit:
                return limit + 1
            data = ascii85decode(data)
        elif name in ("/ASCIIHexDecode", "/AHx"):
            if _asciihex_size(data) > limit:
                return limit + 1
            data = asciihexdecode(data)
        elif name in ("/CCITTFaxDecode", "/CCF"):
            p = params[n] if n < len(params) else None
            return min(_ccitt_bound(data, p.get_object() if p is not None else None), limit + 1)
        elif name in _PASSED_THROUGH:
            break
        else:
            raise _Unmeasurable(name)
        if len(data) > limit:
            break
    return len(data)


def _page_streams(page, sizes: dict) -> tuple[int, int]:
    """(content bytes, ToUnicode bytes) decoded for a pypdf page: its
    content, and through its resources (and those of its forms and Type 3
    fonts) each form XObject, embedded font file, Type 3 glyph and
    ToUnicode map, each counted once a page. Each stream is decoded only
    as far as what's left of the page's limit, and measuring stops as soon
    as a total is past it (GPT review of 0227: every stream was decoded,
    each up to the whole limit, before the totals were checked). sizes
    caches a stream's exact size by object number across pages."""
    totals = {"content": 0, "cmaps": 0}
    limits = {"content": MAX_PDF_PAGE_CONTENT_BYTES, "cmaps": MAX_PDF_PAGE_CMAP_BYTES}
    counted: set = set()

    def measure(ref, kind):
        # Once a page: two fonts sharing one font file decode it once
        # (review of 0222/0223: it was counted twice).
        key = getattr(ref, "idnum", None)
        if key is not None and key in counted:
            return
        if key is not None:
            counted.add(key)
        left = limits[kind] - totals[kind]
        if key is not None and key in sizes:
            size = sizes[key]
        else:
            size = _decoded_size(ref.get_object(), left)
            if key is not None and size <= left:
                sizes[key] = size                # exact; a cut-off size isn't kept
        totals[kind] += size
        if totals[kind] > limits[kind]:
            raise _PageTooBig

    try:
        ref = page.raw_get("/Contents") if "/Contents" in page else None
        obj = ref.get_object() if ref is not None else None
        for item in (list(obj) if isinstance(obj, list) else [ref] if ref is not None else []):
            measure(item, "content")
        seen = set()
        todo = [page.get("/Resources")]
        while todo:
            res = todo.pop()
            res = res.get_object() if res is not None else None
            if not hasattr(res, "get"):
                continue
            xobjects = res.get("/XObject")
            for ref in (xobjects.get_object().values() if xobjects is not None else []):
                key = getattr(ref, "idnum", None) or id(ref)
                if key in seen:
                    continue
                seen.add(key)
                obj = ref.get_object()
                if obj.get("/Subtype") == "/Form":
                    measure(ref, "content")
                    todo.append(obj.get("/Resources"))
            fonts = res.get("/Font")
            for ref in (fonts.get_object().values() if fonts is not None else []):
                key = getattr(ref, "idnum", None) or id(ref)
                if key in seen:
                    continue
                seen.add(key)
                font = ref.get_object()
                if font.get("/ToUnicode") is not None and hasattr(font["/ToUnicode"].get_object(), "get_data"):
                    measure(font.raw_get("/ToUnicode"), "cmaps")
                descriptors = [font.get("/FontDescriptor")]
                for d in (font.get("/DescendantFonts") or []):
                    descriptors.append(d.get_object().get("/FontDescriptor"))
                for desc in descriptors:
                    desc = desc.get_object() if desc is not None else None
                    for name in ("/FontFile", "/FontFile2", "/FontFile3"):
                        if desc is not None and desc.get(name) is not None:
                            measure(desc.raw_get(name), "content")
                for glyph in (font.get("/CharProcs").get_object().values()
                              if font.get("/CharProcs") is not None else []):
                    measure(glyph, "content")
                if font.get("/Resources") is not None:
                    todo.append(font.get("/Resources"))
    except _PageTooBig:
        log.debug("_page_streams: stopped measuring past a limit: %s", totals)
    return totals["content"], totals["cmaps"]


def _check_text_size(pdf_path: str, pdf) -> None:
    """PdfTooLargeError for a page whose text layer is past the limits,
    before pdfplumber lays any of it out. A PDF pypdf can't measure is
    refused too, page by page (GPT review of 0222: it used to turn every
    size limit off)."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(pdf_path)
        pages = list(reader.pages)
    except Exception as e:
        log.info("_check_text_size: pypdf can't open this PDF", exc_info=True)
        raise PdfTooLargeError("This PDF is damaged and can't be checked before it's read. "
                               "Try printing it to a new PDF.") from e
    sizes: dict = {}
    for i, page in enumerate(pages):
        try:
            content, cmaps = _page_streams(page, sizes)
        except _Unmeasurable as e:
            log.info("_check_text_size: page %d has a stream in %s", i + 1, e)
            raise PdfTooLargeError(f"Page {i + 1} of this PDF uses a kind of compression that can't be checked "
                                   "before it's read. Try printing it to a new PDF.") from e
        except Exception as e:
            log.info("_check_text_size: page %d can't be measured", i + 1, exc_info=True)
            raise PdfTooLargeError(f"Page {i + 1} of this PDF is damaged and can't be checked before it's read. "
                                   "Try printing it to a new PDF.") from e
        if content > MAX_PDF_PAGE_CONTENT_BYTES or cmaps > MAX_PDF_PAGE_CMAP_BYTES:
            log.warning("PDF page %d: %d bytes of content, %d of character maps", i + 1, content, cmaps)
            # The number and the name from the same measure, the one past its
            # limit (content first when both are): max() of the two named
            # the wrong one when the maps were over and the content larger
            # but under its own limit (review of 0224). The number is a lower
            # bound: a stream is inflated only to just past its limit.
            if content > MAX_PDF_PAGE_CONTENT_BYTES:
                size, what = content, "page content and fonts"
            else:
                size, what = cmaps, "font character maps"
            raise PdfTooLargeError(
                f"Page {i + 1} of this PDF holds far more than a recipe "
                f"({size // (1024 * 1024)} MB of {what}). Try a PDF of just the recipe, or a screenshot.")
    rsrcmgr = PDFResourceManager(caching=True)
    total = 0
    for i, page in enumerate(pdf.pages):
        counter = _CharCounter(rsrcmgr, MAX_PDF_PAGE_CHARS)
        try:
            PDFPageInterpreter(rsrcmgr, counter).process_page(page.page_obj)
        except _TooMuchText:
            log.warning("PDF page %d: over %d characters of text", i + 1, MAX_PDF_PAGE_CHARS)
            raise PdfTooLargeError(
                f"Page {i + 1} of this PDF holds more text than a recipe page (over {MAX_PDF_PAGE_CHARS:,} "
                "characters). Try a PDF of just the recipe.") from None
        finally:
            counter.close()
        total += counter.count
        if total > MAX_PDF_TEXT_CHARS:
            log.warning("PDF: over %d characters of text by page %d", MAX_PDF_TEXT_CHARS, i + 1)
            raise PdfTooLargeError(
                f"This PDF holds more text than a recipe (over {MAX_PDF_TEXT_CHARS:,} characters by page {i + 1}). "
                "Try a PDF of just the recipe.")


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


def _ocr_too_long(page: int, started: bool) -> PdfTooLargeError:
    """started: the time ran out while reading this page ("at"), or before
    it began ("before"), so the pages read are clear (review of 0205)."""
    where = "at" if started else "before"
    return PdfTooLargeError(
        f"Reading this PDF's scanned pages took more than {int(MAX_PDF_OCR_SECONDS // 60)} minutes "
        f"(stopped {where} page {page}). Try splitting it.")


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
    ocr_started = None   # at the first scanned page: the budget is OCR time
    clock = None         # its token (ocr.start_deadline), ended below

    try:
        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            if len(pdf.pages) > MAX_PDF_PAGES:
                raise PdfTooLargeError(
                    f"PDF has {len(pdf.pages)} pages (limit {MAX_PDF_PAGES}) -- "
                    "too large to process. Try splitting it first."
                )
            _check_text_size(pdf_path, pdf)
            for i, page in enumerate(pdf.pages):
                try:
                    text = page.extract_text() or ""
                    if len(text.strip()) >= MIN_CHARS_PER_PAGE:
                        page_texts.append(text)
                        continue

                    # No usable text layer on this page -- render to image and OCR it.
                    # The clock starts at the first scanned page; checked again before
                    # rendering each later one, and every Tesseract run stops at it.
                    if ocr_started is None:
                        ocr_started = time.monotonic()
                        clock = start_deadline(MAX_PDF_OCR_SECONDS, now=True)
                    elif time.monotonic() - ocr_started > MAX_PDF_OCR_SECONDS:
                        raise _ocr_too_long(i + 1, started=False)
                    ocr_pages.append(i + 1)
                    pil_image = page.to_image(resolution=_ocr_resolution(page.width, page.height)).original
                    try:
                        ocr_text, confidence = _ocr_image(pil_image)
                    except OcrTimeUp:
                        log.debug("extract_pdf_text: OCR time ran out on page %d", i + 1, exc_info=True)
                        raise _ocr_too_long(i + 1, started=True) from None
                    page_texts.append(ocr_text)
                    if confidence is not None:
                        ocr_confidences.append(confidence)
                finally:
                    page.close()     # its layout objects; pdfplumber keeps them otherwise
    finally:
        if clock is not None:
            end_deadline(clock)

    avg_confidence = (
        sum(ocr_confidences) / len(ocr_confidences) if ocr_confidences else None
    )

    return PdfIngestResult(
        raw_text="\n\n".join(page_texts),
        ocr_used_on_pages=ocr_pages,
        avg_ocr_confidence=avg_confidence,
        page_count=page_count,
    )
