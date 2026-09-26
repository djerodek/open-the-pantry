import cv2
import numpy as np
from PIL import Image
import pytesseract

from .pdf_ingest import segment_raw_text, MAX_OCR_PIXELS  # reuse the same heuristic segmentation and pixel budget
from .deskew import deskew_grayscale, auto_orient
from ..file_validation import JPEG_QUALITY
from ..logging_setup import get_logger

log = get_logger("ocr")


class ImageIngestResult:
    def __init__(self, raw_text: str, ocr_confidence):
        self.raw_text = raw_text
        self.ocr_confidence = ocr_confidence  # 0-100, or None


def _preprocess(image_path: str):
    """Grayscale + orientation + deskew + threshold + upscale via OpenCV,
    improves Tesseract accuracy on screenshots (rendered text, usually clean)
    and photographed pages alike (including ones photographed sideways or at
    a slight angle). Returns (image, degrees rotated clockwise)."""
    img = cv2.imread(image_path)
    if img is None:
        return Image.open(image_path).convert("L"), 0

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    oriented, rotated = auto_orient(Image.fromarray(gray))
    gray = deskew_grayscale(np.array(oriented))

    h, w = gray.shape
    # As in pdf_ingest._preprocess_for_ocr: cap the upscale (and any
    # already-oversized upload) at the same pixel budget, so an extreme
    # aspect ratio -- a 20 x 40000 px upload -- can't multiply out to
    # gigapixels before Tesseract ever sees it.
    scale = 1500 / w if w < 1500 else 1.0
    current_pixels = w * h
    if current_pixels * scale * scale > MAX_OCR_PIXELS:
        scale = (MAX_OCR_PIXELS / current_pixels) ** 0.5
    if scale != 1.0:
        new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
        gray = cv2.resize(gray, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

    # Otsu thresholding -- binarizes text vs. background, helps with low-contrast
    # screenshots (e.g. stylized recipe-card graphics with text over a photo).
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return Image.fromarray(thresh), rotated


def _rotate_stored_file(image_path: str, degrees_clockwise: int):
    """Keep the saved photo the same way up as the text that was read from
    it, so the recipe's showcase image doesn't display sideways."""
    try:
        with Image.open(image_path) as im:
            im.load()
            fmt = im.format
            turned = im.rotate(-degrees_clockwise, expand=True)
        # A second JPEG encode (the upload was already re-encoded once);
        # at Pillow's default quality 75 the loss compounded visibly.
        turned.save(image_path, format=fmt, **({"quality": JPEG_QUALITY} if fmt == "JPEG" else {}))
    except Exception:
        log.warning("Couldn't rotate stored image %s", image_path, exc_info=True)


def ingest_image(image_path: str) -> ImageIngestResult:
    processed, rotated = _preprocess(image_path)
    if rotated:
        _rotate_stored_file(image_path, rotated)
    text = pytesseract.image_to_string(processed)

    try:
        data = pytesseract.image_to_data(processed, output_type=pytesseract.Output.DICT)
        confidences = [int(c) for c in data["conf"] if c not in ("-1", -1)]
        avg_conf = sum(confidences) / len(confidences) if confidences else None
    except Exception:
        log.warning("OCR confidence unavailable for %s", image_path, exc_info=True)
        avg_conf = None

    return ImageIngestResult(raw_text=text, ocr_confidence=avg_conf)


def ingest_and_segment(image_path: str) -> dict:
    result = ingest_image(image_path)
    segmented = segment_raw_text(result.raw_text)
    segmented["raw_text"] = result.raw_text
    segmented["ocr_confidence"] = result.ocr_confidence
    return segmented
