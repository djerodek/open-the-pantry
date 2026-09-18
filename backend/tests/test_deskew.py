import os
import sys

import numpy as np
import pytesseract
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.ingestion.deskew import deskew_grayscale  # noqa: E402

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _make_rotated_text_image(angle_degrees: float) -> np.ndarray:
    img = Image.new("L", (900, 350), color=255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(FONT_PATH, 40)
    draw.text((80, 130), "Ingredients: 2 cups flour", fill=0, font=font)
    rotated = img.rotate(angle_degrees, expand=True, fillcolor=255)
    return np.array(rotated)


def _ocr_readable(gray: np.ndarray) -> bool:
    text = pytesseract.image_to_string(Image.fromarray(gray)).strip()
    return "ngredient" in text and "flour" in text


@pytest.mark.parametrize("angle", [-15, -10, -8, -5, -3, -1, 1, 3, 5, 8, 10])
def test_deskew_corrects_rotation_in_both_directions(angle):
    """Regression test for a real bug: an earlier version of deskew_grayscale
    applied the correction angle with the wrong sign, which rotated the
    image *further* in the wrong direction instead of fixing the skew --
    verified only by actually running OCR on known-rotated test images in
    both directions, not by trusting cv2.minAreaRect's documented angle
    convention (which is inconsistent across OpenCV versions/builds).

    Doesn't assert the rotated image is unreadable *before* correction --
    Tesseract has its own mild tolerance for small skew, so that's not
    reliably true at small angles and isn't the point. What matters is
    that the corrected output is reliably readable."""
    rotated = _make_rotated_text_image(angle)
    corrected = deskew_grayscale(rotated)
    assert _ocr_readable(corrected), f"deskew failed to correct a {angle} degree rotation"


def test_deskew_leaves_unrotated_image_readable():
    img = _make_rotated_text_image(0)
    corrected = deskew_grayscale(img)
    assert _ocr_readable(corrected)


def test_deskew_caps_correction_beyond_max_angle():
    """Skew beyond MAX_DESKEW_ANGLE is deliberately left uncorrected -- a
    larger detected angle is more likely a misdetection than real skew
    worth correcting. This locks in that boundary is respected, not that
    the specific cap value is exactly right."""
    from app.ingestion.deskew import MAX_DESKEW_ANGLE
    rotated = _make_rotated_text_image(MAX_DESKEW_ANGLE + 10)
    corrected = deskew_grayscale(rotated)
    assert np.array_equal(corrected, rotated), "should be returned unchanged, not partially corrected"


def test_deskew_does_not_crash_on_blank_image():
    blank = np.full((300, 800), 255, dtype=np.uint8)
    result = deskew_grayscale(blank)
    assert result.shape == blank.shape


def test_deskew_does_not_crash_on_sparse_content():
    """A few stray pixels shouldn't be enough to attempt an angle estimate."""
    sparse = np.full((300, 800), 255, dtype=np.uint8)
    sparse[10:12, 10:12] = 0  # a handful of dark pixels, well under MIN_CONTENT_POINTS
    result = deskew_grayscale(sparse)
    assert np.array_equal(result, sparse)
