import re
import numpy as np
import pdfplumber
import pytesseract
from PIL import Image

from .deskew import deskew_grayscale, auto_orient
from ..logging_setup import get_logger

log = get_logger("pdf")

MIN_CHARS_PER_PAGE = 20
MAX_PDF_PAGES = 60  # generous for even a multi-recipe scanned chapter; bounds worst-case OCR time


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


# Rendering budget for one scanned page. 300 dpi on a letter page is
# ~8.4 MP; an 8.5 x 200 inch page (the iOS full-page-screenshot maximum)
# was 153 MP and 1.25 GB before OCR even started, with up to three OCR jobs
# at once. Large pages get a lower dpi instead.
MAX_OCR_PIXELS = 35_000_000
OCR_DPI = 300


def _ocr_resolution(width_pt: float, height_pt: float) -> int:
    """dpi for rendering a page of this size (in points) within the budget.

    No minimum dpi: a floor of 72 let a 200 x 200 inch page render at
    207 MP (1.7 GB peak) before anything could shrink it. A page that big
    comes out at under 72 dpi and probably OCRs badly, which beats the
    memory it would otherwise take."""
    area_in2 = max(width_pt, 1) / 72 * max(height_pt, 1) / 72
    dpi = int((MAX_OCR_PIXELS / area_in2) ** 0.5)
    return max(1, min(OCR_DPI, dpi))


def _preprocess_for_ocr(pil_image: Image.Image) -> Image.Image:
    """Grayscale + deskew + upscale to improve Tesseract accuracy on
    borderline images, including scans fed in at a slight angle.

    The upscale-to-1500px-wide step below is unconditional on its own --
    for an extreme aspect ratio (a 12 x 60000 px render of a 3 x 14400 pt
    page) it multiplies out to billions of pixels even though the render
    itself stayed inside MAX_OCR_PIXELS. An image already over the budget
    is scaled down instead (the render is sized to fit, so this is a
    safety net)."""
    oriented, _ = auto_orient(pil_image.convert("L"))
    gray_arr = deskew_grayscale(np.array(oriented))
    gray = Image.fromarray(gray_arr)

    scale = 1500 / gray.width if gray.width < 1500 else 1.0
    current_pixels = gray.width * gray.height
    if current_pixels * scale * scale > MAX_OCR_PIXELS:
        scale = (MAX_OCR_PIXELS / current_pixels) ** 0.5
    if scale != 1.0:
        new_w = max(1, int(gray.width * scale))
        new_h = max(1, int(gray.height * scale))
        gray = gray.resize((new_w, new_h), Image.LANCZOS)
    return gray


_ocr_lang_cache = None


def ocr_lang() -> str:
    """"eng+fra" when the French language data is installed (it is in the
    Docker image), so accented French reads as French instead of being
    guessed into English letters; "eng" otherwise."""
    global _ocr_lang_cache
    if _ocr_lang_cache is None:
        try:
            langs = set(pytesseract.get_languages(config=""))
        except Exception:
            log.debug("ocr_lang: couldn't list languages", exc_info=True)
            langs = set()
        _ocr_lang_cache = "eng+fra" if "fra" in langs else "eng"
    return _ocr_lang_cache


def _ocr_image(pil_image: Image.Image):
    # A zero-width or zero-height render (a degenerate page box) has no text
    # and would divide by zero in the upscale below.
    if not pil_image.width or not pil_image.height:
        return "", None
    processed = _preprocess_for_ocr(pil_image)
    text = pytesseract.image_to_string(processed, lang=ocr_lang())

    # Per-word confidence average, used to drive the "OCR quality: low" badge.
    try:
        data = pytesseract.image_to_data(processed, lang=ocr_lang(), output_type=pytesseract.Output.DICT)
        confidences = [int(c) for c in data["conf"] if c not in ("-1", -1)]
        avg_conf = sum(confidences) / len(confidences) if confidences else None
    except Exception:
        log.warning("_ocr_image: caught error, continuing", exc_info=True)
        avg_conf = None

    return text, avg_conf


# "Ingredients" alone on a line, optionally followed by a recipe card's
# serving-size buttons ("Ingredients 1X 2X 3X") or a count, as grocery and
# recipe apps show it ("Ingredients 13 total", "Method 2 steps"). Without
# the count, a screenshot's steps were never found.
# French too: "Ingrédients", "Préparation", "Étapes", "Méthode",
# "Mode de préparation", "Marche à suivre" (accents optional: OCR drops them).
_HEADING_COUNT = r"(?:\(?\d+\)?\s*(?:total|items?|steps?|ingredients?|[ée]tapes?|ingr[ée]dients?)?)?"
HEADING_INGREDIENTS = re.compile(
    r"^\s*ingr[eé]dients?\s*:?\s*(?:\d+(?:\.\d+)?\s*[x×]\s*)*" + _HEADING_COUNT + r"\s*$",
    re.IGNORECASE | re.MULTILINE
)
HEADING_STEPS = re.compile(
    # (?!step N): "Step 1" / "Étape 1" is a step's label, not the section heading.
    r"^(?!\s*(?:step|[ée]tape)\s*\d)\s*(instructions?|directions?|method|steps?|preparation|"
    r"pr[ée]paration|[ée]tapes?|m[ée]thode|mode\s+de\s+pr[ée]paration|marche\s+[àa]\s+suivre|directives?)"
    r"\s*:?\s*" + _HEADING_COUNT + r"\s*$",
    re.IGNORECASE | re.MULTILINE
)
NUMBERED_STEP = re.compile(r"^\s*\d+[\.\)]\s+")
QUANTITY_LEAD = re.compile(
    r"^\s*\d+[\d/\.\s]*\s*(cups?|tbsp|tsp|g|kg|oz|lb|ml|l|pinch|clove)?\b", re.IGNORECASE
)
# Anything starting with a number or a vulgar fraction: "1 ¼ teaspoons",
# "½ cup". Used to tell a new ingredient from a wrapped continuation line.
_QTY_START = re.compile(r"^\s*(\d|[¼½¾⅓⅔⅛⅜⅝⅞])")
# A step marker: "1." / "1)" / "1 " followed by a capital, or a bare number
# on its own line (some recipe cards put the number in a separate column).
# Capital letters include accented ones: French steps start "Égoutter".
_UPPER = "A-ZÀ-ÖØ-Þ"
_STEP_START = re.compile(rf"^\s*(?:[Ss]tep\s*|[ÉéEe]tape\s*)?(\d{{1,2}})(?:[\.\):]\s*|\s+)(?=[{_UPPER}])")
# A step number alone on its line: "3", or an app's "Step 3" / "Étape 3" label.
_BARE_NUMBER = re.compile(r"^\s*(?:step\s*|[ée]tape\s*)?\d{1,2}\s*:?\s*$", re.IGNORECASE)

# Lines that are page furniture, not recipe content. Printed web pages carry
# the browser's date/time and "Page 1 of 3"; recipe cards carry serving-size
# and unit toggles.
_JUNK_LINE = re.compile(
    r"^\s*("
    r"\d{4}-\d{2}-\d{2},?\s+\d{1,2}:\d{2}(\s*[AP]M)?"         # 2026-09-25, 1:44 PM
    r"|\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}(\s*[AP]M)?"  # 9/25/26, 1:44 PM
    r"|page \d+ of \d+"
    r"|[LJIl|\[\]_]{1,3}"                                     # an OCR'd empty checkbox: "LJ", "L"
    r"|(?:\S+\s+){0,4}[\w-]+\.(?:com|ca|net|org|co\.uk|io)"      # a site footer: "AA & a recipetineats.com"
    r"|\d{1,2}:\d{2}(\s+\S{1,3}){0,5}"                           # phone status bar: "7:38 a 7 @®"
    r"|https?://\S+"
    r"|(\d+(\.\d+)?\s*[x×]\s*)+"                               # 1X 2X 3X
    r"|us customary(\s+metric)?|metric"
    r"|(save|pin|print|rate|share|jump to recipe)(\s+(save|pin|print|rate|recipe|share))*\s*↓?"
    r")\s*$",
    re.IGNORECASE,
)
# Where the steps end on a recipe card or printed page.
_STEPS_END = re.compile(
    r"^\s*(notes?|recipe notes|nutrition(\s+information|\s+facts)?|video|equipment|"
    r"did you make this.*|course|cuisine|keyword|author|"
    r"(ratings?|reviews?|comments?)(\s*\(\d+\))?|\d+\s+(ratings?|reviews?|comments?)|"
    # French: "Notes" is the same word; "Valeur nutritive", "Commentaires (12)", "Avis".
    r"valeurs?\s+nutritives?|informations?\s+nutritionnelles?|astuces?|conseils?|"
    r"(commentaires?|avis|[ée]valuations?)(\s*\(\d+\))?)\s*:?\s*$",
    re.IGNORECASE,
)
_BYLINE = re.compile(r"^\s*by[:\s]", re.IGNORECASE)
_BREADCRUMB = re.compile(r"\S\s+/\s+\S.*\s+/\s+\S")


def _is_junk(line: str) -> bool:
    return bool(_JUNK_LINE.match(line))


_SECTION_STARTS = ("for the ", "pour la ", "pour le ", "pour les ", "pour l'", "pour l\u2019")


def _looks_like_section_label(line: str) -> bool:
    """"PEPPERED BACON CURE", "For the sauce:", "Topping" -- a sub-heading
    inside an ingredient list, not an ingredient."""
    if _QTY_START.match(line) or len(line.split()) > 6:
        return False
    low = line.lower()
    return line.isupper() or line.rstrip().endswith(":") or low.startswith(_SECTION_STARTS)


def _clean_ingredients(lines):
    """Drop page furniture, join wrapped lines, and label sub-headings.

    A line that doesn't start with a quantity is a continuation of the one
    before if that one left a bracket open or this one starts in lowercase:
    "1 ¼ teaspoons Prague powder #1 (curing" + "salt)". Sub-headings are
    kept -- dropping them loses which ingredients belong to which part --
    but reworded so they read as a heading, not as an ingredient.
    """
    out = []
    for line in lines:
        if _is_junk(line):
            continue
        m = _LABELLED_QTY.match(line)
        if m:
            # "Noodles: 8 oz wheat noodles": the label repeats the
            # ingredient and goes. "For the dough: 2 cups flour": the label
            # is a section, kept on its own line (DeepSeek review of 0073:
            # it was dropped with the other kind).
            if m.group(1).lower().startswith(_SECTION_STARTS):
                out.append(f"{m.group(1).strip()}:")
            line = line[m.end():]
        if _looks_like_section_label(line):
            label = line.rstrip(":").strip()
            if label.isupper() or label.istitle():
                label = label.lower()
            if not label.lower().startswith("for "):
                label = f"For the {label[0].lower() + label[1:]}"
            # "FOR THE BASE" came out as "for the base:".
            out.append(f"{label[0].upper()}{label[1:]}:")
            continue
        prev = out[-1] if out else None
        continues = prev is not None and not prev.endswith(":") and not _QTY_START.match(line) and (
            prev.count("(") > prev.count(")") or line[:1].islower() or line[:1] in ")],;"
        )
        if continues:
            out[-1] = f"{prev} {line}"
        else:
            out.append(line)
    return out


def _clean_steps(lines):
    """Rebuild whole steps from printed lines.

    Printed and OCR'd text has one line per line of the page, not one per
    step, so taking each line as a step split every instruction into
    fragments. If the block has step numbers, each number starts a step and
    everything up to the next number belongs to it. Without numbers, a line
    that ends a sentence ends the step. Leading numbers are removed -- the
    app numbers steps itself.
    """
    kept = []
    for line in lines:
        if _STEPS_END.match(line):
            break
        if not _is_junk(line):
            kept.append(line)

    numbered = sum(1 for l in kept if _STEP_START.match(l) or _BARE_NUMBER.match(l))
    steps = []
    if numbered >= 2 or (numbered == 1 and len(kept) > 1 and (_STEP_START.match(kept[0]) or _BARE_NUMBER.match(kept[0]))):
        for line in kept:
            if _BARE_NUMBER.match(line):
                steps.append("")
                continue
            m = _STEP_START.match(line)
            if m:
                steps.append(line[m.end():].strip())
            elif steps:
                steps[-1] = f"{steps[-1]} {line}".strip()
            else:
                steps.append(line)
    else:
        for line in kept:
            line = NUMBERED_STEP.sub("", line)
            if steps and not re.search(r"[.!?:)]\s*$", steps[-1]):
                steps[-1] = f"{steps[-1]} {line}"
            else:
                steps.append(line)
    return [s for s in steps if s]


def _guess_title(lines, ing_line_index):
    """The recipe's name, not whatever happens to be the first line.

    Printed web pages start with the date and "Page 1 of 1"; blog pages start
    with shipping banners and breadcrumbs. In order of preference:
      1. the line just above the author byline closest before the
         ingredients -- recipe cards put "Title" then "By: Name";
      2. a short line that appears more than once (the name is repeated in
         the article heading, the card, and often the print header);
      3. the first line that isn't page furniture.
    """
    def ok(l):
        return (not _is_junk(l) and not _BYLINE.match(l) and not _BREADCRUMB.search(l)
                and 2 <= len(l) <= 80 and not HEADING_INGREDIENTS.match(l) and not HEADING_STEPS.match(l)
                and not re.search(r"\$\d", l)
                # "Sel et poivre au goût" under two sub-recipes is repeated,
                # but it's an ingredient, not the name.
                and not _QTY_START.match(l) and not _INGREDIENT_TAIL.search(l) and not _YIELD.match(l))

    limit = ing_line_index if ing_line_index is not None else len(lines)
    for i in range(limit - 1, 0, -1):
        if _BYLINE.match(lines[i]) and ok(lines[i - 1]):
            return lines[i - 1]

    counts = {}
    for l in lines:
        if ok(l) and 1 <= len(l.split()) <= 8 and not l.endswith("."):
            counts[l.lower()] = counts.get(l.lower(), 0) + 1
    repeated = [l for l in lines if counts.get(l.lower(), 0) >= 2]
    if repeated:
        return repeated[0]

    for l in lines:
        if ok(l):
            return l
    return "Untitled Recipe"


_BULLET = re.compile(r"^\s*(?:[-•*·▪◦‣]\s+)+")
_EMPHASIS_TIGHT = re.compile(r"\*{1,2}([^*\n]+?)\*{1,2}(?=\S)")
_EMPHASIS = re.compile(r"\*{1,2}([^*\n]+?)\*{1,2}")
# "Noodles: 8 oz wheat noodles" -- a label in front of the quantity.
_LABELLED_QTY = re.compile(r"^([A-Z][\w &/'()-]{0,30}):\s+(?=[\d¼½¾⅓⅔⅛⅜⅝⅞])")


def _normalize_line(line: str) -> str:
    """Plain text written by mail apps: Gmail sends bold as *text* and list
    items as "   - item". Without this, "- 1 tsp soy sauce" doesn't start
    with a quantity and "*1.Cook the noodles:*" doesn't start with a step
    number, and neither is recognized."""
    line = _BULLET.sub("", line)
    line = _EMPHASIS_TIGHT.sub(r"\1 ", line)   # "*1.Cook:*Stops" -> "1.Cook: Stops"
    line = _EMPHASIS.sub(r"\1", line)
    # Tesseract's usual misread of "tbsp" (seen on a real screenshot).
    line = re.sub(r"\bt[o0]sp\b", "tbsp", line, flags=re.IGNORECASE)
    return line.strip()


# Leftovers of a step number drawn in a circle, as OCR reads it: "@ Add",
# "9 Cover", "© Skim", "faa) Serve", "3) Return".
_MARKER_GARBAGE = re.compile(rf"^\s*(?:[^\w\s]{{1,2}}|\w{{1,3}}\)|\d{{1,2}}[\.\)]?)\s+(?=[{_UPPER}])")

# How a method line usually starts (English and French, accents folded).
_STEP_VERBS = set("""
add adjust arrange bake beat blend boil bring brown brush chill chop coat combine cook cool cover crush cut
divide drain drizzle dust fill flip fold fry garnish grate grease grill heat knead lay let line lower marinate
mash melt mix pat peel place pour preheat prepare press put reduce refrigerate remove return rinse roast roll
rub season serve set shake simmer skim slice soak spoon spread sprinkle squeeze stir strain stuff taste
thread toast top toss transfer trim turn wash whisk wipe wrap in once when while meanwhile then using make
using bake saute sear steam
ajouter arroser assaisonner badigeonner battre beurrer chauffer couper couvrir cuire decouper deposer
dorer egoutter emincer enfourner etaler faire farcir fouetter garnir griller hacher incorporer laisser
laver mariner melanger mettre mijoter napper parsemer peler petrir placer poivrer porter prechauffer
preparer recouvrir reduire refroidir remettre remuer repartir reserver retirer rincer rouler saler
saupoudrer servir tailler trancher transferer verser dans pendant entre-temps ensuite puis lorsque quand
""".split())

# Ingredient lines with no quantity that read like a sentence: "Salt and
# pepper to taste", "Fresh herbs, for serving". They usually come straight
# after the last measured ingredient, where one would become step 1.
from ..text_rules import INGREDIENT_TAIL as _INGREDIENT_TAIL  # noqa: E402 (one copy, shared)


def _is_prose(line: str) -> bool:
    """A line of method text rather than an ingredient: several words that
    end a sentence, or start with a cooking verb. "2 large celery stalks"
    isn't; "Cut the beef into 5cm/2" chunks. Pat dry" is. A capital first
    letter alone isn't enough: "Salt and pepper to taste" would count."""
    words = line.split()
    if len(words) < 4 or _INGREDIENT_TAIL.search(line):
        return False
    stripped = _MARKER_GARBAGE.sub("", line).strip()
    if not stripped or _QTY_START.match(stripped):
        return False
    return bool(re.search(r"[.!?]\s*$", line)) or _first_word(stripped) in _STEP_VERBS


def _first_word(text: str) -> str:
    import unicodedata
    first = unicodedata.normalize("NFKD", text.split()[0].lower())
    return "".join(c for c in first if not unicodedata.combining(c)).strip(",.;:!")


def _short_step(line: str) -> bool:
    """A step too short for _is_prose ("Mix everything.", "Bake 20 min."):
    under four words, starting with a cooking verb, no amount in front.
    Only used where there are no headings, after the ingredients; the
    four-word floor stays for _is_prose itself, which also decides where
    ingredients end ("Top sirloin" mustn't become a step there). DeepSeek
    review of 0096: such a step was dropped and the recipe failed as
    having none."""
    stripped = _MARKER_GARBAGE.sub("", line).strip()
    if not stripped or len(stripped.split()) >= 4 or _QTY_START.match(stripped) or _INGREDIENT_TAIL.search(line):
        return False
    return _first_word(stripped) in _STEP_VERBS


def _prose_steps(lines):
    steps = []
    for line in lines:
        if _STEPS_END.match(line):
            break
        line = _MARKER_GARBAGE.sub("", line).strip()
        if not line:
            continue
        if steps and not re.search(r"[.!?]\s*$", steps[-1]):
            steps[-1] = f"{steps[-1]} {line}"
        else:
            steps.append(line)
    return steps


def prepare_lines(raw_text: str) -> list[str]:
    """The text as the segmenter reads it: one cleaned line per printed
    line, blank lines dropped. Shared with the learned segmenter (app/ml),
    which labels exactly these lines."""
    lines = [_normalize_line(l) for l in (raw_text or "").split("\n")]
    return [l for l in lines if l]


# Settings -> Learning: a model trained on the user's own corrected imports
# (app/ml/segmenter.py). Set by app/ml/service.py when one is in use; it
# gets the text and this module's own result, and returns its result or
# None to keep the rules' one.
_learned_segmenter = None


def set_learned_segmenter(fn):
    global _learned_segmenter
    _learned_segmenter = fn


def segment_raw_text(raw_text: str) -> dict:
    """Title, ingredients and steps from extracted or OCR'd text: the rules
    below, or the learned model when one is in use and it beat the rules
    on the user's own recipes. No LLM either way."""
    result = rule_segment_raw_text(raw_text)
    learned = _learned_segmenter
    if learned is not None:
        try:
            out = learned(raw_text, result)
            if out is not None:
                return out
        except Exception:
            log.exception("Learned segmenter failed; using the rules' result")
    return result


# "(Donne environ 10 croquettes)", "Makes 12 cookies", "Serves 4",
# "Portions : 6", "Rendement : 2 litres". The servings, not an ingredient.
_YIELD = re.compile(
    r"^\s*\(?\s*(?:donne|makes|serves|yield(?:s)?|rendement|portions?|servings?|pour(?=\s+\d+\s*(?:personnes?|portions?)))\s*:?\s+(.{1,60}?)\s*\)?\s*$",
    re.IGNORECASE)


def _yield_of(line: str) -> str | None:
    m = _YIELD.match(line)
    if not m or not re.search(r"\d", m.group(1)):
        return None
    return m.group(1).strip(" .")


def _is_paragraph_line(line: str) -> bool:
    """Method text in a paragraph, as a word processor wraps it: a long line
    with a sentence break inside, or many words, or the usual prose test."""
    words = line.split()
    if _QTY_START.match(line) and len(words) < 12:
        return False
    return ((len(line) >= 60 and (re.search(r"[.!?]\s+\S", line) is not None or len(words) >= 10))
            or _is_prose(line))


def _join_paragraph(lines):
    """Lines of one paragraph, joined; a word split at the line end
    ("Mille-" + "Îles") is put back together."""
    text = ""
    for line in lines:
        line = line.strip()
        if text.endswith("-") and len(text) > 1 and text[-2].isalpha() and line[:1].isalpha():
            text += line
        else:
            text = f"{text} {line}".strip()
    return text


def _component_recipe(body: list[str]):
    """A recipe in parts with no Ingredients/Method headings, as written in
    a word processor: [part name] ingredient lines, a method paragraph,
    then the next part ("Sauce tartare", its ingredients, "Mix
    everything."). Returns (ingredients, steps, servings), or None when the
    text isn't laid out that way (at least two parts, each a run of
    ingredients followed by method text)."""
    kinds = []
    servings = None
    for line in body:
        prev_kind, prev_line = (kinds[-1], body[len(kinds) - 1]) if kinds else (None, "")
        if _yield_of(line) and servings is None:
            servings = _yield_of(line)
            kinds.append("yield")
        elif prev_kind == "prose" and not re.search(r"[.!?:]\s*$", prev_line):
            kinds.append("prose")                     # the paragraph goes on
        elif _is_prose(line):                         # "9 Cook the garlic." is a circled step number
            kinds.append("prose")
        elif _QTY_START.match(line) or (_INGREDIENT_TAIL.search(line) and len(line.split()) <= 12):
            kinds.append("ing")
        elif _is_paragraph_line(line):
            kinds.append("prose")
        elif len(line.split()) <= 5 and not re.search(r"[.!?]\s*$", line) and (
                line.rstrip().endswith(":") or line.isupper() or prev_kind == "prose"
                or all(w[:1].isupper() for w in line.split() if len(w) > 2)):
            # "Panure :", "SAUCE", "Sauce Tartare", or any short line
            # right after a method paragraph; not "Jus et zeste d'un citron".
            kinds.append("label")
        else:
            kinds.append("ing")
    # Runs of the same kind, in order.
    runs = []
    for kind, line in zip(kinds, body):
        if kind == "yield":
            continue
        if runs and runs[-1][0] == kind and kind != "label":
            runs[-1][1].append(line)
        else:
            runs.append((kind, [line]))
    # A part: two or more ingredient lines, then method text.
    parts = sum(1 for i, (k, _) in enumerate(runs)
                if k == "prose" and i > 0 and runs[i - 1][0] == "ing" and len(runs[i - 1][1]) >= 2)
    if parts < 2:
        return None

    def label_text(line):
        label = line.strip().rstrip(":").strip()
        if label.isupper():
            label = label.capitalize()
        return f"{label}:"

    ingredients, steps = [], []
    after_method = False      # a part name follows a method paragraph
    part_label = None
    for kind, lines in runs:
        if kind == "label":
            label = label_text(lines[0])
            ingredients.append(label)
            if after_method:
                part_label = label
            after_method = False
        elif kind == "ing":
            ingredients += _clean_ingredients(lines)
            after_method = False
        else:
            if part_label:
                steps.append(part_label)
                part_label = None
            steps.append(_join_paragraph(lines))
            after_method = True
    return ingredients, steps, servings


def rule_segment_raw_text(raw_text: str) -> dict:
    """
    Heuristic segmentation of raw extracted/OCR'd text into title/ingredients/steps.
    No LLM. Used as a starting point for the manual-review screen -- expected to
    need correction on inconsistent layouts, especially OCR output.

    When "Ingredients" appears more than once (a blog post that talks about
    ingredients before its recipe card), the heading followed by the most
    quantity lines is the one used.
    """
    lines = prepare_lines(raw_text)

    ing_idx = [i for i, l in enumerate(lines) if HEADING_INGREDIENTS.match(l)]
    step_idx = [i for i, l in enumerate(lines) if HEADING_STEPS.match(l)]

    ingredients, steps = [], []
    chosen_ing = None
    servings = next((y for y in (_yield_of(l) for l in lines[:12]) if y), None)

    if ing_idx and step_idx:
        best = None
        for i in ing_idx:
            after = [s for s in step_idx if s > i]
            before = [s for s in step_idx if s < i]
            if after:
                s = after[0]
                ing_block, step_block = lines[i + 1:s], lines[s + 1:]
                nxt = [j for j in ing_idx if j > s]
                if nxt:
                    step_block = lines[s + 1:nxt[0]]
            else:
                s = before[-1]
                step_block, ing_block = lines[s + 1:i], lines[i + 1:]
            score = sum(1 for l in ing_block if _QTY_START.match(l))
            if best is None or score > best[0]:
                best = (score, i, ing_block, step_block)
        _, chosen_ing, ing_block, step_block = best
        ingredients = _clean_ingredients(ing_block)
        steps = _clean_steps(step_block)
    elif ing_idx:
        # "Ingredients" but no "Instructions" heading -- common in emails
        # and notes, where the steps are just a numbered list. The
        # ingredients end where the first numbered step begins.
        i = max(ing_idx, key=lambda j: sum(1 for l in lines[j + 1:j + 40] if _QTY_START.match(l)))
        chosen_ing = i
        rest = lines[i + 1:]
        first_step = next((k for k, l in enumerate(rest)
                           if _STEP_START.match(l) and not _QTY_START.match(_STEP_START.sub("", l))), None)
        if first_step is not None:
            ingredients = _clean_ingredients(rest[:first_step])
            steps = _clean_steps(rest[first_step:])
        else:
            ingredients = _clean_ingredients(rest)
    else:
        # No headings at all: an app screenshot with a checklist of
        # ingredients, then the method. Ingredients are the quantity lines;
        # the steps are the prose after the last of them. Apps draw step
        # numbers in circles, which OCR reads as "@", "9", "©" or "faa)",
        # so steps are split at sentence ends rather than by number.
        body = [l for l in lines[1:] if not _is_junk(l)]
        parts = _component_recipe(body)
        qty = [k for k, l in enumerate(body) if QUANTITY_LEAD.match(l) and not _is_prose(l)]
        prose = [k for k, l in enumerate(body)
                 if _is_prose(l) or (qty and k > qty[-1] and _short_step(l))]
        if parts:
            ingredients, steps, servings = parts
        elif qty and prose and prose[-1] > qty[-1] and len(qty) >= 2:
            first = next(k for k in prose if k > qty[-1])
            # Everything from the first measured line up to the method,
            # including unmeasured lines like "Salt and pepper to taste".
            ingredients = body[qty[0]:first]
            steps = _prose_steps(body[first:])
        else:
            for line in body:
                if NUMBERED_STEP.match(line):
                    steps.append(NUMBERED_STEP.sub("", line))
                elif QUANTITY_LEAD.match(line):
                    ingredients.append(line)

    # A yield line read as an ingredient ("Makes 12 cookies") is the servings.
    ingredients = [l for l in ingredients if not _yield_of(l)]
    return {
        "title_guess": _guess_title(lines, chosen_ing),
        "ingredients": ingredients,
        "steps": steps,
        "servings": servings,
    }
