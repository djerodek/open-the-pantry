import cv2
import numpy as np

# A detected angle beyond this is more likely a misdetection (e.g. picking
# up a photo/logo within the page rather than the page's actual rotation)
# than real skew worth correcting -- applying it would do more harm than
# good. Real-world photo/scan skew is usually mild.
MAX_DESKEW_ANGLE = 15.0
MIN_CONTENT_POINTS = 50  # below this, there isn't enough "ink" to trust an angle estimate


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

    angle = cv2.minAreaRect(coords)[-1]
    # minAreaRect's angle convention is inconsistent across OpenCV
    # versions/builds and isn't reliably documented -- verified empirically
    # against known rotated test images (in both directions, across several
    # magnitudes) rather than trusted from convention docs alone. This
    # build returns angles in [0, 90): values above 45 represent the same
    # rotation referenced against the rectangle's other pair of edges, and
    # need -90 applied to collapse to a small, correctly-signed angle --
    # using them as-is silently produced near-90-degree rotations that
    # only "worked" on some test cases by coincidence (partial content
    # overlap), not because the correction was actually right.
    if angle > 45:
        angle = angle - 90

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
        osd = pytesseract.image_to_osd(pil_img, output_type=pytesseract.Output.DICT)
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


ORIENTATION_MIN_CONF = 1.5
