import cv2
import numpy as np
from PIL import Image
import pytesseract

from .pdf_ingest import segment_raw_text  # reuse the same heuristic segmentation
from .deskew import deskew_grayscale


class ImageIngestResult:
    def __init__(self, raw_text: str, ocr_confidence):
        self.raw_text = raw_text
        self.ocr_confidence = ocr_confidence  # 0-100, or None


def _preprocess(image_path: str) -> Image.Image:
    """Grayscale + deskew + threshold + upscale via OpenCV, improves Tesseract
    accuracy on screenshots (rendered text, usually clean) and photographed
    pages alike (including ones photographed at a slight angle)."""
    img = cv2.imread(image_path)
    if img is None:
        return Image.open(image_path).convert("L")

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = deskew_grayscale(gray)

    h, w = gray.shape
    if w < 1500:
        scale = 1500 / w
        gray = cv2.resize(gray, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_LANCZOS4)

    # Otsu thresholding -- binarizes text vs. background, helps with low-contrast
    # screenshots (e.g. stylized recipe-card graphics with text over a photo).
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    return Image.fromarray(thresh)


def ingest_image(image_path: str) -> ImageIngestResult:
    processed = _preprocess(image_path)
    text = pytesseract.image_to_string(processed)

    try:
        data = pytesseract.image_to_data(processed, output_type=pytesseract.Output.DICT)
        confidences = [int(c) for c in data["conf"] if c not in ("-1", -1)]
        avg_conf = sum(confidences) / len(confidences) if confidences else None
    except Exception:
        avg_conf = None

    return ImageIngestResult(raw_text=text, ocr_confidence=avg_conf)


def ingest_and_segment(image_path: str) -> dict:
    result = ingest_image(image_path)
    segmented = segment_raw_text(result.raw_text)
    segmented["raw_text"] = result.raw_text
    segmented["ocr_confidence"] = result.ocr_confidence
    return segmented
