"""OCR of a page or photo with Tesseract: preparing the image, reading it
(its own layout first, whole lines when that misses), and putting
two-column lists back in order. Split out of pdf_ingest.py (0186)."""
import contextvars
import time

import numpy as np
import pytesseract
from PIL import Image

from .deskew import deskew_grayscale, auto_orient, darkest_channel
from .segment import rule_segment_raw_text
from ..logging_setup import get_logger

log = get_logger("pdf")

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
    oriented, _ = auto_orient(Image.fromarray(darkest_channel(pil_image)))
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

# Each tesseract run is stopped after this long (pytesseract kills the
# process and raises): a crafted image could otherwise hold one of the
# three heavy-job slots indefinitely, and three would stop every import
# and the email scan (Claude security audit of 0140). A full page takes
# a few seconds.
OCR_TIMEOUT_SECONDS = 120

# OCR time for one import, all its pages or photos together: a PDF's scanned
# pages, or the photos combined into one recipe (up to 10, each up to two
# Tesseract runs: 40 minutes at the per-run limit; GPT reviews of 0190 and
# 0204). Each run's limit is cut to what is left, so the import stops at
# this time, not after the page under way.
MAX_IMPORT_OCR_SECONDS = 10 * 60

_deadline: contextvars.ContextVar = contextvars.ContextVar("ocr_deadline", default=None)


class OcrTimeUp(Exception):
    """The import's OCR time (MAX_IMPORT_OCR_SECONDS) ran out."""


def start_deadline(seconds: float):
    """Starts the current import's OCR clock; returns the token for
    end_deadline(). Every Tesseract run in this thread until then stops
    at it."""
    return _deadline.set(time.monotonic() + seconds)


def end_deadline(token) -> None:
    _deadline.reset(token)


def run_timeout() -> float:
    """The time one Tesseract run may take: OCR_TIMEOUT_SECONDS, or what is
    left of the import's OCR time if less. Raises OcrTimeUp when none is."""
    deadline = _deadline.get()
    if deadline is None:
        return OCR_TIMEOUT_SECONDS
    left = deadline - time.monotonic()
    if left <= 0:
        raise OcrTimeUp()
    return min(OCR_TIMEOUT_SECONDS, left)


def _time_up() -> bool:
    deadline = _deadline.get()
    return deadline is not None and time.monotonic() >= deadline


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
    return ocr_text_and_confidence(_preprocess_for_ocr(pil_image))


def ocr_text_and_confidence(processed: Image.Image):
    """The text Tesseract reads and its average word confidence (0-100, or
    None; it drives the "OCR quality: low" badge). One run for both (there
    were two, review of 0165). Two-column lists are read column by column
    (_lines_from_words).

    Tesseract's own layout first: it keeps an ingredient list beside the
    method apart, and two columns of running text in order, which reading
    whole lines (--psm 4, 0169 for every page) mixed (review of 0169: 4-6
    of 9 ingredients). When that gives no ingredients or no steps, the
    page is read again as whole lines, which an upright cookbook page with
    a paragraph across both columns needs (the cookie page's method), and
    the more complete reading is kept.

    Also read again when a step starts mid-word: its first two words in
    lowercase ("re to its bowl", "nce golden brown"). Tesseract's layout
    cut a method running across both columns into strips that interleave
    (the Dosa page: 6 steps, 4 cut, where whole lines gave 5 whole ones;
    review of 0176). A stray letter for a printed step number ("w Beat the
    butter") is followed by a capital, so it doesn't count. The reading
    with fewer such steps is kept, if it still has ingredients and steps."""
    first = _ocr_once(processed, None)
    done, cut = _reading_score(first[0])
    if done[0] == 2 and not cut:
        return first
    second = _ocr_once(processed, "--psm 4")
    done2, cut2 = _reading_score(second[0])
    if done[0] < 2:
        return second if done2 > done else first
    return second if done2[0] == 2 and cut2 < cut else first


def _cut_step(step: str) -> bool:
    words = step.split()[:2]
    return len(words) == 2 and all(w[:1].islower() for w in words)


def _reading_score(text: str) -> tuple[tuple[int, int], int]:
    """((whether there are ingredients + whether there are steps, how many
    of them), how many steps start mid-word): which of two readings of a
    page to keep."""
    try:
        seg = rule_segment_raw_text(text)
    except Exception:
        log.warning("Couldn't segment an OCR reading to compare it", exc_info=True)
        return (0, 0), 0
    ing, steps = seg.get("ingredients") or [], seg.get("steps") or []
    return (bool(ing) + bool(steps), len(ing) + len(steps)), sum(_cut_step(s) for s in steps)


def _ocr_once(processed: Image.Image, config: str | None):
    kwargs = {"config": config} if config else {}
    try:
        data = pytesseract.image_to_data(processed, lang=ocr_lang(), output_type=pytesseract.Output.DICT,
                                         timeout=run_timeout(), **kwargs)
    except RuntimeError:
        # pytesseract's "Tesseract process timeout": the import's time, not
        # this run's own limit, when the clock has run out.
        log.debug("_ocr_once: Tesseract stopped", exc_info=True)
        if _time_up():
            raise OcrTimeUp() from None
        raise
    confidences = []
    for c in data["conf"]:
        try:
            if float(c) != -1:
                confidences.append(float(c))
        except (TypeError, ValueError):
            continue
    avg_conf = sum(confidences) / len(confidences) if confidences else None
    return _lines_from_words(data, processed.width), avg_conf


# A gap this wide between two words on one line is a gutter between
# columns, not a space: several times the text's height, and a fair part of
# the page.
COLUMN_GAP_HEIGHTS = 4
COLUMN_GAP_PAGE = 0.05


def _lines_from_words(data: dict, page_width: int) -> str:
    """The text as image_to_string gives it (a line per line, a blank line
    between paragraphs), except that a paragraph laid out in two columns --
    a two-column ingredient list -- is given left column first, then right.
    Tesseract read across the gap: "1 cup uncooked rice indian spices:"
    (Claude review of 0165, 5 pages of 9). Two columns need at least two
    lines of a paragraph split at the same place; a line across the gutter,
    or a heading between two column blocks, ends a block."""
    paragraphs = {}
    for i, word in enumerate(data["text"]):
        if int(data["level"][i]) != 5 or not str(word).strip():
            continue
        key = (data["block_num"][i], data["par_num"][i])
        para = paragraphs.setdefault(key, {})
        para.setdefault(data["line_num"][i], []).append(
            (int(data["left"][i]), int(data["width"][i]), int(data["height"][i]), str(word).strip()))
    out = []
    for para in paragraphs.values():
        lines = [sorted(words) for _, words in sorted(para.items())]
        out.append("\n".join(_column_order(lines, page_width)))
    return "\n\n".join(out) + ("\n" if out else "")


def _column_order(lines, page_width: int) -> list[str]:
    def text(words):
        return " ".join(w[3] for w in words)

    def split(words):
        heights = sorted(w[2] for w in words)
        gap = max(COLUMN_GAP_HEIGHTS * heights[len(heights) // 2], COLUMN_GAP_PAGE * page_width)
        parts = [[words[0]]]
        for prev, word in zip(words, words[1:]):
            if word[0] - (prev[0] + prev[1]) > gap:
                parts.append([])
            parts[-1].append(word)
        return parts

    parted = [split(words) for words in lines]
    two = [p for p in parted if len(p) == 2]
    if len(two) < 2:
        return [text(words) for words in lines]
    starts = sorted(p[1][0][0] for p in two)
    col = starts[len(starts) // 2]
    tol = COLUMN_GAP_PAGE * page_width

    def kind(p):
        if len(p) == 2:
            ok = abs(p[1][0][0] - col) <= tol and p[0][-1][0] + p[0][-1][1] <= col
            return "two" if ok else "across"
        if len(p) > 2:
            return "across"
        if p[0][0][0] >= col - tol:
            return "right"
        return "left" if p[0][-1][0] + p[0][-1][1] < col else "across"

    kinds = [kind(p) for p in parted]
    out, left, right = [], [], []

    def flush():
        out.extend(left + right)
        left.clear()
        right.clear()

    for n, (p, k) in enumerate(zip(parted, kinds)):
        nxt = kinds[n + 1] if n + 1 < len(kinds) else None
        if k == "across":
            # A line over the gutter (a heading row, a paragraph) ends the
            # columns before it.
            flush()
            out.append(text(lines[n]))
        elif k == "left" and nxt == "two":
            # A heading between two column blocks ("WISH" after "SIFT"):
            # the block above is given whole before it (owner's cookie page).
            flush()
            out.append(text(p[0]))
        elif k == "two":
            left.append(text(p[0]))
            right.append(text(p[1]))
        elif k == "left":
            left.append(text(p[0]))
        else:
            right.append(text(p[0]))
    flush()
    return out
