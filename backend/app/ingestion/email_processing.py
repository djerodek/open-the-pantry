import os
import re
import uuid

from .pdf_ingest import extract_pdf_text, segment_raw_text, PdfTooLargeError
from .image_ingest import ingest_image
from .url_ingest import ingest_url
from .ingredient_parser import parse_ingredient_block
from .tagger import suggest_tags
from ..file_validation import validate_and_save_pdf_bytes, validate_and_save_image_bytes

URL_RE = re.compile(r'https?://[^\s<>"\')]+')


def extract_email_parts(msg) -> dict:
    """Pulls out a PDF attachment, an image attachment, the plain-text
    body, and any http(s) URLs found in that body from a parsed
    email.message.Message. Only the first attachment of each kind is
    used -- a tagged email is expected to be about one recipe."""
    pdf_bytes = None
    image_bytes = None
    body_text = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition") or "")
            content_type = part.get_content_type()
            is_attachment = "attachment" in content_disposition or bool(part.get_filename())

            if is_attachment:
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                if content_type == "application/pdf" and pdf_bytes is None:
                    pdf_bytes = payload
                elif content_type.startswith("image/") and image_bytes is None:
                    image_bytes = payload
            elif content_type == "text/plain" and not body_text:
                payload = part.get_payload(decode=True)
                if payload:
                    body_text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body_text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    body_text = body_text.strip()
    urls = URL_RE.findall(body_text)

    return {"pdf_bytes": pdf_bytes, "image_bytes": image_bytes, "body_text": body_text, "urls": urls}


def _build_result(title, ingredients_raw, steps, raw_text, image_path=None, ocr_confidence=None, source_detail=""):
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
    }


def _failure(reason: str) -> dict:
    return {"success": False, "error": reason}


def process_tagged_email(msg, tmp_dir: str) -> dict:
    """Attempts extraction in order: PDF attachment, image attachment, a
    single clear URL in the body, then the body text itself. Each path
    reuses the same ingestion pipeline as its manual-entry equivalent.
    Never raises for an ordinary parse failure -- returns a result dict
    with success=False and a reason instead, so one bad email in a scan
    doesn't take down the rest."""
    try:
        parts = extract_email_parts(msg)
    except Exception as e:
        return _failure(f"Could not read email content: {e}")

    name_prefix = f"email-{uuid.uuid4().hex}"

    # 1. PDF attachment
    if parts["pdf_bytes"]:
        pdf_name = validate_and_save_pdf_bytes(parts["pdf_bytes"], tmp_dir, name_prefix)
        if pdf_name:
            pdf_path = os.path.join(tmp_dir, pdf_name)
            try:
                pdf_result = extract_pdf_text(pdf_path)
            except PdfTooLargeError as e:
                if os.path.isfile(pdf_path):
                    os.remove(pdf_path)
                return _failure(str(e))
            except Exception as e:
                if os.path.isfile(pdf_path):
                    os.remove(pdf_path)
                return _failure(f"Could not read PDF attachment: {e}")

            segmented = segment_raw_text(pdf_result.raw_text)
            os.remove(pdf_path)  # attachment's job is done once text is extracted; not kept as the showcase image
            if segmented["ingredients"] or segmented["steps"]:
                return _build_result(
                    segmented["title_guess"], segmented["ingredients"], segmented["steps"],
                    pdf_result.raw_text, ocr_confidence=pdf_result.avg_ocr_confidence,
                    source_detail="PDF attachment",
                )
            # fall through to other paths if the PDF had nothing usable

    # 2. Image attachment
    if parts["image_bytes"]:
        image_name = validate_and_save_image_bytes(parts["image_bytes"], tmp_dir, name_prefix)
        if image_name:
            image_path = os.path.join(tmp_dir, image_name)
            try:
                ocr_result = ingest_image(image_path)
            except Exception as e:
                os.remove(image_path)
                return _failure(f"Could not read image attachment: {e}")

            segmented = segment_raw_text(ocr_result.raw_text)
            if segmented["ingredients"] or segmented["steps"]:
                return _build_result(
                    segmented["title_guess"], segmented["ingredients"], segmented["steps"],
                    ocr_result.raw_text, image_path=image_name, ocr_confidence=ocr_result.ocr_confidence,
                    source_detail="image attachment",
                )
            os.remove(image_path)  # nothing usable came from it; don't leave it as an orphaned draft file

    # 3. A single clear URL in the body -- hand off to the existing,
    # SSRF-guarded URL ingestion pipeline rather than trying to parse
    # marketing/newsletter HTML directly.
    # Reaching here means no attachment produced a usable recipe -- both
    # attachment branches above return on success. Gating on whether
    # attachment *bytes* existed (rather than whether they worked) would
    # let an unparseable PDF or a decorative signature image block the
    # documented "attachment -> link -> body" fallback chain at step one.
    if len(parts["urls"]) == 1:
        try:
            url_result = ingest_url(parts["urls"][0])
            return _build_result(
                url_result.title, url_result.ingredients, url_result.steps,
                url_result.raw_text, source_detail=f"URL in body ({parts['urls'][0]})",
            )
        except Exception:
            pass  # fall through to plain body-text parsing

    # 4. Last resort: treat the body text itself as the recipe.
    if parts["body_text"]:
        segmented = segment_raw_text(parts["body_text"])
        if segmented["ingredients"] or segmented["steps"]:
            return _build_result(
                segmented["title_guess"], segmented["ingredients"], segmented["steps"],
                parts["body_text"], source_detail="email body text",
            )

    return _failure("Could not find a recipe in this email (no attachment, link, or recognizable body text).")
