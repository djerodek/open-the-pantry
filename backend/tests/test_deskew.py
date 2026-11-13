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
    # Pale beige list dots (spread about 34) keep the gray mix: darkened,
    # they became a column of their own (Claude review of 0167).
    ImageDraw.Draw(img).ellipse((20, 300, 30, 310), fill=(214, 205, 180))
    assert darkest_channel(img)[305, 25] > 195
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


def test_a_straight_page_with_a_title_further_left_isnt_turned():
    """Owner's cookie page: the title starts 24 px left of everything else,
    so the ink's outline slants by about a degree; turning the straight page
    by that blurred the thin 1s away ("1 c cocoa" read as "C cocoa"). A real
    tilt makes the upright box clearly larger than the turned one."""
    im = Image.new("L", (1100, 1700), 255)
    d = ImageDraw.Draw(im)
    d.rectangle((100, 100, 360, 150), fill=60)                 # the title
    for y in range(250, 760, 40):
        d.rectangle((124, y, 900, y + 12), fill=60)            # the text
    d.rectangle((124, 800, 1000, 1600), fill=40)               # the photo
    page = np.asarray(im)
    assert deskew_grayscale(page) is page
    # The same page really tilted is still straightened.
    tilted = np.asarray(im.rotate(4, expand=True, fillcolor=255))
    assert deskew_grayscale(tilted) is not tilted


# ---- Review of 0169: the tilt read from the text lines

def _lines_page(n_lines=12, width=1100):
    im = Image.new("L", (width, 160 + 50 * n_lines), 255)
    d = ImageDraw.Draw(im)
    for i in range(n_lines):
        d.text((80, 80 + 50 * i), f"Line {i + 1}: whisk the eggs with the sugar until pale and thick",
               font=text_font(28), fill=30)
    return im


@pytest.mark.parametrize("angle", [0.5, 1.5, -2, 3, 10])
def test_the_tilt_comes_from_the_text_lines(angle):
    """0169 left a short page (Maple sauce, Pina colada) crooked at 0.5-3
    degrees, and the rectangle around the ink was about a degree off on
    full pages."""
    for page in (_lines_page(3), _lines_page(14)):
        turned = np.asarray(page.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC))
        assert deskew_grayscale(turned) is not turned
        from app.ingestion.deskew import estimate_skew
        assert abs(estimate_skew(turned) + angle) <= 0.15, (angle, estimate_skew(turned))


def test_a_straight_page_with_the_ingredients_beside_the_method_isnt_turned():
    """The rectangle said 12.4 degrees for such a page; 0168 turned it by
    that (2-11% of the words read)."""
    im = Image.new("L", (1400, 1100), 255)
    d = ImageDraw.Draw(im)
    for i in range(8):
        d.text((60, 120 + 44 * i), f"{i + 1} cup item", font=text_font(26), fill=30)
    for i in range(16):
        d.text((560, 60 + 38 * i), "Stir the sauce slowly until it thickens and coats a spoon.", font=text_font(24), fill=30)
    page = np.asarray(im)
    assert deskew_grayscale(page) is page


def test_a_photo_over_the_budget_is_reduced_before_anything_works_on_it(tmp_path, monkeypatch):
    """GPT review of 0174: the 35 MP OCR budget was applied last, after the
    channel, photo-mask, page and tilt steps had each made full-size copies
    (641 MiB at the peak for one 50 MP photo, three imports at once). The
    page step now gets an image already inside the budget; a smaller one is
    left as it is."""
    from app.ingestion import image_ingest
    seen = []
    real = image_ingest.crop_to_page
    monkeypatch.setattr(image_ingest, "crop_to_page", lambda gray: (seen.append(gray.size), real(gray))[1])
    monkeypatch.setattr(image_ingest, "MAX_OCR_PIXELS", 1_000_000)
    big, small = tmp_path / "big.jpg", tmp_path / "small.jpg"
    Image.new("RGB", (2000, 1500), (235, 228, 214)).save(big)
    Image.new("RGB", (1200, 800), (235, 228, 214)).save(small)
    image_ingest._preprocess(str(big))
    image_ingest._preprocess(str(small))
    assert 950_000 < seen[0] <= 1_000_000        # 3 MP brought down to the 1 MP budget
    assert seen[1] == 1200 * 800                 # inside the budget: untouched


def test_the_colour_channel_step_gives_the_same_pixels_in_less_memory():
    """Same output as the int16 version it replaced, with the photo mask
    (whose labels array is the biggest temporary) run before the other
    full-size arrays are made: 9 bytes a pixel at the peak, now 7."""
    import tracemalloc

    from app.ingestion.deskew import COLOUR_SPREAD, _photo_mask, darkest_channel
    import cv2
    rng = np.random.default_rng(1)
    img = np.full((1500, 1500, 3), 235, np.uint8)
    img[150:700, 200:1000] = rng.integers(0, 255, (550, 800, 3))      # a photo
    img[900:908, 150:1300] = (20, 60, 200)                            # coloured text
    img[1000:1008, 150:1300] = (90, 90, 90)                           # gray text
    darkest = img.min(axis=2)
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    coloured = (img.max(axis=2).astype(np.int16) - darkest) >= COLOUR_SPREAD
    expected = np.where(coloured & ~_photo_mask(darkest), darkest, gray).astype(np.uint8)
    tracemalloc.start()
    tracemalloc.reset_peak()
    base = tracemalloc.get_traced_memory()[0]
    got = darkest_channel(img)
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    assert got.dtype == np.uint8 and np.array_equal(got, expected)
    assert peak <= 8 * img.shape[0] * img.shape[1], f"{peak / (img.shape[0] * img.shape[1]):.1f} bytes a pixel"
