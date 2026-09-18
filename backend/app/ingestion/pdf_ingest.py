import re
import numpy as np
import pdfplumber
import pytesseract
from PIL import Image

from .deskew import deskew_grayscale

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
        return None

    try:
        reader = PdfReader(pdf_path)
    except Exception:
        return None

    best_bytes = None
    best_area = 0
    for page in reader.pages:
        try:
            images = page.images
        except Exception:
            continue
        for img in images:
            try:
                pil_img = Image.open(io.BytesIO(img.data))
                width, height = pil_img.size
            except Exception:
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
            pil_image = page.to_image(resolution=300).original
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


def _preprocess_for_ocr(pil_image: Image.Image) -> Image.Image:
    """Grayscale + deskew + upscale to improve Tesseract accuracy on
    borderline images, including scans fed in at a slight angle."""
    gray_arr = deskew_grayscale(np.array(pil_image.convert("L")))
    gray = Image.fromarray(gray_arr)
    if gray.width < 1500:
        scale = 1500 / gray.width
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
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
        avg_conf = None

    return text, avg_conf


HEADING_INGREDIENTS = re.compile(r"^\s*ingredients?\s*:?\s*$", re.IGNORECASE | re.MULTILINE)
HEADING_STEPS = re.compile(
    r"^\s*(instructions?|directions?|method|steps?)\s*:?\s*$", re.IGNORECASE | re.MULTILINE
)
NUMBERED_STEP = re.compile(r"^\s*\d+[\.\)]\s+")
QUANTITY_LEAD = re.compile(
    r"^\s*\d+[\d/\.\s]*\s*(cups?|tbsp|tsp|g|kg|oz|lb|ml|l|pinch|clove)?\b", re.IGNORECASE
)


def segment_raw_text(raw_text: str) -> dict:
    """
    Heuristic segmentation of raw extracted/OCR'd text into title/ingredients/steps.
    No LLM. Used as a starting point for the manual-review screen -- expected to
    need correction on inconsistent layouts, especially OCR output.
    """
    lines = [l.strip() for l in raw_text.split("\n")]
    lines = [l for l in lines if l]

    ing_match = HEADING_INGREDIENTS.search(raw_text)
    step_match = HEADING_STEPS.search(raw_text)

    ingredients, steps, title_guess = [], [], (lines[0] if lines else "Untitled Recipe")

    if ing_match and step_match:
        ing_start = raw_text.index(ing_match.group())
        step_start = raw_text.index(step_match.group())
        if ing_start < step_start:
            ing_block = raw_text[ing_start + len(ing_match.group()):step_start]
            step_block = raw_text[step_start + len(step_match.group()):]
        else:
            step_block = raw_text[step_start + len(step_match.group()):ing_start]
            ing_block = raw_text[ing_start + len(ing_match.group()):]
        ingredients = [l.strip() for l in ing_block.split("\n") if l.strip()]
        steps = [l.strip() for l in step_block.split("\n") if l.strip()]
    else:
        # No clear headings found -- fall back to line-pattern matching.
        for line in lines[1:]:
            if NUMBERED_STEP.match(line):
                steps.append(NUMBERED_STEP.sub("", line))
            elif QUANTITY_LEAD.match(line):
                ingredients.append(line)

    return {
        "title_guess": title_guess,
        "ingredients": ingredients,
        "steps": steps,
    }
