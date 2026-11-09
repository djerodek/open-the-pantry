import cv2
import numpy as np

# A detected angle beyond this is more likely a misdetection (e.g. picking
# up a photo/logo within the page rather than the page's actual rotation)
# than real skew worth correcting -- applying it would do more harm than
# good. Real-world photo/scan skew is usually mild.
MAX_DESKEW_ANGLE = 15.0
MIN_CONTENT_POINTS = 50  # below this, there isn't enough "ink" to trust an angle estimate


def _skew_angle(rect) -> float:
    """Skew in degrees, in the sign getRotationMatrix2D expects, from the
    rectangle's corner points rather than from rect[2].

    minAreaRect's angle field changed convention in OpenCV 4.5.1 (from
    [-90, 0) to (0, 90]) and the sign it implies flipped with it. Code
    calibrated against one version rotates the wrong way on the other --
    an external review running a different OpenCV saw exactly that, on the
    negative-angle tests only. The corner points mean the same thing in
    every version, so the angle is measured from them: of the rectangle's
    edges, take the one closest to horizontal and measure its slope.
    """
    pts = cv2.boxPoints(rect)
    best = None
    for i in range(4):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % 4]
        if x2 < x1:
            x1, y1, x2, y2 = x2, y2, x1, y1
        dx, dy = x2 - x1, y2 - y1
        if dx == 0 and dy == 0:
            continue
        a = float(np.degrees(np.arctan2(dy, dx)))   # image coords: y grows downward
        if -45 < a <= 45 and (best is None or abs(a) < abs(best)):
            best = a
    return best if best is not None else 0.0


def darkest_channel(image) -> np.ndarray:
    """A grayscale array for OCR: each pixel's darkest colour channel, except
    inside photos, which keep the usual gray mix.

    The gray mix turned light coloured text white: a cookbook's teal titles
    (gray about 206) went above the threshold and were never read (Claude
    review of 0165: 0 titles of 9). The darkest channel reads them, but it
    also darkens a photo (a yellow mango's blue channel is near 0), and a
    dark photo broke Tesseract's layout of the whole page: columns and
    lines cut into pieces. Photos are the large dense areas that aren't
    page background; text, even a big title, is thin and sparse. Black and
    gray text read the same either way. Takes a BGR or RGB array, or a PIL
    image."""
    arr = np.asarray(image.convert("RGB")) if hasattr(image, "convert") else np.asarray(image)
    if arr.ndim == 2:
        return arr
    arr = arr[:, :, :3]
    darkest = arr.min(axis=2)
    gray = cv2.cvtColor(np.ascontiguousarray(arr), cv2.COLOR_RGB2GRAY)  # the channel order barely matters here
    photos = _photo_mask(darkest)
    return np.where(photos, gray, darkest).astype(np.uint8)


PHOTO_MIN_AREA = 0.02       # of the image
PHOTO_MIN_SIDE = 0.08       # of the image's height and width
PHOTO_MIN_FILL = 0.6        # of its rectangle that isn't background


def _photo_mask(darkest: np.ndarray) -> np.ndarray:
    h, w = darkest.shape
    ink = (darkest < 235).astype(np.uint8)
    ink = cv2.dilate(ink, np.ones((3, 3), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    mask = np.zeros((h, w), bool)
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if (area >= PHOTO_MIN_AREA * w * h and bh >= PHOTO_MIN_SIDE * h and bw >= PHOTO_MIN_SIDE * w
                and area >= PHOTO_MIN_FILL * bw * bh):
            mask[y:y + bh, x:x + bw] = True
    return mask


# A page photographed on a table: Otsu's threshold took the table as ink,
# so the angle came from the table (0 degrees) and the dark edges filled
# what Tesseract read (Claude review of 0165: 0-62% of the words at 6-10
# degrees). The page is the largest bright shape; it's straightened by its
# own edges and cropped to. Only when it's tilted: an upright bright card
# or photo in a screenshot (dark mode) is left alone, as is a page that
# fills the frame.
PAGE_MIN_AREA = 0.25        # of the image
PAGE_MAX_AREA = 0.97
PAGE_MIN_FILL = 0.9         # the shape's area over its rectangle's: a page, not a blob
PAGE_MIN_TILT = 0.5
PAGE_MAX_TILT = 30.0
PAGE_MIN_CONTRAST = 60      # the page's mean brightness over its surroundings'


def crop_to_page(gray: np.ndarray) -> np.ndarray:
    h, w = gray.shape
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    _, bright = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    # Close the text inside the page so it doesn't cut the shape up.
    k = max(3, (min(h, w) // 60) | 1)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, k)))
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return gray
    page = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(page)
    rect = cv2.minAreaRect(page)
    (cx, cy), (rw, rh), _ = rect
    if not (PAGE_MIN_AREA * w * h <= area <= PAGE_MAX_AREA * w * h) or area < PAGE_MIN_FILL * rw * rh:
        return gray
    angle = _skew_angle(rect)
    if not (PAGE_MIN_TILT <= abs(angle) <= PAGE_MAX_TILT):
        return gray
    mask = np.zeros_like(gray)
    cv2.drawContours(mask, [page], -1, 255, -1)
    inside, outside = gray[mask > 0], gray[mask == 0]
    if outside.size and float(inside.mean()) - float(outside.mean()) < PAGE_MIN_CONTRAST:
        return gray
    matrix = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    turned = cv2.warpAffine(gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
    # The rectangle's sides after turning: the longer one runs the way the
    # page's horizontal edge does.
    pts = cv2.transform(np.array([cv2.boxPoints(rect)]), matrix)[0]
    x0, y0 = np.floor(pts.min(axis=0)).astype(int)
    x1, y1 = np.ceil(pts.max(axis=0)).astype(int)
    m = 3                                                   # just inside the edge
    x0, y0, x1, y1 = max(0, x0 + m), max(0, y0 + m), min(w, x1 - m), min(h, y1 - m)
    if x1 - x0 < w // 5 or y1 - y0 < h // 5:
        return gray
    return turned[y0:y1, x0:x1]


def deskew_grayscale(gray: np.ndarray) -> np.ndarray:
    """
    Detect and correct small rotational skew (a tilted phone photo of a
    physical page, or a slightly crooked scanner feed) before OCR. Takes
    and returns a grayscale numpy array so it drops cleanly into either the
    screenshot/photo pipeline (already cv2-based) or the PDF-page OCR
    pipeline (PIL-based; converted to/from an array at the call site).

    Uses the minimum-area bounding rectangle of thresholded "ink" pixels to
    estimate the skew angle. That thresholding is purely for angle
    detection -- the rotation is applied to the original grayscale input,
    not the binarized copy, so tonal information is preserved for whatever
    thresholding step the caller does next.
    """
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    coords = cv2.findNonZero(thresh)
    if coords is None or len(coords) < MIN_CONTENT_POINTS:
        return gray  # not enough content to estimate an angle reliably

    angle = _skew_angle(cv2.minAreaRect(coords))

    if abs(angle) < 0.3 or abs(angle) > MAX_DESKEW_ANGLE:
        return gray  # negligible skew, or the estimate looks unreliable

    h, w = gray.shape
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        gray, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE,
    )


def auto_orient(pil_img):
    """Turn a page that's sideways or upside down the right way up for OCR.

    Returns (image, degrees_rotated_clockwise). deskew_grayscale only
    corrects small tilts; a photo of a page taken in landscape, or a
    recipe card photographed on its side, went to OCR at 90 degrees and
    came back as gibberish. Tesseract's orientation detector (the osd
    model, installed with tesseract-ocr) says which way is up.

    Below ORIENTATION_MIN_CONF the image is left alone: a wrong rotation
    is worse than none, and detection needs a fair amount of text.
    """
    import pytesseract
    from ..logging_setup import get_logger
    log = get_logger("ocr")
    try:
        from .pdf_ingest import OCR_TIMEOUT_SECONDS
        osd = pytesseract.image_to_osd(pil_img, output_type=pytesseract.Output.DICT, timeout=OCR_TIMEOUT_SECONDS)
    except Exception as e:  # "Too few characters", no osd model, etc.
        log.info("Orientation detection skipped: %s", str(e).strip().splitlines()[0] if str(e).strip() else e)
        return pil_img, 0
    rotate = int(osd.get("rotate", 0)) % 360
    conf = float(osd.get("orientation_conf", 0) or 0)
    if rotate == 0 or conf < ORIENTATION_MIN_CONF:
        if rotate:
            log.info("Orientation: detector says %d degrees but confidence %.1f is too low; not rotating", rotate, conf)
        return pil_img, 0
    log.info("Orientation: page was rotated; turning it %d degrees clockwise (confidence %.1f)", rotate, conf)
    # PIL rotates counter-clockwise; osd "rotate" is the clockwise correction.
    return pil_img.rotate(-rotate, expand=True, fillcolor=255 if pil_img.mode == "L" else None), rotate


# Measured on this project's 29 real screenshots and photos, each also turned
# 90/180/270 degrees (116 cases): the one wrong rotation came at confidence
# 5.9, so a higher threshold wouldn't have prevented it; 3 or 4 only left
# 8-16 more sideways or upside-down pages unfixed. Raise it only with
# examples of wrong rotations below the new value.
ORIENTATION_MIN_CONF = 1.5
