import asyncio
import fcntl
import os
import shutil
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import anyio
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from . import models, schemas
from .database import get_db, engine, SessionLocal, DATA_DIR, UPLOADS_DIR, TMP_DIR
from .init_db import init_db
from .ingestion.url_ingest import ingest_url, safe_get
from .ingestion.pdf_ingest import extract_pdf_text, segment_raw_text, PdfTooLargeError, extract_largest_embedded_image
from .ingestion.image_ingest import ingest_and_segment, ingest_image
from .ingestion.ingredient_parser import parse_ingredient_block
from .ingestion.tagger import suggest_tags
from .ingestion.email_processing import process_tagged_email
from .export import render_recipe_html, render_recipe_pdf
from .time_utils import ddhhmm_to_minutes, available_time_buckets
from . import email_client
from . import crypto

_lock_file_handle = None


def _acquire_single_instance_lock():
    """SQLite doesn't support multiple concurrent writer processes safely.
    This app is meant to run as a single container/instance against its
    data directory -- an accidental multi-replica deployment (or two
    instances pointed at the same bind-mounted ./data by mistake) could
    silently corrupt the database. Fail fast and loudly instead. Must run
    before init_db(), which does its own raw sqlite3 DDL work that would be
    exactly the kind of concurrent-writer scenario this guards against."""
    global _lock_file_handle
    lock_path = os.path.join(DATA_DIR, ".recipe-app.lock")
    _lock_file_handle = open(lock_path, "w")
    try:
        fcntl.flock(_lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise RuntimeError(
            "Another instance of this app appears to already be running against "
            f"this data directory ({DATA_DIR}). SQLite doesn't support concurrent "
            "writers safely -- refusing to start a second instance against the "
            "same data."
        )


_acquire_single_instance_lock()
init_db()

TMP_FILE_MAX_AGE_SECONDS = 24 * 60 * 60
TMP_SWEEP_INTERVAL_SECONDS = 6 * 60 * 60  # re-sweep periodically, not just at startup

# Bounds concurrent Tesseract/pdfplumber work. This isn't primarily about
# thread-pool exhaustion (the pool size bump in lifespan() below covers
# that) -- it's that running many OCR jobs at once just thrashes CPU
# between them without actually finishing any of them faster. A personal
# self-hosted tool has no legitimate reason to run more than a few at once.
MAX_CONCURRENT_HEAVY_JOBS = 3
_heavy_job_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_HEAVY_JOBS)


def _run_heavy(fn, *args, **kwargs):
    with _heavy_job_semaphore:
        return fn(*args, **kwargs)


from .file_validation import (
    MAX_UPLOAD_BYTES, PDF_MAGIC, IMAGE_MAGICS,
    reencode_image, validate_and_save_image_bytes, validate_and_save_pdf_bytes,
    is_safe_stored_filename, safe_join,
)


def _sweep_stale_tmp_files():
    """Safety net for draft files left behind when the add-recipe modal is
    closed without saving and the frontend's explicit discard call didn't
    fire (e.g. tab closed mid-flow), or a browser/tab crash. Runs at startup
    and periodically thereafter (see lifespan below) -- a container can run
    for weeks, so a startup-only sweep isn't enough on its own."""
    now = time.time()
    for name in os.listdir(TMP_DIR):
        path = os.path.join(TMP_DIR, name)
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > TMP_FILE_MAX_AGE_SECONDS:
                os.remove(path)
        except OSError:
            pass


def _sweep_orphaned_upload_files():
    """Covers a narrower gap than the tmp sweep: _promote_temp_file moves a
    file into UPLOADS_DIR before its DB commit lands, and create_recipe's
    except block only demotes it back on a *caught Python exception*. A
    hard crash (OOM kill, power loss) between the promote and the commit
    isn't caught by anything, and would otherwise leave that file in
    UPLOADS_DIR forever with no recipe referencing it. Cross-references
    UPLOADS_DIR against every image_path currently in the DB; anything
    unreferenced AND older than the age threshold gets removed -- the age
    check avoids racing an in-flight request that promoted a file moments
    before its commit was due to land."""
    db = SessionLocal()
    try:
        referenced = {
            r[0] for r in db.query(models.Recipe.image_path)
            .filter(models.Recipe.image_path.isnot(None)).all()
        }
    finally:
        db.close()

    now = time.time()
    for name in os.listdir(UPLOADS_DIR):
        path = os.path.join(UPLOADS_DIR, name)
        try:
            if (os.path.isfile(path) and name not in referenced
                    and now - os.path.getmtime(path) > TMP_FILE_MAX_AGE_SECONDS):
                os.remove(path)
        except OSError:
            pass


async def _periodic_tmp_sweep():
    while True:
        await asyncio.sleep(TMP_SWEEP_INTERVAL_SECONDS)
        _sweep_stale_tmp_files()
        _sweep_orphaned_upload_files()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sync ingestion endpoints (OCR/PDF work) and lightweight CRUD endpoints
    # share FastAPI's default worker thread pool. The default cap (40) is
    # plenty for a personal app under normal use, but bumping it gives real
    # headroom so a burst of heavy ingestion requests can't starve simple
    # requests (list/search/get) of a thread. MAX_CONCURRENT_HEAVY_JOBS
    # above is the actual CPU-thrashing guard; this is just pool headroom.
    anyio.to_thread.current_default_thread_limiter().total_tokens = 100

    _sweep_stale_tmp_files()
    _sweep_orphaned_upload_files()
    sweep_task = asyncio.create_task(_periodic_tmp_sweep())
    email_scan_task = asyncio.create_task(_daily_email_scan_loop())
    yield
    sweep_task.cancel()
    email_scan_task.cancel()
    engine.dispose()


app = FastAPI(title="Recipe App", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Optional API key auth. Off by default (RECIPE_APP_API_KEY unset), matching
# the "personal LAN tool" default this app is designed around -- see the
# README security notes for the reasoning. Setting the env var turns this
# into a required `X-API-Key` header on every request except /healthz (so
# Docker's HEALTHCHECK, which sends no such header, keeps working).
#
# This is API-level enforcement only -- there's no frontend login screen in
# this version, so using it currently means either an API client that sends
# the header itself, or a reverse proxy configured to inject it. Documented
# as such in the README rather than presented as a full turnkey login flow.
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("RECIPE_APP_API_KEY", "").strip()


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    if not API_KEY or request.url.path == "/healthz":
        return await call_next(request)
    if request.headers.get("X-API-Key") != API_KEY:
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-API-Key header."})
    return await call_next(request)


# ---------------------------------------------------------------------------
# Lightweight in-memory rate limiter, no new dependency. Active by default
# (not opt-in) at a generous threshold -- this is defense-in-depth against
# accidental hammering or abuse if the app ends up reachable beyond its
# intended LAN/VPN audience, not a precise quota system. Per-process,
# per-IP; resets on restart. Fine for a single-instance personal app; would
# need a shared store (e.g. Redis) to mean anything across multiple
# instances, which this app doesn't support anyway (see the single-instance
# lock above).
# ---------------------------------------------------------------------------
RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("RECIPE_APP_RATE_LIMIT", "120"))
_request_log: dict[str, deque] = defaultdict(deque)
_rate_limit_lock = threading.Lock()


@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _rate_limit_lock:
        log = _request_log[client_ip]
        while log and now - log[0] > RATE_LIMIT_WINDOW_SECONDS:
            log.popleft()
        if len(log) >= RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse(status_code=429, content={"detail": "Too many requests, slow down."})
        log.append(now)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    """Liveness check with no auth requirement and no DB dependency --
    used by the Dockerfile HEALTHCHECK. Deliberately separate from any
    endpoint that returns real data."""
    return {"status": "ok"}


def _fts_phrase(q: str) -> str:
    """Turn arbitrary user search text into a safe FTS5 phrase-query literal.
    Without this, raw user input is parsed as FTS5 query syntax (not just a
    plain bound value) -- a stray unmatched quote raises a SQLite
    OperationalError, and characters like `-`, `:`, `{}` have special
    meaning (NOT, column filters) that could change which column is
    searched. Wrapping as an escaped phrase neutralizes all of that."""
    return '"' + q.replace('"', '""') + '"'


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
        ext = next((mapped for magic, mapped in IMAGE_MAGICS if first_chunk.startswith(magic)), None)
        if ext is None:
            os.remove(tmp_write_path)
            raise HTTPException(status_code=400, detail="File does not look like a valid image.")
        try:
            reencode_image(tmp_write_path)
        except Exception:
            os.remove(tmp_write_path)
            raise HTTPException(status_code=400, detail="File could not be decoded as a valid image.")
    else:
        os.remove(tmp_write_path)
        raise HTTPException(status_code=400, detail="Unknown expected file type.")

    final_name = f"{name_prefix}{ext}"
    os.rename(tmp_write_path, os.path.join(dest_dir, final_name))
    return final_name


def _download_and_save_showcase_image(url: str, dest_dir: str, name_prefix: str,
                                       max_bytes: int = MAX_UPLOAD_BYTES) -> str | None:
    """Download a recipe page's showcase image (e.g. og:image / JSON-LD
    image) through the same safety pipeline as a direct upload: SSRF-guarded
    fetch with redirect re-validation (safe_get), a size cap, then the
    shared magic-byte/re-encode validation. Never raises -- a failed
    thumbnail fetch shouldn't fail the whole recipe ingestion."""
    try:
        resp = safe_get(url, timeout=10)
    except Exception:
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
        return None
    if not image_bytes:
        return None
    return validate_and_save_image_bytes(image_bytes, dest_dir, name_prefix)


def _get_or_create_tags(db: Session, tag_ins: list[schemas.TagIn]) -> list[models.Tag]:
    tags = []
    for t in tag_ins:
        tag = db.query(models.Tag).filter_by(name=t.name, category=t.category).first()
        if not tag:
            tag = models.Tag(name=t.name, category=t.category, subgroup=t.subgroup)
            db.add(tag)
            db.flush()
        tags.append(tag)
    return tags


def _apply_tags(db: Session, recipe: models.Recipe, tag_ins: list[schemas.TagIn]):
    recipe.tags = _get_or_create_tags(db, tag_ins)
    # Denormalized search column -- see init_db.py FTS setup for why this
    # exists instead of a trigger across the recipe_tags join table.
    recipe.tags_text = " ".join(t.name for t in recipe.tags)


def _promote_temp_file(temp_filename: str | None) -> str | None:
    """Move a draft file from TMP_DIR into UPLOADS_DIR at save time. If the
    filename doesn't exist in TMP_DIR (already promoted, or not a temp file
    reference at all), assume it's already a final uploads-relative name."""
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
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Optional email ingest: a daily scan (plus an on-demand button) that looks
# for UNSEEN emails whose subject contains a configured keyword, tries to
# parse each as a recipe, and reports results by email. Everything about
# it is opt-in and disabled by default.
# ---------------------------------------------------------------------------

def _get_email_settings(db: Session) -> models.EmailIngestSettings:
    """Settings are a singleton row (id=1), created on first access with
    defaults so the rest of the code never has to handle 'not configured
    yet' as a separate case."""
    settings = db.get(models.EmailIngestSettings, 1)
    if not settings:
        settings = models.EmailIngestSettings(id=1, enabled=False)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _queue_notification(db: Session, success: bool, message: str):
    db.add(models.EmailNotificationQueueItem(success=success, message=message))
    db.commit()


def _flush_notifications(db: Session, settings: models.EmailIngestSettings, force: bool = False) -> bool:
    """Sends one batched notification email covering everything queued
    since the last one, if the cooldown has elapsed (or force=True).
    Returns whether an email was actually sent.

    Queued items are only deleted after the send succeeds -- if SMTP
    fails, they stay queued for the next attempt rather than being lost.
    The cooldown exists so a scan that hits many failures at once
    produces a single digest rather than a burst of separate emails."""
    queued = db.query(models.EmailNotificationQueueItem).order_by(
        models.EmailNotificationQueueItem.created_at
    ).all()
    if not queued:
        return False

    if not force and settings.last_notification_sent_at and settings.cooldown_minutes:
        elapsed = datetime.now(settings.last_notification_sent_at.tzinfo) - settings.last_notification_sent_at
        if elapsed < timedelta(minutes=settings.cooldown_minutes):
            return False  # still cooling down; items stay queued for the next flush

    if not (settings.smtp_host and settings.username and settings.password_encrypted and settings.notify_email):
        return False

    successes = [q for q in queued if q.success]
    failures = [q for q in queued if not q.success]
    if failures and successes:
        subject = f"[PARTIAL] Open the Pantry: {len(successes)} ingested, {len(failures)} failed"
    elif failures:
        subject = f"[FAILURE] Open the Pantry: unable to parse {len(failures)} email(s)"
    else:
        subject = f"[SUCCESS] Open the Pantry: {len(successes)} recipe(s) ingested"

    body_lines = []
    if successes:
        body_lines.append("Ingested:")
        body_lines.extend(f"  - {q.message}" for q in successes)
        body_lines.append("")
    if failures:
        body_lines.append("Failed:")
        body_lines.extend(f"  - {q.message}" for q in failures)

    try:
        password = crypto.decrypt_secret(settings.password_encrypted)
        smtp = email_client.connect_smtp(
            settings.smtp_host, settings.smtp_port, settings.username, password, settings.smtp_use_tls
        )
        try:
            email_client.send_email(
                smtp, settings.username, settings.notify_email, subject, "\n".join(body_lines)
            )
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
    except Exception:
        return False  # leave everything queued; next scan retries

    for q in queued:
        db.delete(q)
    settings.last_notification_sent_at = datetime.now()
    db.commit()
    return True


def _save_email_recipe(db: Session, result: dict, subject: str) -> int:
    """Persists a successfully-parsed emailed recipe. Mirrors the
    auto-save batch endpoints (no manual review stage) since there's no
    one at the keyboard during a 3am scan."""
    image_path = _promote_temp_file(result.get("image_path")) if result.get("image_path") else None
    try:
        recipe = models.Recipe(
            title=result["title"],
            source_type="email",
            raw_text=result.get("raw_text"),
            ocr_confidence=result.get("ocr_confidence"),
            image_path=image_path,
        )
        db.add(recipe)
        db.flush()
        for pos, ing in enumerate(result["ingredients"]):
            db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **ing))
        for pos, step in enumerate(result["steps"]):
            db.add(models.Step(recipe_id=recipe.id, position=pos, text=step))
        tag_ins = [schemas.TagIn(**t) for t in result["tags"]]
        _apply_tags(db, recipe, tag_ins)
        db.commit()
    except Exception:
        db.rollback()
        _demote_to_tmp(image_path)
        raise
    return recipe.id


def run_email_scan(force_notify: bool = False) -> dict:
    """One full inbox pass: find tagged UNSEEN emails, try to parse each,
    save successes, queue result notifications, then flush them subject
    to the cooldown. Used by both the daily scheduled scan and the
    on-demand 'Scan inbox now' button, so the two can't drift apart.

    Opens its own Session rather than accepting the caller's: a
    SQLAlchemy Session is not thread-safe, and this runs in a worker
    thread for both callers (FastAPI's threadpool for the endpoint,
    anyio.to_thread for the scheduled loop). Passing in a session
    created and already used on the event-loop thread would be
    undefined behavior that happens to work under SQLite until it
    doesn't.

    Every message is marked Seen once handled -- success or failure --
    so a permanently-unparseable email isn't retried forever. Individual
    message failures never abort the scan."""
    db = SessionLocal()
    try:
        return _run_email_scan_inner(db, force_notify)
    finally:
        db.close()


def _run_email_scan_inner(db: Session, force_notify: bool) -> dict:
    settings = _get_email_settings(db)
    if not settings.enabled:
        return {"scanned": 0, "succeeded": 0, "failed": 0, "messages": ["Email ingest is disabled."]}
    if not (settings.imap_host and settings.username and settings.password_encrypted):
        return {"scanned": 0, "succeeded": 0, "failed": 0, "messages": ["Email ingest is not fully configured."]}

    messages = []
    succeeded = failed = 0

    try:
        password = crypto.decrypt_secret(settings.password_encrypted)
    except Exception as e:
        return {"scanned": 0, "succeeded": 0, "failed": 0, "messages": [str(e)]}

    try:
        imap = email_client.connect_imap(
            settings.imap_host, settings.imap_port, settings.username, password, settings.imap_use_ssl
        )
    except Exception as e:
        return {"scanned": 0, "succeeded": 0, "failed": 0, "messages": [f"Could not connect: {e}"]}

    try:
        msg_ids = email_client.search_unseen_by_subject(imap, settings.subject_keyword)
        for msg_id in msg_ids:
            subject = "(unknown subject)"
            try:
                msg = email_client.fetch_full_message(imap, msg_id)
                subject = email_client.decode_subject(msg.get("Subject", "")) or subject
                result = _run_heavy(process_tagged_email, msg, TMP_DIR)
                if result["success"]:
                    recipe_id = _save_email_recipe(db, result, subject)
                    succeeded += 1
                    line = f"{result['title']} (from \"{subject}\", via {result['source_detail']})"
                    messages.append(f"OK: {line}")
                    _queue_notification(db, True, line)
                else:
                    failed += 1
                    line = f"\"{subject}\": {result['error']}"
                    messages.append(f"FAILED: {line}")
                    _queue_notification(db, False, line)
            except Exception as e:
                failed += 1
                line = f"\"{subject}\": {e}"
                messages.append(f"FAILED: {line}")
                _queue_notification(db, False, line)
            finally:
                # Marked handled either way -- otherwise an email that can
                # never be parsed would be retried on every future scan.
                try:
                    email_client.mark_seen(imap, msg_id)
                except Exception:
                    pass
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    settings.last_scan_at = datetime.now()
    db.commit()

    _flush_notifications(db, settings, force=force_notify)

    return {"scanned": succeeded + failed, "succeeded": succeeded, "failed": failed, "messages": messages}


async def _daily_email_scan_loop():
    """Runs one scan per day at the configured hour. Checks every 15
    minutes rather than sleeping until the target time, so a changed
    scan hour (or a container that was asleep/suspended through the
    window) is picked up without needing a restart. A day-stamp guard
    ensures at most one scan per calendar day even though the check
    itself runs frequently."""
    last_scan_day = None
    while True:
        await asyncio.sleep(15 * 60)
        try:
            # Read the schedule config in a worker thread too -- no
            # Session is created on (or crosses) the event-loop thread.
            def _due_check():
                db = SessionLocal()
                try:
                    settings = _get_email_settings(db)
                    return bool(settings.enabled), settings.daily_scan_hour
                finally:
                    db.close()

            enabled, scan_hour = await anyio.to_thread.run_sync(_due_check)
            if not enabled:
                continue
            now = datetime.now()
            today = now.date()
            if last_scan_day == today or now.hour != scan_hour:
                continue
            last_scan_day = today
            await anyio.to_thread.run_sync(run_email_scan)
        except Exception:
            # A scheduled scan failing must never kill the loop -- the
            # next day's attempt should still happen.
            pass


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

@app.post("/api/ingest/url")
def ingest_from_url(payload: schemas.UrlIngestRequest):
    try:
        result = ingest_url(payload.url)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))

    parsed_ingredients = parse_ingredient_block(result.ingredients)
    tag_suggestions = suggest_tags(
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
        "suggested_tags": [{"name": n, "category": c, "subgroup": s} for n, c, s in tag_suggestions],
        "extraction_method": result.method,
        "ocr_confidence": None,
    }


@app.post("/api/ingest/url/batch")
def ingest_from_url_batch(payload: schemas.BatchUrlIngestRequest, db: Session = Depends(get_db)):
    """Batch add: unlike the single-URL path, results are saved directly
    (using the auto-parsed fields and auto-suggested tags as-is) rather than
    routed through the manual-review screen -- reviewing N items one at a
    time isn't really a batch operation. Failures are reported per-URL so
    they can be retried individually through the reviewed single-URL flow."""
    succeeded, failed = [], []
    for url in payload.urls:
        url = url.strip()
        if not url:
            continue
        image_path = None
        try:
            result = ingest_url(url)
            parsed_ingredients = parse_ingredient_block(result.ingredients)
            tag_suggestions = suggest_tags(
                result.title, [i["name"] or i["raw_line"] for i in parsed_ingredients], result.raw_text
            )
            if result.image_url:
                image_path = _download_and_save_showcase_image(result.image_url, UPLOADS_DIR, f"url-img-{uuid.uuid4().hex}")
            recipe = models.Recipe(
                title=result.title,
                source_type="url",
                source_url=url,
                servings=result.servings,
                prep_time=result.prep_time,
                cook_time=result.cook_time,
                total_time=result.total_time,
                raw_text=result.raw_text,
                image_path=image_path,
            )
            db.add(recipe)
            db.flush()
            for pos, ing in enumerate(parsed_ingredients):
                db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **ing))
            for pos, step in enumerate(result.steps):
                db.add(models.Step(recipe_id=recipe.id, position=pos, text=step))
            tag_ins = [schemas.TagIn(name=n, category=c, subgroup=s) for n, c, s in tag_suggestions]
            _apply_tags(db, recipe, tag_ins)
            db.commit()
            succeeded.append({"url": url, "recipe_id": recipe.id, "title": result.title})
        except Exception as e:
            db.rollback()
            if image_path:
                stray_path = safe_join(UPLOADS_DIR, image_path)
                if stray_path and os.path.isfile(stray_path):
                    os.remove(stray_path)
            failed.append({"url": url, "error": str(e)})
    return {"succeeded": succeeded, "failed": failed}


@app.post("/api/ingest/pdf")
def ingest_from_pdf(file: UploadFile = File(...)):
    name_prefix = f"pdf-{uuid.uuid4().hex}"
    temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="pdf")
    temp_path = os.path.join(TMP_DIR, temp_name)

    try:
        pdf_result = _run_heavy(extract_pdf_text, temp_path)
    except PdfTooLargeError as e:
        os.remove(temp_path)
        raise HTTPException(status_code=400, detail=str(e))

    segmented = segment_raw_text(pdf_result.raw_text)
    parsed_ingredients = parse_ingredient_block(segmented["ingredients"])
    tag_suggestions = suggest_tags(
        segmented["title_guess"], [i["name"] or i["raw_line"] for i in parsed_ingredients],
        pdf_result.raw_text,
    )

    # Best-effort: pull out the largest embedded image as a showcase photo.
    # A failure here never fails the ingestion -- the recipe's text content
    # is returned either way.
    image_path = _run_heavy(_extract_pdf_showcase_image, temp_path, TMP_DIR, f"pdf-img-{uuid.uuid4().hex}")

    return {
        "title": segmented["title_guess"],
        "source_type": "pdf",
        "raw_text": pdf_result.raw_text,
        "image_path": image_path,  # draft reference; promoted on save
        "ingredients": parsed_ingredients,
        "steps": [{"text": s} for s in segmented["steps"]],
        "suggested_tags": [{"name": n, "category": c, "subgroup": s} for n, c, s in tag_suggestions],
        "ocr_used_on_pages": pdf_result.ocr_used_on_pages,
        "ocr_confidence": pdf_result.avg_ocr_confidence,
        "stored_file": temp_name,  # a draft reference; promoted to UPLOADS_DIR only on save
    }


@app.post("/api/ingest/pdf/batch")
def ingest_from_pdf_batch(files: list[UploadFile] = File(...), db: Session = Depends(get_db)):
    """Batch add for PDFs: auto-saves each file directly, same rationale as
    the URL batch endpoint. The source PDF itself isn't kept as an image
    attachment here (only screenshot/manual entries carry an image) -- only
    its extracted content is saved."""
    succeeded, failed = [], []
    for file in files:
        name_prefix = f"pdf-{uuid.uuid4().hex}"
        temp_path = None
        try:
            temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="pdf")
            temp_path = os.path.join(TMP_DIR, temp_name)
            pdf_result = _run_heavy(extract_pdf_text, temp_path)
            segmented = segment_raw_text(pdf_result.raw_text)
            parsed_ingredients = parse_ingredient_block(segmented["ingredients"])
            tag_suggestions = suggest_tags(
                segmented["title_guess"], [i["name"] or i["raw_line"] for i in parsed_ingredients],
                pdf_result.raw_text,
            )
            recipe = models.Recipe(
                title=segmented["title_guess"],
                source_type="pdf",
                raw_text=pdf_result.raw_text,
                ocr_confidence=pdf_result.avg_ocr_confidence,
            )
            db.add(recipe)
            db.flush()
            for pos, ing in enumerate(parsed_ingredients):
                db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **ing))
            for pos, step in enumerate(segmented["steps"]):
                db.add(models.Step(recipe_id=recipe.id, position=pos, text=step))
            tag_ins = [schemas.TagIn(name=n, category=c, subgroup=s) for n, c, s in tag_suggestions]
            _apply_tags(db, recipe, tag_ins)
            db.commit()
            succeeded.append({"filename": file.filename, "recipe_id": recipe.id, "title": recipe.title})
        except HTTPException as e:
            db.rollback()
            failed.append({"filename": file.filename, "error": e.detail})
        except Exception as e:
            db.rollback()
            failed.append({"filename": file.filename, "error": str(e)})
        finally:
            if temp_path and os.path.isfile(temp_path):
                os.remove(temp_path)  # batch mode never keeps the source file
    return {"succeeded": succeeded, "failed": failed}


@app.post("/api/ingest/image")
def ingest_from_image(file: UploadFile = File(...)):
    name_prefix = f"img-{uuid.uuid4().hex}"
    temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="image")
    temp_path = os.path.join(TMP_DIR, temp_name)

    segmented = _run_heavy(ingest_and_segment, temp_path)
    parsed_ingredients = parse_ingredient_block(segmented["ingredients"])
    tag_suggestions = suggest_tags(
        segmented["title_guess"], [i["name"] or i["raw_line"] for i in parsed_ingredients],
        segmented["raw_text"],
    )

    return {
        "title": segmented["title_guess"],
        "source_type": "screenshot",
        "raw_text": segmented["raw_text"],
        "ingredients": parsed_ingredients,
        "steps": [{"text": s} for s in segmented["steps"]],
        "suggested_tags": [{"name": n, "category": c, "subgroup": s} for n, c, s in tag_suggestions],
        "ocr_confidence": segmented["ocr_confidence"],
        "stored_file": temp_name,
        "image_path": temp_name,  # draft reference; promoted on save
    }


MAX_COMBINED_IMAGES = 10  # generous for a multi-screenshot recipe, bounds worst-case single-request OCR time


@app.post("/api/ingest/images")
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
    except HTTPException:
        # A bad file partway through aborts the whole combined draft rather
        # than silently combining a partial set -- clean up anything
        # already saved before re-raising.
        for name in temp_names:
            path = os.path.join(TMP_DIR, name)
            if os.path.isfile(path):
                os.remove(path)
        raise

    combined_raw_text = "\n\n".join(raw_texts)
    segmented = segment_raw_text(combined_raw_text)
    parsed_ingredients = parse_ingredient_block(segmented["ingredients"])
    tag_suggestions = suggest_tags(
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

    return {
        "title": segmented["title_guess"],
        "source_type": "screenshot",
        "raw_text": combined_raw_text,
        "ingredients": parsed_ingredients,
        "steps": [{"text": s} for s in segmented["steps"]],
        "suggested_tags": [{"name": n, "category": c, "subgroup": s} for n, c, s in tag_suggestions],
        "ocr_confidence": avg_confidence,
        "stored_file": showcase_image,
        "image_path": showcase_image,
        "image_count": len(files),
    }


@app.post("/api/upload-image")
def upload_image(file: UploadFile = File(...)):
    """Standalone image upload used by the manual-entry form to attach a photo
    of a handwritten card. Written to TMP_DIR like other ingestion drafts --
    promoted to permanent storage only if the recipe is actually saved."""
    name_prefix = f"manual-{uuid.uuid4().hex}"
    temp_name = _save_upload_streaming(file, TMP_DIR, name_prefix, expect="image")
    return {"stored_file": temp_name}


@app.delete("/api/ingest/draft/{filename}")
def discard_draft_file(filename: str):
    """Called by the frontend when the add-recipe modal is closed without
    saving, so an uploaded PDF/screenshot/photo doesn't linger on disk with
    nothing in the database referencing it."""
    safe_name = os.path.basename(filename)  # no path traversal
    path = os.path.join(TMP_DIR, safe_name)
    if os.path.isfile(path):
        os.remove(path)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Recipe CRUD
# ---------------------------------------------------------------------------

@app.post("/api/recipes", response_model=schemas.RecipeOut)
def create_recipe(payload: schemas.RecipeCreate, db: Session = Depends(get_db)):
    # Promoting the file (a filesystem move) can't be rolled back by the DB
    # transaction below it -- if anything after this fails, the except
    # block demotes it back to TMP_DIR so it doesn't become a permanent
    # orphan with no recipe referencing it.
    promoted_image = _promote_temp_file(payload.image_path)
    try:
        recipe = models.Recipe(
            title=payload.title,
            source_type=payload.source_type,
            source_url=payload.source_url,
            servings=payload.servings,
            prep_time=payload.prep_time,
            cook_time=payload.cook_time,
            total_time=payload.total_time,
            image_path=promoted_image,
            raw_text=payload.raw_text,
            ocr_confidence=payload.ocr_confidence,
            favorite=payload.favorite,
            tastiness_rating=payload.tastiness_rating,
            cook_time_rating=payload.cook_time_rating,
            difficulty_rating=payload.difficulty_rating,
            actual_cook_time_minutes=ddhhmm_to_minutes(payload.actual_cook_time),
        )
        db.add(recipe)
        db.flush()

        for pos, ing in enumerate(payload.ingredients):
            db.add(models.Ingredient(
                recipe_id=recipe.id, position=pos, raw_line=ing.raw_line,
                quantity=ing.quantity, unit=ing.unit, name=ing.name,
            ))

        for pos, step_text in enumerate(payload.steps):
            db.add(models.Step(recipe_id=recipe.id, position=pos, text=step_text))

        _apply_tags(db, recipe, payload.tags)

        db.commit()
    except Exception:
        db.rollback()
        _demote_to_tmp(promoted_image)
        raise

    db.refresh(recipe)
    return recipe


@app.get("/api/recipes", response_model=list[schemas.RecipeSummaryOut])
def list_recipes(
    q: str | None = None,
    tags: list[str] | None = Query(default=None),        # repeated ?tags=A&tags=B, AND logic
    max_minutes: int | None = None,        # actual_cook_time_minutes <= max_minutes
    favorite: bool | None = None,
    db: Session = Depends(get_db),
):
    matched_via: dict[int, set[str]] = {}

    if q:
        # SECURITY NOTE: the `{title raw_text}` / `{tags_text}` column-set
        # prefixes below are fixed string literals, never derived from user
        # input -- only `phrase` (the escaped, quoted user query) varies.
        # If this ever changes to accept a caller-supplied column name,
        # that name must be validated against an allow-list first; column
        # names can't be parameterized via SQL bind parameters the way
        # values can.
        phrase = _fts_phrase(q)
        text_rows = db.execute(
            text("SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH :m"),
            {"m": f"{{title raw_text}} : {phrase}"},
        ).fetchall()
        for r in text_rows:
            matched_via.setdefault(r[0], set()).add("text")

        tag_rows = db.execute(
            text("SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH :m"),
            {"m": f"{{tags_text}} : {phrase}"},
        ).fetchall()
        for r in tag_rows:
            matched_via.setdefault(r[0], set()).add("tag")

        if not matched_via:
            return []
        query = db.query(models.Recipe).filter(models.Recipe.id.in_(matched_via.keys()))
    else:
        query = db.query(models.Recipe)

    if favorite:
        query = query.filter(models.Recipe.favorite.is_(True))

    if max_minutes is not None:
        query = query.filter(
            models.Recipe.actual_cook_time_minutes.isnot(None),
            models.Recipe.actual_cook_time_minutes <= max_minutes,
        )

    # Stacked tag filters: AND logic -- a recipe must carry every selected tag.
    if tags:
        for tag_name in tags:
            query = query.filter(
                models.Recipe.id.in_(
                    db.query(models.Recipe.id)
                    .join(models.Recipe.tags)
                    .filter(models.Tag.name == tag_name)
                )
            )

    results = query.order_by(models.Recipe.created_at.desc()).all()

    summaries = []
    for r in results:
        summary = schemas.RecipeSummaryOut.model_validate(r)
        if r.id in matched_via:
            summary.matched_via = sorted(matched_via[r.id])
        summaries.append(summary)
    return summaries


@app.get("/api/time-buckets")
def get_time_buckets(db: Session = Depends(get_db)):
    """Available '<= X' time-filter options, generated only from data on
    hand (see time_utils.available_time_buckets)."""
    rows = db.query(models.Recipe.actual_cook_time_minutes).filter(
        models.Recipe.actual_cook_time_minutes.isnot(None)
    ).all()
    logged = [r[0] for r in rows]
    return available_time_buckets(logged)


@app.get("/api/recipes/{recipe_id}", response_model=schemas.RecipeOut)
def get_recipe(recipe_id: int, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


@app.delete("/api/recipes/{recipe_id}")
def delete_recipe(recipe_id: int, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    db.delete(recipe)
    db.commit()
    # Only touch the filesystem after the DB delete has actually committed --
    # deleting the file first would leave the DB row referencing a file that
    # no longer exists if the commit then failed.
    _delete_recipe_files(recipe)
    return {"ok": True}


@app.post("/api/recipes/batch-delete")
def batch_delete_recipes(payload: schemas.BatchDeleteRequest, db: Session = Depends(get_db)):
    """Per-item commit + per-item file deletion (rather than one commit at
    the end): if a delete mid-batch fails, everything before it is already
    safely committed and cleaned up, and everything after it is untouched --
    instead of one failure potentially leaving files deleted from disk for
    recipes whose DB rows never actually got removed."""
    deleted, missing, failed = [], [], []
    for recipe_id in payload.ids:
        recipe = db.get(models.Recipe, recipe_id)
        if not recipe:
            missing.append(recipe_id)
            continue
        try:
            db.delete(recipe)
            db.commit()
        except Exception as e:
            db.rollback()
            failed.append({"id": recipe_id, "error": str(e)})
            continue
        _delete_recipe_files(recipe)
        deleted.append(recipe_id)
    return {"deleted": deleted, "missing": missing, "failed": failed}


@app.put("/api/recipes/{recipe_id}", response_model=schemas.RecipeOut)
def update_recipe(recipe_id: int, payload: schemas.RecipeCreate, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    new_image = _promote_temp_file(payload.image_path)
    old_image = recipe.image_path
    try:
        recipe.title = payload.title
        recipe.servings = payload.servings
        recipe.prep_time = payload.prep_time
        recipe.cook_time = payload.cook_time
        recipe.total_time = payload.total_time
        recipe.image_path = new_image
        recipe.raw_text = payload.raw_text
        recipe.favorite = payload.favorite
        recipe.tastiness_rating = payload.tastiness_rating
        recipe.cook_time_rating = payload.cook_time_rating
        recipe.difficulty_rating = payload.difficulty_rating
        recipe.actual_cook_time_minutes = ddhhmm_to_minutes(payload.actual_cook_time)

        db.query(models.Ingredient).filter_by(recipe_id=recipe.id).delete()
        db.query(models.Step).filter_by(recipe_id=recipe.id).delete()
        for pos, ing in enumerate(payload.ingredients):
            db.add(models.Ingredient(
                recipe_id=recipe.id, position=pos, raw_line=ing.raw_line,
                quantity=ing.quantity, unit=ing.unit, name=ing.name,
            ))
        for pos, step_text in enumerate(payload.steps):
            db.add(models.Step(recipe_id=recipe.id, position=pos, text=step_text))

        _apply_tags(db, recipe, payload.tags)

        db.commit()
    except Exception:
        db.rollback()
        # New image was promoted on the assumption this commit would
        # succeed -- demote it back to TMP_DIR rather than leaving an
        # orphan, and leave the old image file untouched since the recipe
        # row wasn't actually updated.
        if old_image != new_image:
            _demote_to_tmp(new_image)
        raise

    # Only remove the superseded file once the swap has actually committed.
    if old_image and old_image != new_image:
        old_path = safe_join(UPLOADS_DIR, old_image)
        if old_path and os.path.isfile(old_path):
            os.remove(old_path)

    db.refresh(recipe)
    return recipe


@app.patch("/api/recipes/{recipe_id}/rating", response_model=schemas.RecipeOut)
def update_rating(recipe_id: int, payload: schemas.RatingUpdate, db: Session = Depends(get_db)):
    """Lightweight endpoint for the card-level quick controls (favorite
    toggle, tastiness/pace/difficulty popovers) -- avoids resending the
    full recipe payload just to change one rating."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    if payload.favorite is not None:
        recipe.favorite = payload.favorite
    if payload.tastiness_rating is not None:
        recipe.tastiness_rating = payload.tastiness_rating
    if payload.cook_time_rating is not None:
        recipe.cook_time_rating = payload.cook_time_rating
    if payload.difficulty_rating is not None:
        recipe.difficulty_rating = payload.difficulty_rating
    if payload.actual_cook_time is not None:
        recipe.actual_cook_time_minutes = ddhhmm_to_minutes(payload.actual_cook_time) if payload.actual_cook_time else None

    db.commit()
    db.refresh(recipe)
    return recipe


@app.patch("/api/recipes/{recipe_id}/notes", response_model=schemas.RecipeOut)
def update_notes(recipe_id: int, payload: schemas.NotesUpdate, db: Session = Depends(get_db)):
    """Lightweight endpoint for the recipe-detail notes button -- avoids
    resending the full recipe payload just to add/edit/clear notes."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    recipe.notes = payload.notes or None  # '' and None both clear

    db.commit()
    db.refresh(recipe)
    return recipe


@app.patch("/api/recipes/{recipe_id}/image", response_model=schemas.RecipeOut)
def update_image(recipe_id: int, payload: schemas.ImageUpdate, db: Session = Depends(get_db)):
    """Lightweight endpoint for manually uploading, replacing, or removing
    a recipe's showcase image without resending the full recipe payload.
    payload.image_path is either a TMP_DIR draft filename from a prior
    POST /api/upload-image call (to promote as the new image) or omitted/
    null (to remove the current image entirely)."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    old_image = recipe.image_path
    if payload.image_path:
        # The contract is "pass a filename returned by a prior
        # POST /api/upload-image". Verify it actually exists as a draft
        # rather than storing a reference to a file that was never
        # uploaded -- _promote_temp_file passes unknown names through
        # unchanged, which would leave a permanently-broken image link.
        draft_path = safe_join(TMP_DIR, payload.image_path)
        if not (draft_path and os.path.isfile(draft_path)):
            raise HTTPException(
                status_code=400,
                detail="Unknown image reference -- upload the image first.",
            )
    new_image = _promote_temp_file(payload.image_path) if payload.image_path else None

    try:
        recipe.image_path = new_image
        db.commit()
    except Exception:
        db.rollback()
        if new_image and new_image != old_image:
            _demote_to_tmp(new_image)
        raise

    # Only remove the superseded file once the swap has actually committed.
    if old_image and old_image != new_image:
        old_path = safe_join(UPLOADS_DIR, old_image)
        if old_path and os.path.isfile(old_path):
            os.remove(old_path)

    db.refresh(recipe)
    return recipe


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@app.get("/api/tags", response_model=list[schemas.TagOut])
def list_tags(db: Session = Depends(get_db)):
    return db.query(models.Tag).order_by(models.Tag.category, models.Tag.subgroup, models.Tag.name).all()


# ---------------------------------------------------------------------------
# Email ingest (optional feature)
# ---------------------------------------------------------------------------

def _email_settings_out(settings: models.EmailIngestSettings) -> schemas.EmailSettingsOut:
    return schemas.EmailSettingsOut(
        enabled=settings.enabled,
        imap_host=settings.imap_host,
        imap_port=settings.imap_port,
        imap_use_ssl=settings.imap_use_ssl,
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_use_tls=settings.smtp_use_tls,
        username=settings.username,
        password_set=bool(settings.password_encrypted),
        notify_email=settings.notify_email,
        subject_keyword=settings.subject_keyword,
        daily_scan_hour=settings.daily_scan_hour,
        cooldown_minutes=settings.cooldown_minutes,
        last_scan_at=settings.last_scan_at.isoformat() if settings.last_scan_at else None,
        encryption_configured=crypto.encryption_configured(),
    )


@app.get("/api/email-settings", response_model=schemas.EmailSettingsOut)
def get_email_settings(db: Session = Depends(get_db)):
    return _email_settings_out(_get_email_settings(db))


@app.put("/api/email-settings", response_model=schemas.EmailSettingsOut)
def update_email_settings(payload: schemas.EmailSettingsIn, db: Session = Depends(get_db)):
    settings = _get_email_settings(db)

    if payload.password:
        # Fails closed if no encryption key is configured -- a credential is
        # never written in plaintext as a fallback.
        try:
            settings.password_encrypted = crypto.encrypt_secret(payload.password)
        except crypto.EncryptionNotConfiguredError as e:
            raise HTTPException(status_code=400, detail=str(e))
    # An omitted/blank password leaves the stored one untouched.

    if payload.enabled and not (payload.imap_host and payload.username and settings.password_encrypted):
        raise HTTPException(
            status_code=400,
            detail="IMAP host, username, and a password are all required before enabling email ingest.",
        )

    settings.enabled = payload.enabled
    settings.imap_host = payload.imap_host
    settings.imap_port = payload.imap_port
    settings.imap_use_ssl = payload.imap_use_ssl
    settings.smtp_host = payload.smtp_host
    settings.smtp_port = payload.smtp_port
    settings.smtp_use_tls = payload.smtp_use_tls
    settings.username = payload.username
    settings.notify_email = payload.notify_email
    settings.subject_keyword = payload.subject_keyword or "[RECIPE]"
    settings.daily_scan_hour = payload.daily_scan_hour
    settings.cooldown_minutes = payload.cooldown_minutes

    db.commit()
    db.refresh(settings)
    return _email_settings_out(settings)


@app.delete("/api/email-settings/password")
def clear_email_password(db: Session = Depends(get_db)):
    """Removes the stored credential (and disables the feature, since it
    can't run without one)."""
    settings = _get_email_settings(db)
    settings.password_encrypted = None
    settings.enabled = False
    db.commit()
    return {"ok": True}


@app.post("/api/email-settings/test", response_model=schemas.EmailTestResult)
def test_email_settings(db: Session = Depends(get_db)):
    """Round-trip check: sends a [TEST] email to the notify address via
    SMTP, then connects over IMAP to confirm reading works too. Reports
    which specific step failed rather than a generic error, since
    misconfigured host/port/credentials are the common case here."""
    settings = _get_email_settings(db)
    if not (settings.imap_host and settings.smtp_host and settings.username and settings.password_encrypted):
        return schemas.EmailTestResult(success=False, message="Fill in IMAP host, SMTP host, username, and password first.")
    if not settings.notify_email:
        return schemas.EmailTestResult(success=False, message="Set a notification email address first.")

    try:
        password = crypto.decrypt_secret(settings.password_encrypted)
    except Exception as e:
        return schemas.EmailTestResult(success=False, message=str(e))

    try:
        smtp = email_client.connect_smtp(
            settings.smtp_host, settings.smtp_port, settings.username, password, settings.smtp_use_tls
        )
        try:
            email_client.send_email(
                smtp, settings.username, settings.notify_email,
                "[TEST] Open the Pantry email ingest",
                "This is a test message from Open the Pantry.\n\n"
                "Receiving it means outgoing notifications are working. "
                f"Open the Pantry will scan for emails whose subject contains "
                f"\"{settings.subject_keyword}\" once daily at "
                f"{settings.daily_scan_hour:02d}:00.",
            )
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
    except Exception as e:
        return schemas.EmailTestResult(success=False, message=f"SMTP (sending) failed: {e}")

    try:
        imap = email_client.connect_imap(
            settings.imap_host, settings.imap_port, settings.username, password, settings.imap_use_ssl
        )
        try:
            imap.logout()
        except Exception:
            pass
    except Exception as e:
        return schemas.EmailTestResult(
            success=False,
            message=f"Sending worked, but IMAP (reading) failed: {e}",
        )

    return schemas.EmailTestResult(
        success=True,
        message=f"Sent a [TEST] email to {settings.notify_email} and confirmed inbox access. Check that it arrived.",
    )


@app.post("/api/email-settings/scan", response_model=schemas.EmailScanResult)
def scan_inbox_now():
    """On-demand equivalent of the daily scheduled scan -- identical
    logic, just triggered manually. force_notify bypasses the cooldown
    since the user is watching and explicitly asked for this."""
    result = run_email_scan(force_notify=True)
    return schemas.EmailScanResult(**result)


# ---------------------------------------------------------------------------
# Export / share / print
# ---------------------------------------------------------------------------

@app.get("/api/recipes/{recipe_id}/export.pdf")
def export_pdf(recipe_id: int, include_notes: bool = False, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    abs_image = safe_join(UPLOADS_DIR, recipe.image_path) if recipe.image_path else None
    # The showcase image is deliberately never included in the shared PDF --
    # not a query param/user choice, unlike include_notes.
    pdf_bytes = render_recipe_pdf(recipe, abs_image, include_notes=include_notes, include_image=False)
    filename = f"{recipe.title.replace(' ', '_')}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/recipes/{recipe_id}/export.html")
def export_html(recipe_id: int, include_notes: bool = False, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    abs_image = safe_join(UPLOADS_DIR, recipe.image_path) if recipe.image_path else None
    html_str = render_recipe_html(recipe, abs_image, include_notes=include_notes)
    filename = f"{recipe.title.replace(' ', '_')}.html"
    return Response(
        content=html_str,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# Static file serving: uploaded images, and the frontend PWA itself
# ---------------------------------------------------------------------------

app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")
# Lets the add-recipe review screen preview a draft's auto-captured
# showcase image (from URL/PDF ingestion) before it's saved/promoted.
# Content here has already been through the same magic-byte-check +
# Pillow re-encode pipeline as UPLOADS_DIR, so this isn't a materially
# different trust boundary -- filenames are unguessable UUIDs either way.
app.mount("/tmp-preview", StaticFiles(directory=TMP_DIR), name="tmp_preview")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
