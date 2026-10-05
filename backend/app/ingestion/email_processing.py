import os
import re
import uuid

from .completeness import missing_parts as missing_sections
from .pdf_ingest import extract_pdf_text, segment_raw_text, PdfTooLargeError
from .image_ingest import ingest_image
from .url_ingest import ingest_url, require_complete, UrlValidationError, NoRecipeFoundError, IncompleteRecipeError, SiteRefusedError
from .ingredient_parser import parse_ingredient_block
from .tagger import suggest_tags
from ..file_validation import validate_and_save_pdf_bytes, validate_and_save_image_bytes
from ..logging_setup import get_logger

log = get_logger("email")

URL_RE = re.compile(r'https?://[^\s<>"\')]+')

# Footers that mail apps add to every message. Outlook's carries a link
# ("Get Outlook for iOS<https://aka.ms/o0ukef>"), so an email that is just
# a recipe link arrived with two links and the link step was skipped.
CLIENT_FOOTER_RE = re.compile(
    r"^\s*(get outlook for (ios|android)|sent from (my |outlook|yahoo|mail for windows|gmail|samsung|proton)).*$",
    re.IGNORECASE | re.MULTILINE,
)

# At most this many links are tried, in order, stopping at the first that
# yields a recipe. A newsletter with dozens of links shouldn't trigger
# dozens of fetches.
MAX_LINKS_TRIED = 3


def strip_client_boilerplate(text: str) -> str:
    """Drop mail-app footers and anything after a standard "-- " signature
    delimiter, so neither contributes links or stray "ingredient" lines."""
    text = re.split(r"(?m)^-- ?$", text, maxsplit=1)[0]
    return CLIENT_FOOTER_RE.sub("", text)


# An inline image (one without an explicit "attachment" disposition) must
# be at least this long on its longer side to count as a recipe photo.
# Apple Mail -- iPhone and Mac -- marks every photo and PDF it sends as
# "inline", so inline parts can't simply be skipped; this is what separates
# a photo from a signature logo or newsletter banner. 640 px is iOS Mail's
# "Medium" size, the smallest that OCRs usefully; signature graphics sit
# well under it, and email templates are 600 px wide.
MIN_INLINE_PHOTO_EDGE = 640

SUPPORTED_IMAGE_NOTE = "JPEG, PNG, GIF or WebP"


def _leaf_parts(msg):
    """Yield every non-multipart part of a message."""
    if msg.is_multipart():
        for sub in msg.get_payload():
            yield from _leaf_parts(sub)
    else:
        yield msg


def _image_size(payload: bytes):
    """(width, height) from the image header, or None if Pillow can't read
    it. Header only -- no pixel decode, so a huge photo costs nothing."""
    # Magic bytes first, and only the four formats the app accepts: this
    # runs on images from inbound email before any other validation, and a
    # bare Image.open() would hand them to every decoder Pillow has.
    from ..file_validation import detect_image_ext
    if detect_image_ext(payload[:16]) is None:
        return None
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(payload), formats=["JPEG", "PNG", "GIF", "WEBP"]) as im:
            return im.size
    except Exception:
        log.debug("_image_size: caught error, continuing", exc_info=True)
        return None


def _html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for br in soup.find_all(["br"]):
        br.replace_with("\n")
    for block in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        block.append("\n")
    # Links whose visible text isn't the URL (a "Share -> Mail" from an app
    # often looks like this) would otherwise lose the address entirely.
    for a in soup.find_all("a", href=True):
        if a["href"].startswith(("http://", "https://")) and a["href"] not in a.get_text():
            a.append(f" {a['href']}")
    text = soup.get_text()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in text.splitlines()))


def extract_email_parts(msg) -> dict:
    """Pulls out a PDF, an image, the body text, and any http(s) URLs in
    that body from a parsed email.message.Message.

    Which parts count as attachments:
      * anything with Content-Disposition: attachment;
      * a PDF with a filename, whatever its disposition;
      * an inline image with a filename, if it is photo-sized
        (MIN_INLINE_PHOTO_EDGE).
    The previous rule accepted only "attachment". Apple Mail marks photos and
    PDFs "inline", so everything sent from an iPhone was silently ignored.
    That rule came from a real problem -- signature logos being OCR'd -- and
    the size floor still covers it: a logo is small, a recipe photo isn't.

    One PDF and one image are used. Explicit attachments win; among inline
    images the largest wins, so a banner that clears the size floor can't
    beat the actual photo.

    `skipped` lists what was seen and not used, with the reason, so a
    failure notice can say "ignored IMG_0412.HEIC: not JPEG, PNG..." instead
    of "no attachment found".
    """
    pdf_bytes = None
    pdf_name = None
    image_bytes = None
    image_name = None
    inline_images = []  # (pixels, name, payload)
    body_text = ""
    html_text = ""
    skipped = []

    for part in _leaf_parts(msg):
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition") or "").lower()
        explicit = disposition.startswith("attachment") or "attachment" in disposition.split(";")[0]
        filename = _filename(part)
        is_file = explicit or bool(filename)

        is_pdf = content_type == "application/pdf" or (
            content_type == "application/octet-stream" and (filename or "").lower().endswith(".pdf")
        )

        if is_file and is_pdf:
            payload = part.get_payload(decode=True)
            if payload and pdf_bytes is None:
                pdf_bytes, pdf_name = payload, filename or "PDF"
            continue

        if is_file and content_type.startswith("image/"):
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            label = filename or content_type
            if explicit:
                if image_bytes is None:
                    image_bytes, image_name = payload, label
                continue
            size = _image_size(payload)
            if size is None:
                skipped.append(f"{label}: not a supported image ({SUPPORTED_IMAGE_NOTE})")
                continue
            if max(size) < MIN_INLINE_PHOTO_EDGE:
                skipped.append(f"{label}: {size[0]}x{size[1]}, too small to be a recipe photo")
                continue
            inline_images.append((size[0] * size[1], label, payload))
            continue

        if is_file:
            skipped.append(f"{filename or content_type}: {content_type} isn't a PDF or image")
            continue

        if content_type == "text/plain" and not body_text:
            payload = part.get_payload(decode=True)
            if payload:
                body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        elif content_type == "text/html" and not html_text:
            payload = part.get_payload(decode=True)
            if payload:
                html_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")

    if image_bytes is None and inline_images:
        inline_images.sort(key=lambda t: t[0], reverse=True)
        _, image_name, image_bytes = inline_images[0]
        skipped.extend(f"{name}: a larger photo was used instead" for _, name, _ in inline_images[1:])

    # HTML-only mail (some apps' "Share -> Mail") has no text/plain part at all.
    if not body_text.strip() and html_text:
        try:
            body_text = _html_to_text(html_text)
        except Exception:
            log.exception("HTML-to-text conversion failed")
            body_text = ""

    body_text = strip_client_boilerplate(body_text).strip()
    # Deduplicated: the same link often appears twice (as text and as the
    # link target).
    urls = list(dict.fromkeys(u.rstrip(".,;:!?") for u in URL_RE.findall(body_text)))

    return {
        "pdf_bytes": pdf_bytes, "pdf_name": pdf_name,
        "image_bytes": image_bytes, "image_name": image_name,
        "body_text": body_text, "urls": urls, "skipped": skipped,
    }


def _build_result(title, ingredients_raw, steps, raw_text, image_path=None, ocr_confidence=None, source_detail="",
                  source_url=None):
    parsed_ingredients = parse_ingredient_block(ingredients_raw)
    tag_suggestions = suggest_tags(
        title, [i["name"] or i["raw_line"] for i in parsed_ingredients], raw_text
    )
    return {
        "success": True,
        "title": title or "Untitled Recipe",
        "ingredients": parsed_ingredients,
        "steps": steps,
        "tags": [{"name": n, "category": c, "subgroup": s} for n, c, s in tag_suggestions],
        "image_path": image_path,
        "raw_text": raw_text,
        "ocr_confidence": ocr_confidence,
        "source_detail": source_detail,
        # Only a recipe read from a link in the body has one; the recipe page
        # shows it as a "View original" link.
        "source_url": source_url,
    }


def _filename(part) -> str | None:
    """The attachment's file name, decoded. Apple Mail sends non-ASCII names
    as RFC 2047 encoded words ("=?utf-8?B?...?="), which the message
    parser used here leaves as-is: the name in the log and in failure
    messages came out as that encoded string."""
    name = part.get_filename()
    if name and "=?" in name:
        try:
            from email.header import decode_header, make_header
            name = str(make_header(decode_header(name)))
        except Exception:
            log.debug("Couldn't decode attachment name %r", name, exc_info=True)
    return name


# Email has no review screen, so OCR this unsure is refused rather than
# saved as a recipe of garbled words (a photo at an angle, handwriting).
# The app's "OCR quality: low" badge starts at 55; this is below that.
MIN_EMAIL_OCR_CONFIDENCE = 50


def _ocr_too_poor(confidence) -> bool:
    return confidence is not None and confidence < MIN_EMAIL_OCR_CONFIDENCE


def _failure(reason: str) -> dict:
    return {"success": False, "error": reason}


def process_tagged_email(msg, tmp_dir: str) -> dict:
    """Attempts extraction in order: PDF attachment, image attachment, a
    single clear URL in the body, then the body text itself. Each path
    reuses the same ingestion pipeline as its manual-entry equivalent.
    Never raises for an ordinary parse failure -- returns a result dict
    with success=False and a reason instead, so one bad email in a scan
    doesn't take down the rest.

    The failure reason lists every path tried and why each gave up. It
    used to be one fixed sentence ("no attachment, link, or recognizable
    body text") even when there WAS a link that failed to load or a photo
    that OCR'd to nothing -- which made a real problem look like a user
    error."""
    try:
        parts = extract_email_parts(msg)
    except Exception as e:
        log.exception("Could not parse email structure")
        return _failure(f"Could not read email content: {e}")

    # The MIME layout as the app saw it. Most "why wasn't this picked up"
    # questions are answered by this one line.
    layout = []
    for part in _leaf_parts(msg):
        payload = part.get_payload(decode=True) or b""
        disp = str(part.get("Content-Disposition") or "-").split(";")[0]
        layout.append(f"{part.get_content_type()}[{disp}"
                      f"{', ' + _filename(part) if _filename(part) else ''}, {len(payload)}B]")
    log.info("Parts: %s", " ".join(layout) or "(none)")
    log.info("Found: pdf=%s image=%s urls=%s body=%d chars; skipped=%s",
             parts["pdf_name"], parts["image_name"], parts["urls"], len(parts["body_text"]), parts["skipped"])

    name_prefix = f"email-{uuid.uuid4().hex}"
    tried = list(parts["skipped"])
    # A PDF or photo that gave only ingredients or only steps (a page cut off
    # before its recipe card, one screenshot of several).
    incomplete_hint = False

    # 1. PDF attachment
    if parts["pdf_bytes"]:
        label = parts["pdf_name"]
        pdf_name = validate_and_save_pdf_bytes(parts["pdf_bytes"], tmp_dir, name_prefix)
        if not pdf_name:
            tried.append(f"{label}: not a valid PDF, or over the size limit")
        else:
            pdf_path = os.path.join(tmp_dir, pdf_name)
            try:
                pdf_result = extract_pdf_text(pdf_path)
            except PdfTooLargeError as e:
                if os.path.isfile(pdf_path):
                    os.remove(pdf_path)
                return _failure(str(e))
            except Exception as e:
                log.exception("PDF attachment %s: extraction failed", label)
                if os.path.isfile(pdf_path):
                    os.remove(pdf_path)
                return _failure(f"Could not read PDF attachment: {e}")

            segmented = segment_raw_text(pdf_result.raw_text)
            os.remove(pdf_path)  # attachment's job is done once text is extracted; not kept as the showcase image
            if pdf_result.avg_ocr_confidence is not None:
                log.info("PDF %s: OCR confidence %.0f", label, pdf_result.avg_ocr_confidence)
            # Only a fully scanned PDF is judged on its OCR: a page with a
            # real text layer is reliable however the scanned pages went.
            fully_scanned = pdf_result.page_count and len(pdf_result.ocr_used_on_pages) == pdf_result.page_count
            if fully_scanned and _ocr_too_poor(pdf_result.avg_ocr_confidence):
                tried.append(f"{label}: the scan couldn't be read reliably "
                             f"(text recognition confidence {pdf_result.avg_ocr_confidence:.0f}%)")
            elif not missing_sections(segmented["ingredients"], segmented["steps"]):
                return _build_result(
                    segmented["title_guess"], segmented["ingredients"], segmented["steps"],
                    pdf_result.raw_text, ocr_confidence=pdf_result.avg_ocr_confidence,
                    source_detail="PDF attachment",
                )
            elif segmented["ingredients"] or segmented["steps"]:
                incomplete_hint = True
                tried.append(f"{label}: only part of a recipe "
                             f"(no {' or '.join(missing_sections(segmented['ingredients'], segmented['steps']))} found)")
            else:
                tried.append(f"{label}: no ingredient or step lines found in its text")

    # 2. Image attachment
    if parts["image_bytes"]:
        label = parts["image_name"]
        image_name = validate_and_save_image_bytes(parts["image_bytes"], tmp_dir, name_prefix)
        if not image_name:
            tried.append(f"{label}: not a supported image ({SUPPORTED_IMAGE_NOTE}), or over the size limit")
        else:
            image_path = os.path.join(tmp_dir, image_name)
            try:
                ocr_result = ingest_image(image_path)
            except Exception as e:
                log.exception("Image %s: OCR failed", label)
                os.remove(image_path)
                return _failure(f"Could not read image attachment: {e}")

            segmented = segment_raw_text(ocr_result.raw_text)
            if ocr_result.ocr_confidence is not None:
                log.info("Image %s: OCR confidence %.0f", label, ocr_result.ocr_confidence)
            if _ocr_too_poor(ocr_result.ocr_confidence):
                os.remove(image_path)
                tried.append(f"{label}: the photo couldn't be read reliably "
                             f"(text recognition confidence {ocr_result.ocr_confidence:.0f}%); "
                             "a sharper, straight-on photo or the typed text works better")
            elif not missing_sections(segmented["ingredients"], segmented["steps"]):
                return _build_result(
                    segmented["title_guess"], segmented["ingredients"], segmented["steps"],
                    ocr_result.raw_text, image_path=image_name, ocr_confidence=ocr_result.ocr_confidence,
                    source_detail="image attachment",
                )
            elif segmented["ingredients"] or segmented["steps"]:
                os.remove(image_path)
                incomplete_hint = True
                tried.append(f"{label}: only part of a recipe "
                             f"(no {' or '.join(missing_sections(segmented['ingredients'], segmented['steps']))} found)")
            else:
                os.remove(image_path)  # nothing usable came from it; don't leave it as an orphaned draft file
                tried.append(f"{label}: text recognition found no ingredient or step lines")

    # 3. A single clear URL in the body -- hand off to the existing,
    # SSRF-guarded URL ingestion pipeline rather than trying to parse
    # marketing/newsletter HTML directly.
    # Reaching here means no attachment produced a usable recipe -- both
    # attachment branches above return on success. Gating on whether
    # attachment *bytes* existed (rather than whether they worked) would
    # let an unparseable PDF or a decorative signature image block the
    # documented "attachment -> link -> body" fallback chain at step one.
    # Links are tried in order, first success wins. This used to require
    # exactly one link and skip the step otherwise -- which, with Outlook's
    # footer link, meant any email sent from Outlook for iOS.
    pdf_hint = False
    if len(parts["urls"]) > MAX_LINKS_TRIED:
        tried.append(f"{len(parts['urls'])} links in the body; at most {MAX_LINKS_TRIED} are followed")
    else:
        for url in parts["urls"]:
            try:
                url_result = require_complete(ingest_url(url))
                return _build_result(
                    url_result.title, url_result.ingredients, url_result.steps,
                    url_result.raw_text, source_detail=f"URL in body ({url})", source_url=url,
                )
            except Exception as e:
                # Full traceback for the unexpected; the URL checks and a
                # site's refusal are already explained in one line.
                log.warning("Link %s failed: %s", url, e, exc_info=not isinstance(e, UrlValidationError))
                if isinstance(e, (NoRecipeFoundError, IncompleteRecipeError, SiteRefusedError)):
                    pdf_hint = True
                    # The "add that instead" advice is for the app's own
                    # screens; the email gets its own version below.
                    reason_only = re.sub(r"\s*(?:Sites with a bot check do this; s|S)ave the page as a PDF.*$", "", str(e))
                    tried.append(f"link {url}: {reason_only}")
                else:
                    tried.append(f"link {url}: {e}")

    # 4. Last resort: treat the body text itself as the recipe.
    if parts["body_text"]:
        segmented = segment_raw_text(parts["body_text"])
        if not missing_sections(segmented["ingredients"], segmented["steps"]):
            return _build_result(
                segmented["title_guess"], segmented["ingredients"], segmented["steps"],
                parts["body_text"], source_detail="email body text",
            )
        if segmented["ingredients"] or segmented["steps"]:
            tried.append("body text: only part of a recipe "
                         f"(no {' or '.join(missing_sections(segmented['ingredients'], segmented['steps']))} found)")
        else:
            tried.append("body text: no ingredient or step lines")
    else:
        tried.append("body: empty")

    if not (parts["pdf_bytes"] or parts["image_bytes"] or parts["urls"]):
        tried.insert(0, "no PDF, photo or link in the email")
    reason = "No recipe found. Tried: " + "; ".join(tried) + "."
    if pdf_hint:
        reason += (" To add it, save the page as a PDF and email that as an attachment, "
                   "with the same word in the subject.")
    elif incomplete_hint:
        reason += (" If the PDF or photo stops before the recipe card, send one that includes it. "
                   "Otherwise add the recipe in the app, from screenshots or by hand.")
    log.warning(reason)
    return _failure(reason)
