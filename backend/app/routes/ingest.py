"""Add Recipe: link, PDF, photo and combined-photo imports, single and batch. Each returns a draft
for the review screen, or saves to the review queue for the batch paths."""
import os
import uuid
from fastapi import Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session
from .. import models, schemas
from ..database import get_db, UPLOADS_DIR, TMP_DIR
from ..ingestion.completeness import incomplete_info, incomplete_message, missing_parts as missing_sections
from ..ingestion.url_ingest import ingest_url, log_url, UrlValidationError, IncompleteRecipeError, require_complete, missing_parts
from ..ingestion.pdf_ingest import extract_pdf_text, segment_raw_text, PdfTooLargeError
from ..ingestion.image_ingest import ingest_and_segment, ingest_image
from ..ingestion.ingredient_parser import parse_ingredient_block
from ..ingestion.tagger import suggest_tags, explain_tags
from ..backup import remove_upload_file
from .. import duplicates
from ..file_validation import is_safe_stored_filename, safe_join
from .common import _apply_tags, _download_and_save_showcase_image, _extract_pdf_showcase_image, _run_heavy, _save_upload_streaming
from fastapi import APIRouter
from ..logging_setup import get_logger

log = get_logger("app")
router = APIRouter()


# ---------------------------------------------------------------------------
# Ingestion endpoints -- each returns a *draft* structure for the manual-review
# screen; files are written to TMP_DIR and nothing is saved to the DB (or
# promoted to permanent storage) until the user confirms via POST /api/recipes.
#
# Note these are plain `def`, not `async def`: they do blocking file I/O and
# CPU-bound work (pdfplumber, Tesseract OCR, OpenCV). FastAPI runs sync path
# functions in a worker thread pool automatically: an `async def` here would
# instead run this blocking work directly on the single event loop thread,
# stalling every other concurrent request for the duration.
# ---------------------------------------------------------------------------

@router.post("/api/ingest/url")
def ingest_from_url(payload: schemas.UrlIngestRequest):
    try:
        result = ingest_url(payload.url)
    except Exception as e:
        log.warning("Add from URL failed for %s: %s", log_url(payload.url), e,
                    exc_info=not isinstance(e, UrlValidationError))
        raise HTTPException(status_code=422, detail=str(e))

    # A half recipe (no ingredients, or no steps) is reported as a failed
    # import, with the partial draft attached so "Continue anyway" can still
    # open it in the review screen.
    incomplete = None
    try:
        require_complete(result)
    except IncompleteRecipeError as e:
        log.info("Add from URL: incomplete recipe from %s: %s", log_url(payload.url), e)
        incomplete = {"missing": missing_parts(result), "message": str(e)}

    parsed_ingredients = parse_ingredient_block(result.ingredients)
    tag_suggestions = explain_tags(
        result.title, [i["name"] or i["raw_line"] for i in parsed_ingredients], result.raw_text
    )

    # Best-effort showcase image download -- a failure here (broken image
    # URL, oversized, not actually an image, etc.) shouldn't fail the
    # whole ingestion, so the recipe's text content is returned either way.
    image_path = None
    if result.image_url:
        image_path = _download_and_save_showcase_image(result.image_url, TMP_DIR, f"url-img-{uuid.uuid4().hex}")

    return {
        "title": result.title,
        "source_type": "url",
        "source_url": payload.url,
        "servings": result.servings,
        "prep_time": result.prep_time,
        "cook_time": result.cook_time,
        "total_time": result.total_time,
        "image_url": result.image_url,
        "image_path": image_path,  # draft reference; promoted on save, same as screenshot drafts
        "raw_text": result.raw_text,
        "ingredients": parsed_ingredients,
        "steps": result.steps,
        "suggested_tags": tag_suggestions,     # with where each came from (tagger.explain_tags)
        "extraction_method": result.method,
        "ocr_confidence": None,
        "incomplete": incomplete,
    }


# Per-request caps on the batch endpoints. Each item is a network fetch or
# a PDF parse (possibly OCR), processed one after another inside a single
# request, so an unbounded list is an unbounded request. Generous for a
# person pasting a reading list; the frontend checks the same numbers.
MAX_BATCH_URLS = 50
MAX_BATCH_PDFS = 20
# Total size of one PDF batch. The count cap alone still allowed 20 x 20 MB.
# Checked after the upload has arrived (Starlette spools it to disk first),
# so this bounds the work done, not the bytes received -- a size limit on
# the request itself belongs in a reverse proxy, if one is in front.
MAX_BATCH_PDF_BYTES = 100 * 1024 * 1024


@router.post("/api/ingest/url/batch")
def ingest_from_url_batch(payload: schemas.BatchUrlIngestRequest, db: Session = Depends(get_db)):
    """Batch add: unlike the single-URL path, results are saved directly
    (using the auto-parsed fields and auto-suggested tags as-is) rather than
    routed through the manual-review screen -- reviewing N items one at a
    time isn't really a batch operation. Failures are reported per-URL so
    they can be retried individually through the reviewed single-URL flow."""
    if len([u for u in payload.urls if u.strip()]) > MAX_BATCH_URLS:
        raise HTTPException(status_code=400, detail=f"Too many links in one batch (max {MAX_BATCH_URLS}). Split the list and send the rest separately.")
    succeeded, failed, skipped = [], [], []
    for url in payload.urls:
        url = url.strip()
        if not url:
            continue
        image_path = None
        try:
            # Already saved from this link: not fetched or saved again.
            # Inside the try, so a failure here is this link's failure, not
            # the batch's.
            existing = duplicates.same_link(db, url)
            if existing:
                skipped.append({"url": url, "recipe_id": existing["id"], "title": existing["title"]})
                continue
            result = require_complete(ingest_url(url))
            same_title = duplicates.find(db, title=result.title, limit=1)
            parsed_ingredients = parse_ingredient_block(result.ingredients)
            tag_suggestions = suggest_tags(
                result.title, [i["name"] or i["raw_line"] for i in parsed_ingredients], result.raw_text
            )
            if result.image_url:
                image_path = _download_and_save_showcase_image(result.image_url, UPLOADS_DIR, f"url-img-{uuid.uuid4().hex}")
            fields, parsed_ingredients, steps = schemas.fit_recipe(
                {"title": result.title, "source_url": url, "servings": result.servings, "prep_time": result.prep_time,
                 "cook_time": result.cook_time, "total_time": result.total_time, "raw_text": result.raw_text},
                parsed_ingredients, result.steps)
            recipe = models.Recipe(**fields, source_type="url", image_path=image_path, needs_review=True)
            db.add(recipe)
            db.flush()
            for pos, ing in enumerate(parsed_ingredients):
                db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **ing))
            for pos, step in enumerate(steps):
                db.add(models.Step(recipe_id=recipe.id, position=pos, text=step))
            tag_ins = [schemas.TagIn(name=n, category=c, subgroup=s) for n, c, s in tag_suggestions]
            _apply_tags(db, recipe, tag_ins)
            db.commit()
            succeeded.append({"url": url, "recipe_id": recipe.id, "title": result.title,
                              "duplicate_of": same_title[0] if same_title else None})
        except Exception as e:
            log.warning("Batch URL %s failed: %s", log_url(url), e, exc_info=not isinstance(e, UrlValidationError))
            db.rollback()
            if image_path:
                stray_path = safe_join(UPLOADS_DIR, image_path)
                if stray_path and os.path.isfile(stray_path):
                    remove_upload_file(stray_path)
            failed.append({"url": url, "error": str(e)})
    return {"succeeded": succeeded, "failed": failed, "skipped": skipped}


@router.post("/api/ingest/pdf")
def ingest_from_pdf(file: UploadFile = File(...)):
    name_prefix = f"pdf-{uuid.uuid4().hex}"
    temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="pdf")
    temp_path = os.path.join(TMP_DIR, temp_name)

    try:
        pdf_result = _run_heavy(extract_pdf_text, temp_path)
    except PdfTooLargeError as e:
        os.remove(temp_path)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        # Any other failure: remove the upload now rather than leaving it
        # for the 24-hour sweep.
        log.warning("ingest_from_pdf: request failed", exc_info=True)
        if os.path.isfile(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=400, detail="Could not read this PDF.")

    segmented = segment_raw_text(pdf_result.raw_text)
    parsed_ingredients = parse_ingredient_block(segmented["ingredients"])
    tag_suggestions = explain_tags(
        segmented["title_guess"], [i["name"] or i["raw_line"] for i in parsed_ingredients],
        pdf_result.raw_text,
    )

    # Best-effort: pull out the largest embedded image as a showcase photo.
    # A failure here never fails the ingestion -- the recipe's text content
    # is returned either way.
    image_path = _run_heavy(_extract_pdf_showcase_image, temp_path, TMP_DIR, f"pdf-img-{uuid.uuid4().hex}")

    # No ingredients or no steps: reported as a failed import, with the
    # draft attached so "Continue anyway" can still open it.
    incomplete = incomplete_info("pdf", segmented["ingredients"], segmented["steps"])
    if incomplete:
        log.info("Add from PDF: incomplete recipe from %s: %s", file.filename, incomplete["message"])

    return {
        "incomplete": incomplete,
        "title": segmented["title_guess"],
        "source_type": "pdf",
        "servings": segmented.get("servings"),
        "raw_text": pdf_result.raw_text,
        "image_path": image_path,  # draft reference; promoted on save
        "ingredients": parsed_ingredients,
        "steps": [{"text": s} for s in segmented["steps"]],
        "suggested_tags": tag_suggestions,     # with where each came from (tagger.explain_tags)
        "ocr_used_on_pages": pdf_result.ocr_used_on_pages,
        "ocr_confidence": pdf_result.avg_ocr_confidence,
        "stored_file": temp_name,  # a draft reference; promoted to UPLOADS_DIR only on save
    }


# Batch results are a list of lines, with no buttons to offer, so the
# other ways to add the recipe go in the message itself.
BATCH_INCOMPLETE_ADVICE = (" Add it on its own from screenshots of the recipe "
                           "(Screenshot/Photo), or enter it by hand.")


@router.post("/api/ingest/pdf/batch")
def ingest_from_pdf_batch(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    """Batch add for PDFs: auto-saves each file directly, same rationale as
    the URL batch endpoint. The source PDF itself isn't kept as an image
    attachment here (only screenshot/manual entries carry an image) -- only
    its extracted content is saved."""
    if len(files) > MAX_BATCH_PDFS:
        raise HTTPException(status_code=400, detail=f"Too many PDFs in one batch (max {MAX_BATCH_PDFS}). Send the rest separately.")
    total = 0
    for f in files:
        size = getattr(f, "size", None)
        if size is None:
            f.file.seek(0, os.SEEK_END); size = f.file.tell(); f.file.seek(0)
        total += size
    if total > MAX_BATCH_PDF_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"These PDFs total {total / 1048576:.0f} MB; one batch can be at most "
                   f"{MAX_BATCH_PDF_BYTES // 1048576} MB. Send them in smaller groups.",
        )
    succeeded, failed = [], []
    for file in files:
        name_prefix = f"pdf-{uuid.uuid4().hex}"
        temp_path = None
        try:
            temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="pdf")
            temp_path = os.path.join(TMP_DIR, temp_name)
            pdf_result = _run_heavy(extract_pdf_text, temp_path)
            segmented = segment_raw_text(pdf_result.raw_text)
            missing = missing_sections(segmented["ingredients"], segmented["steps"])
            if missing:
                log.info("PDF batch: incomplete recipe from %s (no %s)", file.filename, " or ".join(missing))
                failed.append({"filename": file.filename, "error": incomplete_message("pdf", missing) + BATCH_INCOMPLETE_ADVICE})
                continue
            parsed_ingredients = parse_ingredient_block(segmented["ingredients"])
            tag_suggestions = suggest_tags(
                segmented["title_guess"], [i["name"] or i["raw_line"] for i in parsed_ingredients],
                pdf_result.raw_text,
            )
            same_title = duplicates.find(db, title=segmented["title_guess"], limit=1)
            fields, parsed_ingredients, steps = schemas.fit_recipe(
                {"title": segmented["title_guess"], "servings": segmented.get("servings"),
                 "raw_text": pdf_result.raw_text}, parsed_ingredients, segmented["steps"])
            recipe = models.Recipe(**fields, source_type="pdf", ocr_confidence=pdf_result.avg_ocr_confidence,
                                   needs_review=True)
            db.add(recipe)
            db.flush()
            for pos, ing in enumerate(parsed_ingredients):
                db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **ing))
            for pos, step in enumerate(steps):
                db.add(models.Step(recipe_id=recipe.id, position=pos, text=step))
            tag_ins = [schemas.TagIn(name=n, category=c, subgroup=s) for n, c, s in tag_suggestions]
            _apply_tags(db, recipe, tag_ins)
            db.commit()
            succeeded.append({"filename": file.filename, "recipe_id": recipe.id, "title": recipe.title,
                              "duplicate_of": same_title[0] if same_title else None})
        except HTTPException as e:
            db.rollback()
            failed.append({"filename": file.filename, "error": e.detail})
        except Exception as e:
            log.warning("ingest_from_pdf_batch: caught error, continuing", exc_info=True)
            db.rollback()
            failed.append({"filename": file.filename, "error": str(e)})
        finally:
            if temp_path and os.path.isfile(temp_path):
                os.remove(temp_path)  # batch mode never keeps the source file
    return {"succeeded": succeeded, "failed": failed}


@router.post("/api/ingest/image")
def ingest_from_image(file: UploadFile = File(...)):
    name_prefix = f"img-{uuid.uuid4().hex}"
    temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="image")
    temp_path = os.path.join(TMP_DIR, temp_name)

    try:
        segmented = _run_heavy(ingest_and_segment, temp_path)
    except Exception:
        # The upload is kept on success (it becomes the draft's image), but
        # a failed OCR leaves nothing to keep it for.
        log.warning("ingest_from_image: request failed", exc_info=True)
        if os.path.isfile(temp_path):
            os.remove(temp_path)
        raise HTTPException(status_code=400, detail="Could not read text from this image.")
    parsed_ingredients = parse_ingredient_block(segmented["ingredients"])
    tag_suggestions = explain_tags(
        segmented["title_guess"], [i["name"] or i["raw_line"] for i in parsed_ingredients],
        segmented["raw_text"],
    )

    incomplete = incomplete_info("image", segmented["ingredients"], segmented["steps"])
    if incomplete:
        log.info("Add from photo: incomplete recipe from %s: %s", file.filename, incomplete["message"])

    return {
        "incomplete": incomplete,
        "title": segmented["title_guess"],
        "source_type": "screenshot",
        "servings": segmented.get("servings"),
        "raw_text": segmented["raw_text"],
        "ingredients": parsed_ingredients,
        "steps": [{"text": s} for s in segmented["steps"]],
        "suggested_tags": tag_suggestions,     # with where each came from (tagger.explain_tags)
        "ocr_confidence": segmented["ocr_confidence"],
        "stored_file": temp_name,
        "image_path": temp_name,  # draft reference; promoted on save
    }


MAX_COMBINED_IMAGES = 10  # generous for a multi-screenshot recipe, bounds worst-case single-request OCR time


@router.post("/api/ingest/images")
def ingest_from_images(files: list[UploadFile] = File(...)):
    """Combine multiple screenshots of the same recipe (e.g. a recipe that
    spans several screenshots, photographed a page at a time) into a single
    draft. Each image is OCR'd independently, in the order submitted --
    order isn't reconstructed or reordered, so submit them in reading
    order -- then their extracted text is concatenated before segmentation
    runs once over the combined whole. This always produces ONE draft for
    review, unlike /api/ingest/pdf/batch or /api/ingest/url/batch (which
    auto-save N separate recipes) -- the entire point here is combining
    fragments into a single recipe, so it goes through the same
    manual-review screen as any other single-item ingestion.

    The showcase image defaults to the first image submitted -- there's no
    reliable way to know which one is "the" photo, and this can be changed
    afterward from the recipe detail page either way."""
    if not files:
        raise HTTPException(status_code=400, detail="No images provided.")
    if len(files) > MAX_COMBINED_IMAGES:
        raise HTTPException(status_code=400, detail=f"Too many images (max {MAX_COMBINED_IMAGES}).")

    temp_names = []
    raw_texts = []
    confidences = []
    try:
        for file in files:
            name_prefix = f"img-{uuid.uuid4().hex}"
            temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="image")
            temp_names.append(temp_name)
            result = _run_heavy(ingest_image, os.path.join(TMP_DIR, temp_name))
            raw_texts.append(result.raw_text)
            if result.ocr_confidence is not None:
                confidences.append(result.ocr_confidence)
    except Exception as e:
        # A bad file partway through aborts the whole combined draft rather
        # than silently combining a partial set -- clean up anything
        # already saved before re-raising. Any exception, not only the
        # validation HTTPException: an OCR crash left the files behind.
        log.warning("ingest_from_images: request failed", exc_info=True)
        for name in temp_names:
            path = os.path.join(TMP_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(status_code=400, detail="Could not read text from one of these images.")

    combined_raw_text = "\n\n".join(raw_texts)
    segmented = segment_raw_text(combined_raw_text)
    parsed_ingredients = parse_ingredient_block(segmented["ingredients"])
    tag_suggestions = explain_tags(
        segmented["title_guess"], [i["name"] or i["raw_line"] for i in parsed_ingredients], combined_raw_text
    )
    avg_confidence = sum(confidences) / len(confidences) if confidences else None

    # Only the first image is kept as the showcase image (agreed default);
    # discard the rest now rather than leaving them as orphaned drafts.
    showcase_image = temp_names[0]
    for name in temp_names[1:]:
        path = os.path.join(TMP_DIR, name)
        if os.path.isfile(path):
            os.remove(path)

    incomplete = incomplete_info("images", segmented["ingredients"], segmented["steps"])
    if incomplete:
        log.info("Add from photos: incomplete recipe from %d images: %s", len(files), incomplete["message"])

    return {
        "incomplete": incomplete,
        "title": segmented["title_guess"],
        "source_type": "screenshot",
        "servings": segmented.get("servings"),
        "raw_text": combined_raw_text,
        "ingredients": parsed_ingredients,
        "steps": [{"text": s} for s in segmented["steps"]],
        "suggested_tags": tag_suggestions,     # with where each came from (tagger.explain_tags)
        "ocr_confidence": avg_confidence,
        "stored_file": showcase_image,
        "image_path": showcase_image,
        "image_count": len(files),
    }


@router.post("/api/upload-image")
def upload_image(file: UploadFile = File(...)):
    """Standalone image upload used by the manual-entry form to attach a photo
    of a handwritten card. Written to TMP_DIR like other ingestion drafts --
    promoted to permanent storage only if the recipe is actually saved."""
    name_prefix = f"manual-{uuid.uuid4().hex}"
    temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="image")
    return {"stored_file": temp_name}


@router.delete("/api/ingest/draft/{filename}")
def discard_draft_file(filename: str):
    """Called by the frontend when the add-recipe modal is closed without
    saving, so an uploaded PDF/screenshot/photo doesn't linger on disk with
    nothing in the database referencing it."""
    safe_name = os.path.basename(filename)  # no path traversal
    # Only names this app gives drafts, as everywhere else a name meets the
    # disk (DeepSeek review of 0073).
    if not is_safe_stored_filename(safe_name):
        return {"ok": False}
    path = os.path.join(TMP_DIR, safe_name)
    if os.path.isfile(path):
        os.remove(path)
    return {"ok": True}
