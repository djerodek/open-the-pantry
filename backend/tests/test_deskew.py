import os
import sys

import numpy as np
import pytesseract
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.ingestion.deskew import deskew_grayscale  # noqa: E402
from .conftest import text_font  # noqa: E402



def _make_rotated_text_image(angle_degrees: float) -> np.ndarray:
    img = Image.new("L", (900, 350), color=255)
    draw = ImageDraw.Draw(img)
    font = text_font(40, bold=True)
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


@pytest.mark.parametrize("skew", [-15, -10, -8, -3, 3, 8, 10, 15])
def test_skew_angle_is_the_same_under_both_opencv_angle_conventions(skew):
    """minAreaRect's angle field changed convention in OpenCV 4.5.1; an
    external review on a different OpenCV saw the negative-angle deskew
    tests fail. The same rectangle can be written as (w, h, a) or
    (h, w, a - 90) -- the two conventions describe it differently -- and
    the skew measured from its corners must not depend on which."""
    from app.ingestion.deskew import _skew_angle

    new_style = ((500.0, 400.0), (600.0, 300.0), float(skew % 90 or 90))
    old_style = ((500.0, 400.0), (300.0, 600.0), new_style[2] - 90.0)
    assert _skew_angle(new_style) == pytest.approx(_skew_angle(old_style), abs=1e-3)  # boxPoints is float32


# ---- Claude review of 0165: coloured titles, photos, pages on a table

def _recipe_page(title_fill=(150, 222, 214)):
    img = Image.new("RGB", (1000, 700), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 40), "SUMMER SALAD", font=text_font(64, bold=True), fill=title_fill)
    for n, line in enumerate(["Ingredients", "2 cups spinach", "1 cucumber, sliced", "Toss everything together."]):
        d.text((60, 170 + n * 60), line, font=text_font(36), fill=(40, 40, 40))
    return img


def test_a_light_coloured_title_is_read(tmp_path):
    """The teal titles (gray about 206) went white at the threshold: 0 of 9
    read. The darkest colour channel reads them."""
    from app.ingestion.image_ingest import ingest_image
    _recipe_page().save(tmp_path / "p.png")
    assert "SUMMER" in ingest_image(str(tmp_path / "p.png")).raw_text.upper()


def test_photos_keep_the_gray_mix_and_text_gets_the_darkest_channel():
    """A dark photo (a yellow mango's blue channel is near 0) broke the
    page's layout, so photos keep the usual gray."""
    from app.ingestion.deskew import darkest_channel
    img = _recipe_page()
    ImageDraw.Draw(img).rectangle((500, 400, 950, 680), fill=(240, 200, 30))     # the "photo"
    out = darkest_channel(img)
    assert out[600, 700] > 150                 # gray of yellow, not its blue channel (30)
    teal = np.argwhere(np.all(np.asarray(img) == (150, 222, 214), axis=2))[0]
    assert out[tuple(teal)] == 150             # the title: its darkest channel
    assert darkest_channel(np.zeros((5, 5), np.uint8)).shape == (5, 5)


def _on_a_table(page: Image.Image, angle: float) -> np.ndarray:
    rng = np.random.default_rng(3)
    turned = page.rotate(angle, expand=True, resample=Image.BICUBIC)
    mask = Image.new("L", page.size, 255).rotate(angle, expand=True)
    table = Image.fromarray(rng.normal(55, 15, (turned.height * 6 // 5, turned.width * 5 // 4)).clip(0, 255).astype(np.uint8))
    table = table.convert("RGB")
    table.paste(turned, ((table.width - turned.width) // 2, (table.height - turned.height) // 2), mask)
    return np.asarray(table)


@pytest.mark.parametrize("angle", [-8, 4, 10])
def test_a_tilted_page_on_a_table_is_straightened_and_cropped(angle, tmp_path):
    """On a dark table the angle came from the table (0 degrees) and 0-62%
    of the words were read at 6-10 degrees."""
    from app.ingestion.deskew import crop_to_page
    from app.ingestion.image_ingest import ingest_image
    photo = _on_a_table(_recipe_page(title_fill=(30, 30, 30)), angle)
    gray = np.asarray(Image.fromarray(photo).convert("L"))
    cropped = crop_to_page(gray)
    assert cropped.shape[0] < gray.shape[0] and cropped.shape[1] < gray.shape[1]
    Image.fromarray(photo).save(tmp_path / "p.jpg", quality=90)
    text = ingest_image(str(tmp_path / "p.jpg")).raw_text.lower()
    assert all(w in text for w in ("summer", "spinach", "cucumber", "together")), text


def test_an_upright_bright_area_or_a_page_on_white_is_left_alone():
    """A dark-mode screenshot with a bright card, or a page that fills the
    frame, isn't cropped: only a tilted page is."""
    from app.ingestion.deskew import crop_to_page
    dark = np.full((800, 600), 20, np.uint8)
    dark[100:500, 50:550] = 245                       # an upright card
    assert crop_to_page(dark) is dark
    page = np.asarray(_recipe_page().convert("L"))
    assert crop_to_page(page) is page
    tilted_on_white = np.asarray(Image.fromarray(page).rotate(6, expand=True, fillcolor=255))
    assert crop_to_page(tilted_on_white) is tilted_on_white
