"""Helpers the route modules share: upload files and drafts, tags, recipe files, times, the
heavy-job gate, and the restore state the main app's middleware keeps."""
import re
import os
import shutil
import threading
from datetime import datetime, timezone
from fastapi import HTTPException, UploadFile, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from .. import models, schemas
from ..database import SessionLocal, DATA_DIR, UPLOADS_DIR, TMP_DIR
from ..ingestion.url_ingest import safe_get
from ..ingestion.pdf_ingest import extract_largest_embedded_image
from ..ingestion.ingredient_parser import parse_ingredient_block
from ..ingestion.tagger import set_user_keywords
from ..backup import remove_upload_file
from ..time_utils import ddhhmm_to_minutes
from .. import https_setup
from .. import admin_lock
from ..ml import service as learning
from ..file_validation import MAX_UPLOAD_BYTES, PDF_MAGIC, reencode_image, validate_and_save_image_bytes, detect_image_ext, is_safe_stored_filename, safe_join
from ..logging_setup import get_logger

log = get_logger("app")


def _split_keywords(raw: str) -> list[str]:
    return [k.strip().lower() for k in re.split(r"[,;\n]", raw or "") if k.strip()]


def refresh_user_keywords(db=None):
    """Hand the tagger the keywords set in Settings -> Tag groups."""
    own = db is None
    db = db or SessionLocal()
    try:
        rows = db.query(models.Tag).filter(
            (models.Tag.keywords != "") | (models.Tag.rules_out_vegetarian.is_(True))).all()
        set_user_keywords([(t.name, t.category, t.subgroup, _split_keywords(t.keywords), bool(t.rules_out_vegetarian))
                           for t in rows])
        # The learned tagger only suggests tags that still exist (a deleted
        # tag would otherwise be created again, until the next retrain).
        learning.refresh_known_tags(lambda: db.query(models.Tag.name, models.Tag.category).all())
    finally:
        if own:
            db.close()

# Bounds concurrent Tesseract/pdfplumber work. This isn't primarily about
# thread-pool exhaustion (the pool size bump in lifespan() below covers
# that) -- it's that running many OCR jobs at once just thrashes CPU
# between them without actually finishing any of them faster. A personal
# self-hosted tool has no legitimate reason to run more than a few at once.
MAX_CONCURRENT_HEAVY_JOBS = 3
_heavy_job_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_HEAVY_JOBS)
learning.set_heavy_gate(_heavy_job_semaphore)


def _run_heavy(fn, *args, **kwargs):
    with _heavy_job_semaphore:
        return fn(*args, **kwargs)


# Backup archives are staged here, NOT in TMP_DIR. TMP_DIR is mounted at
# /tmp-preview so the review screen can show a draft's captured image, which
# would put a complete copy of the database under a publicly reachable path.
# The filenames are unguessable either way, but there is no reason to serve
# them at all.
BACKUP_TMP_DIR = os.path.join(DATA_DIR, "backup-tmp")
os.makedirs(BACKUP_TMP_DIR, exist_ok=True)


# Settings -> Backup & export -> Restore: while a backup is put back, every
# other request on either port gets a 503 (the database and photos are
# being replaced under them), and the restore waits for requests already
# running to finish before it starts. (restore_backup)
_restoring = threading.Event()
# A restore that failed after the library was replaced and couldn't finish
# its remaining steps: _restoring stays set (so the sweeps, scans and
# automatic backups stay off) and every request gets this 503 until the app
# is restarted, whose start-up recovery finishes the photos. Serving the
# backup's database with the old photos would show missing photos, and the
# orphaned-photo sweep would delete the old ones (GPT review of 0155).
_restore_incomplete = threading.Event()
RESTORE_INCOMPLETE_MESSAGE = ("A restore stopped after the library had been replaced and couldn't finish. "
                              "Restart the app (docker compose restart): it finishes the restore when it starts. "
                              "The library from before is in the safety backup (restore-safety-*.zip) in the data folder.")
_inflight = 0
_inflight_lock = threading.Lock()


def _https_only_url(request: Request) -> str | None:
    """Once the app's own HTTPS is on, the settings password, its cookie and
    the locked pages are refused over plain http:// (anyone on the Wi-Fi
    could read them there), except from inside the container (loopback;
    a browser on the host comes through Docker's bridge, so it counts as
    the network). Returns the
    https:// address to use instead, or None when the request may go ahead.

    HTTPS from a proxy (the Traefik route) sets RECIPE_APP_HTTPS_URL; the
    proxy's requests arrive as https (FORWARDED_ALLOW_IPS), direct ones to
    8090 don't. The app's own HTTPS counts only while its listener is
    actually up: if it couldn't start, the secure address doesn't work
    either, and refusing would only lock the owner out of Settings."""
    if request.url.scheme == "https":
        return None
    if request.client and request.client.host in ("127.0.0.1", "::1"):
        return None
    external = os.environ.get("RECIPE_APP_HTTPS_URL", "").strip()
    if external:
        return external
    st = https_setup.status()
    return st.get("url") if st.get("enabled") and st.get("url") and st.get("listening") else None


class _HttpsOnly(Exception):
    def __init__(self, url: str):
        super().__init__(url)
        self.url = url


def _https_only_response(url: str) -> JSONResponse:
    return JSONResponse(status_code=403, content={
        "detail": f"HTTPS is on, so this opens only at the secure address: {url}", "https_url": url})


def _settings_lock_refusal(request: Request):
    """None when this request may use what the settings password guards,
    else the response refusing it (the https-only 403 or the locked 401).
    Also used inside an endpoint whose path is open but some of whose
    requests aren't (the restore check, for a full backup)."""
    secure_url = _https_only_url(request)
    if secure_url:
        return _https_only_response(secure_url)
    if admin_lock.seconds_left(request.cookies.get(admin_lock.COOKIE_NAME)) > 0:
        return None
    detail = ("Locked. Enter the settings password." if admin_lock.password_set()
              else "Create a settings password first.")
    return JSONResponse(status_code=401, content={"detail": detail, "locked": True,
                                                  "password_set": admin_lock.password_set(),
                                                  "reset": admin_lock.was_reset()})


def _save_upload_streaming(file: UploadFile, dest_dir: str, name_prefix: str, expect: str,
                            max_bytes: int = MAX_UPLOAD_BYTES) -> str:
    """Write an upload to disk in chunks (not read fully into memory first),
    enforcing a size cap as it goes. Validates the file's magic bytes match
    the declared type, and names the saved file using an extension derived
    from those validated magic bytes -- never from the client-supplied
    filename (see IMAGE_MAGICS comment above for why). Returns the final
    filename actually used. Raises HTTPException and removes any partial
    file on rejection."""
    tmp_write_path = os.path.join(dest_dir, f"{name_prefix}.partial")
    written = 0
    first_chunk = b""
    with open(tmp_write_path, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            if not first_chunk:
                first_chunk = chunk[:16]
            written += len(chunk)
            if written > max_bytes:
                out.close()
                os.remove(tmp_write_path)
                raise HTTPException(status_code=413, detail=f"File too large (max {max_bytes // (1024 * 1024)} MB).")
            out.write(chunk)
        out.flush()
        os.fsync(out.fileno())

    if written == 0:
        os.remove(tmp_write_path)
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    if expect == "pdf":
        if not first_chunk.startswith(PDF_MAGIC):
            os.remove(tmp_write_path)
            raise HTTPException(status_code=400, detail="File does not look like a valid PDF.")
        ext = ".pdf"
    elif expect == "image":
        ext = detect_image_ext(first_chunk[:16])
        if ext is None:
            os.remove(tmp_write_path)
            raise HTTPException(status_code=400, detail="File does not look like a valid image.")
        try:
            reencode_image(tmp_write_path)
        except Exception:
            log.warning("_save_upload_streaming: request failed", exc_info=True)
            os.remove(tmp_write_path)
            raise HTTPException(status_code=400, detail="File could not be decoded as a valid image.")
    else:
        os.remove(tmp_write_path)
        raise HTTPException(status_code=400, detail="Unknown expected file type.")

    final_name = f"{name_prefix}{ext}"
    try:
        os.rename(tmp_write_path, os.path.join(dest_dir, final_name))
    except OSError:
        log.warning("_save_upload_streaming: couldn't keep %s", final_name, exc_info=True)
        try:
            os.remove(tmp_write_path)
        except OSError:
            log.debug("_save_upload_streaming: .partial already gone", exc_info=True)
        raise HTTPException(status_code=500, detail="The file couldn't be saved on the server.")
    return final_name


def _download_and_save_showcase_image(url: str, dest_dir: str, name_prefix: str,
                                       max_bytes: int = MAX_UPLOAD_BYTES) -> str | None:
    """Download a recipe page's showcase image (e.g. og:image / JSON-LD
    image) through the same safety pipeline as a direct upload: SSRF-guarded
    fetch with redirect re-validation (safe_get), a size cap, then the
    shared magic-byte/re-encode validation. Never raises -- a failed
    thumbnail fetch shouldn't fail the whole recipe ingestion."""
    try:
        resp = safe_get(url, timeout=10, max_bytes=MAX_UPLOAD_BYTES)
    except Exception:
        log.debug("_download_and_save_showcase_image: caught error, continuing", exc_info=True)
        return None
    content = resp.content
    if not content or len(content) > max_bytes:
        return None
    return validate_and_save_image_bytes(content, dest_dir, name_prefix)


def _extract_pdf_showcase_image(pdf_path: str, dest_dir: str, name_prefix: str) -> str | None:
    """Best-effort: pull the largest embedded image out of an ingested PDF
    and validate/save it the same way as any other image source. Never
    raises."""
    try:
        image_bytes = extract_largest_embedded_image(pdf_path)
    except Exception:
        log.debug("_extract_pdf_showcase_image: caught error, continuing", exc_info=True)
        return None
    if not image_bytes:
        return None
    return validate_and_save_image_bytes(image_bytes, dest_dir, name_prefix)


def _logged_minutes(value) -> int | None:
    """A real-world cook time in minutes; 0 (or blank) is no time logged,
    so the recipe page and the export agree."""
    return ddhhmm_to_minutes(value) or None


def _get_or_create_tags(db: Session, tag_ins: list[schemas.TagIn]) -> list[models.Tag]:
    # A category that isn't a group (a typo in an API call, a group deleted
    # while an edit screen was open) would save a tag nothing displays; it
    # goes to Custom instead, as init_db does for ones already stored.
    groups = {key for (key,) in db.query(models.TagGroup.key).all()} | {"custom"}
    tags = []
    for t in tag_ins:
        category = t.category if t.category in groups else "custom"
        subgroup = t.subgroup if category == t.category else None
        tag = db.query(models.Tag).filter_by(name=t.name, category=category).first()
        if not tag:
            tag = models.Tag(name=t.name, category=category, subgroup=subgroup)
            db.add(tag)
            db.flush()
        tags.append(tag)
    return tags


def _ingredient_fields(ing: schemas.IngredientIn) -> dict:
    """Columns for one ingredient row.

    The review, edit and manual-entry screens send each line as
    {raw_line: line, name: line} with no quantity or unit, so anything saved
    from the UI lost its structure. A line that arrives unparsed is parsed
    here; one that already has a quantity or unit is stored as given.
    raw_line is always kept exactly as entered.
    """
    unparsed = not ing.quantity and not ing.unit and (not ing.name or ing.name.strip() == ing.raw_line.strip())
    if unparsed:
        parsed = parse_ingredient_block([ing.raw_line])
        if parsed:
            return {"raw_line": ing.raw_line, "quantity": parsed[0]["quantity"],
                    "unit": parsed[0]["unit"], "name": parsed[0]["name"]}
    return {"raw_line": ing.raw_line, "quantity": ing.quantity, "unit": ing.unit, "name": ing.name}


def _apply_tags(db: Session, recipe: models.Recipe, tag_ins: list[schemas.TagIn]):
    recipe.tags = _get_or_create_tags(db, tag_ins)
    # Denormalized search column -- see init_db.py FTS setup for why this
    # exists instead of a trigger across the recipe_tags join table.
    recipe.tags_text = " ".join(t.name for t in recipe.tags)


def _promote_temp_file(temp_filename: str | None, current: str | None = None) -> str | None:
    """Move a draft file from TMP_DIR into UPLOADS_DIR at save time.

    Accepts only a draft that exists, or `current` -- the image the recipe
    being updated already has. It used to accept any well-formed name that
    wasn't a draft, on the assumption it was already the recipe's own. So a
    recipe could be created pointing at ANOTHER recipe's photo; deleting
    either one then deleted the file out from under the other, and a failed
    create could move the other recipe's photo into tmp/ for the sweep."""
    if not temp_filename:
        return None
    # Defense in depth: the schema layer already rejects anything that
    # isn't an app-generated filename, but this helper is called from
    # several paths and must never join an unvalidated string to a
    # directory -- an absolute path would make os.path.join() discard
    # the base dir entirely.
    if not is_safe_stored_filename(temp_filename):
        raise HTTPException(status_code=400, detail="Invalid image reference.")
    temp_path = safe_join(TMP_DIR, temp_filename)
    final_path = safe_join(UPLOADS_DIR, temp_filename)
    if temp_path and final_path and os.path.isfile(temp_path):
        shutil.move(temp_path, final_path)
        return temp_filename
    if current and temp_filename == current:
        return temp_filename
    raise HTTPException(status_code=400, detail="That photo isn't a pending upload; upload it again.")


def _discard_temp_file(filename: str | None):
    """Deletes a draft in TMP_DIR that won't be saved (an emailed recipe
    already in the library). The tmp sweep would get it eventually."""
    if not filename or not is_safe_stored_filename(filename):
        return
    path = safe_join(TMP_DIR, filename)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError:
            log.debug("_discard_temp_file: couldn't remove %s", filename, exc_info=True)


def _demote_to_tmp(filename: str | None):
    """Compensating action for _promote_temp_file: if the DB transaction
    that was supposed to reference a just-promoted file fails to commit,
    move it back to TMP_DIR rather than leaving an orphan in UPLOADS_DIR
    with nothing pointing at it. It'll be cleaned up by the normal tmp
    sweep from there."""
    if not filename:
        return
    final_path = safe_join(UPLOADS_DIR, filename)
    tmp_path = safe_join(TMP_DIR, filename)
    if final_path and tmp_path and os.path.isfile(final_path):
        try:
            shutil.move(final_path, tmp_path)
        except OSError:
            log.debug("_demote_to_tmp: caught error, continuing", exc_info=True)
            pass


def _delete_recipe_files(recipe: models.Recipe):
    """Total deletion: remove any file on disk associated with this recipe.
    Nothing file-related is left behind once a recipe is deleted. Callers
    must only invoke this AFTER the corresponding DB delete has committed
    successfully -- deleting the file first would leave an unrecoverable
    inconsistency (DB row/reference survives, file already gone) if the
    commit then failed."""
    if recipe.image_path:
        # safe_join returns None for anything outside UPLOADS_DIR, so a
        # malformed stored value can never resolve to a file elsewhere.
        path = safe_join(UPLOADS_DIR, recipe.image_path)
        if path and os.path.isfile(path):
            try:
                remove_upload_file(path)
            except OSError:
                log.debug("_delete_recipe_files: caught error, continuing", exc_info=True)
                pass


# ---------------------------------------------------------------------------
# Optional email ingest: a daily scan (plus an on-demand button) that looks
# for UNSEEN emails whose subject contains a configured keyword, tries to
# parse each as a recipe, and reports results by email. Everything about
# it is opt-in and disabled by default.
# ---------------------------------------------------------------------------


# Email scan/notification times are stored in UTC. SQLite keeps no time
# zone, so they come back naive; _iso_utc() marks them as UTC on the way
# out, and the browser shows them in its own time zone. (They used to be
# container-local with no zone, which the browser read as its own local
# time: hours off whenever the container's TZ wasn't the viewer's.) Only
# the daily scan HOUR is container-local, on purpose: it's set by TZ.
def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _copy_upload(src: str, name: str, who: str):
    """A recipe photo copied to uploads/<name> for Merge or Duplicate. A copy
    that fails part way (disk full) leaves nothing behind, and nothing is
    changed (GPT reviews of 0129 and 0132)."""
    dest = safe_join(UPLOADS_DIR, name)
    if not dest:
        raise HTTPException(status_code=500, detail="The photo couldn't be copied. Nothing was changed.")
    try:
        shutil.copyfile(src, dest)
    except OSError:
        log.warning("%s: photo not copied", who, exc_info=True)
        if os.path.exists(dest):
            remove_upload_file(dest)
        raise HTTPException(status_code=500, detail="The photo couldn't be copied. Nothing was changed.")


# ---------------------------------------------------------------------------
# Export / share / print
# ---------------------------------------------------------------------------

def _attachment_disposition(title: str, ext: str) -> str:
    """Content-Disposition for a download named after a recipe.

    Headers must be Latin-1. The title went in as-is, so "Grandma’s Pie"
    (curly apostrophe), an em dash or an emoji -- all common in scraped
    titles -- raised UnicodeEncodeError and the export returned 500. Now an
    ASCII fallback name plus the real name in RFC 5987 form, which browsers
    prefer and the app's own download code already reads.
    """
    from urllib.parse import quote
    import unicodedata
    base = (title or "recipe").strip()
    ascii_name = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("_") or "recipe"
    utf8_name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", base.replace(" ", "_")) or "recipe"
    return (f'attachment; filename="{ascii_name}.{ext}"; '
            f"filename*=UTF-8''{quote(utf8_name + '.' + ext, safe='')}")
