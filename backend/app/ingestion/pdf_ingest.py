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


def extract_largest_embedded_image(pdf_path: str) -> bytes | None:
    """Best-effort extraction of the largest embedded raster image across
    the PDF's pages, as a heuristic for "the recipe's featured photo" --
    real recipe PDFs commonly have exactly one large photo and the rest
    small decorative/logo elements, so picking by area is a reasonable
    proxy without needing to understand document layout.

    Returns raw image bytes, still unvalidated -- the caller must run
    these through the same magic-byte-check + re-encode pipeline as any
    other image before trusting them; embedded PDF images are exactly as
    untrusted as the PDF itself. Returns None if nothing suitable is
    found or the PDF can't be parsed for images at all (never raises --
    a failed thumbnail extraction should never fail the whole ingestion).
    """
    try:
        from pypdf import PdfReader
        from PIL import Image
        import io
    except Exception:
        log.debug("extract_largest_embedded_image: caught error, continuing", exc_info=True)
        return None

    try:
        reader = PdfReader(pdf_path)
    except Exception:
        log.debug("extract_largest_embedded_image: caught error, continuing", exc_info=True)
        return None

    best_bytes = None
    best_area = 0
    for page in reader.pages:
        try:
            images = page.images
        except Exception:
            log.debug("extract_largest_embedded_image: caught error, continuing", exc_info=True)
            continue
        for img in images:
            try:
                # Same format allowlist as the upload and email paths: this
                # only reads the size, but opening lets Pillow pick any of
                # its ~40 decoders for bytes taken from an untrusted PDF.
                pil_img = Image.open(io.BytesIO(img.data), formats=["JPEG", "PNG", "GIF", "WEBP"])
                width, height = pil_img.size
            except Exception:
                log.debug("extract_largest_embedded_image: caught error, continuing", exc_info=True)
                continue
            if width < MIN_SHOWCASE_IMAGE_DIMENSION or height < MIN_SHOWCASE_IMAGE_DIMENSION:
                continue
            area = width * height
            if area > best_area:
                best_area = area
                best_bytes = img.data

    return best_bytes


class PdfIngestResult:
    def __init__(self, raw_text: str, ocr_used_on_pages: list[int], avg_ocr_confidence):
        self.raw_text = raw_text
        self.ocr_used_on_pages = ocr_used_on_pages
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
    )


# Rendering budget for one scanned page. 300 dpi on a letter page is
# ~8.4 MP; an 8.5 x 200 inch page (the iOS full-page-screenshot maximum)
# was 153 MP and 1.25 GB before OCR even started, with up to three OCR jobs
# at once. Tall pages get a lower dpi instead.
MAX_OCR_PIXELS = 35_000_000
OCR_DPI = 300
MIN_OCR_DPI = 72


def _ocr_resolution(width_pt: float, height_pt: float) -> int:
    """dpi for rendering a page of this size (in points) within the budget."""
    area_in2 = max(width_pt, 1) / 72 * max(height_pt, 1) / 72
    dpi = int((MAX_OCR_PIXELS / area_in2) ** 0.5)
    return max(MIN_OCR_DPI, min(OCR_DPI, dpi))


def _preprocess_for_ocr(pil_image: Image.Image) -> Image.Image:
    """Grayscale + deskew + upscale to improve Tesseract accuracy on
    borderline images, including scans fed in at a slight angle.

    The upscale-to-1500px-wide step below is unconditional on its own --
    for an extreme aspect ratio (a 12 x 60000 px render of a 3 x 14400 pt
    page) it multiplies out to billions of pixels even though the render
    itself stayed inside MAX_OCR_PIXELS. This also catches the render
    already exceeding the budget (MIN_OCR_DPI has a floor below which the
    resolution won't drop, so a very large page can still render over
    budget) by downscaling instead of upscaling in that case."""
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


def _ocr_image(pil_image: Image.Image):
    processed = _preprocess_for_ocr(pil_image)
    text = pytesseract.image_to_string(processed)

    # Per-word confidence average, used to drive the "OCR quality: low" badge.
    try:
        data = pytesseract.image_to_data(processed, output_type=pytesseract.Output.DICT)
        confidences = [int(c) for c in data["conf"] if c not in ("-1", -1)]
        avg_conf = sum(confidences) / len(confidences) if confidences else None
    except Exception:
        log.warning("_ocr_image: caught error, continuing", exc_info=True)
        avg_conf = None

    return text, avg_conf


# "Ingredients" alone on a line, optionally followed by a recipe card's
# serving-size buttons ("Ingredients 1X 2X 3X").
HEADING_INGREDIENTS = re.compile(
    r"^\s*ingredients?\s*:?\s*(?:\d+(?:\.\d+)?\s*[x×]\s*)*$", re.IGNORECASE | re.MULTILINE
)
HEADING_STEPS = re.compile(
    r"^\s*(instructions?|directions?|method|steps?|preparation)\s*:?\s*$", re.IGNORECASE | re.MULTILINE
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
_STEP_START = re.compile(r"^\s*(\d{1,2})(?:[\.\)]\s*|\s+)(?=[A-Z])")
_BARE_NUMBER = re.compile(r"^\s*\d{1,2}\s*$")

# Lines that are page furniture, not recipe content. Printed web pages carry
# the browser's date/time and "Page 1 of 3"; recipe cards carry serving-size
# and unit toggles.
_JUNK_LINE = re.compile(
    r"^\s*("
    r"\d{4}-\d{2}-\d{2},?\s+\d{1,2}:\d{2}(\s*[AP]M)?"         # 2026-09-25, 1:44 PM
    r"|\d{1,2}/\d{1,2}/\d{2,4},?\s+\d{1,2}:\d{2}(\s*[AP]M)?"  # 9/25/26, 1:44 PM
    r"|page \d+ of \d+"
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
    r"did you make this.*|course|cuisine|keyword|author)\s*:?\s*$",
    re.IGNORECASE,
)
_BYLINE = re.compile(r"^\s*by[:\s]", re.IGNORECASE)
_BREADCRUMB = re.compile(r"\S\s+/\s+\S.*\s+/\s+\S")


def _is_junk(line: str) -> bool:
    return bool(_JUNK_LINE.match(line))


def _looks_like_section_label(line: str) -> bool:
    """"PEPPERED BACON CURE", "For the sauce:", "Topping" -- a sub-heading
    inside an ingredient list, not an ingredient."""
    if _QTY_START.match(line) or len(line.split()) > 6:
        return False
    return line.isupper() or line.rstrip().endswith(":") or line.lower().startswith("for the ")


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
        line = _LABELLED_QTY.sub("", line)
        if _looks_like_section_label(line):
            label = line.rstrip(":").strip()
            if label.isupper() or label.istitle():
                label = label.lower()
            if not label.lower().startswith("for "):
                label = f"For the {label[0].lower() + label[1:]}"
            out.append(f"{label}:")
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
                and not re.search(r"\$\d", l))

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
    return line.strip()


def segment_raw_text(raw_text: str) -> dict:
    """
    Heuristic segmentation of raw extracted/OCR'd text into title/ingredients/steps.
    No LLM. Used as a starting point for the manual-review screen -- expected to
    need correction on inconsistent layouts, especially OCR output.

    When "Ingredients" appears more than once (a blog post that talks about
    ingredients before its recipe card), the heading followed by the most
    quantity lines is the one used.
    """
    lines = [_normalize_line(l) for l in raw_text.split("\n")]
    lines = [l for l in lines if l]

    ing_idx = [i for i, l in enumerate(lines) if HEADING_INGREDIENTS.match(l)]
    step_idx = [i for i, l in enumerate(lines) if HEADING_STEPS.match(l)]

    ingredients, steps = [], []
    chosen_ing = None

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
        # No clear headings found -- fall back to line-pattern matching.
        for line in lines[1:]:
            if _is_junk(line):
                continue
            if NUMBERED_STEP.match(line):
                steps.append(NUMBERED_STEP.sub("", line))
            elif QUANTITY_LEAD.match(line):
                ingredients.append(line)

    return {
        "title_guess": _guess_title(lines, chosen_ing),
        "ingredients": ingredients,
        "steps": steps,
    }
