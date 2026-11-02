"""Backup & export, import, scheduled backups, and restoring a full backup."""
import asyncio
import re
import os
import shutil
import threading
import time
import uuid
from typing import Optional
from datetime import datetime
import anyio
from fastapi import Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from sqlalchemy import text
from sqlalchemy.orm import Session, selectinload
from .. import models, schemas
from ..database import get_db, engine, SessionLocal, DATA_DIR, UPLOADS_DIR, TMP_DIR, DB_PATH
from ..init_db import init_db
from ..ingestion.ingredient_parser import parse_ingredient_block
from ..ingestion.tagger import suggest_tags
from ..backup import build_database_backup, build_pdf_bundle, settings_blanked, UPLOADS_LOCK
from .. import scheduled_backup
from .. import crypto
from .. import picker
from .. import search
from .. import duplicates
from .. import interchange
from .. import restore as restore_mod
from ..ml import service as learning
from ..file_validation import validate_and_save_image_bytes
from . import common
from .common import BACKUP_TMP_DIR, _discard_temp_file, _inflight_lock, _restoring, _run_heavy, _settings_lock_refusal, _utc_now, refresh_user_keywords
from .email import _email_scan_lock
from .recipes import create_recipe
from fastapi import APIRouter
from ..logging_setup import get_logger

log = get_logger("app")
router = APIRouter()


# ---------------------------------------------------------------------------
# Backup / export bundles
# ---------------------------------------------------------------------------

def _timestamped(prefix: str) -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"


def _stream_zip_and_cleanup(path: str, filename: str) -> FileResponse:
    """Stream a temp zip to the client, then delete it.

    BackgroundTask runs after the response body has been sent, so the file
    survives long enough to be read. Building these in memory instead is not
    an option: a library with a few hundred photos runs to hundreds of MB,
    and this app is expected to run on a NAS.
    """
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(lambda: os.path.isfile(path) and os.remove(path)),
    )


@router.get("/api/backup/info")
def backup_info(db: Session = Depends(get_db)):
    """Lets the UI warn about size/duration before someone taps a button that
    renders several hundred PDFs."""
    recipe_count = db.query(models.Recipe).count()
    upload_bytes = 0
    upload_count = 0
    if os.path.isdir(UPLOADS_DIR):
        for name in os.listdir(UPLOADS_DIR):
            p = os.path.join(UPLOADS_DIR, name)
            if os.path.isfile(p):
                upload_count += 1
                upload_bytes += os.path.getsize(p)
    return {
        "recipe_count": recipe_count,
        "upload_count": upload_count,
        "upload_bytes": upload_bytes,
        "database_bytes": os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0,
    }


@router.get("/api/backup/database.zip")
def backup_database():
    """Restorable backup: a consistent database snapshot plus every upload.

    Not db-session-dependent on purpose -- it snapshots through SQLite's own
    backup API rather than reading rows through the ORM, which is what makes
    it consistent under WAL. See backup.snapshot_database.
    """
    path = os.path.join(BACKUP_TMP_DIR, f"backup-{uuid.uuid4().hex}.zip")
    try:
        _run_heavy(build_database_backup, path)
    except Exception:
        log.warning("backup_database: request failed", exc_info=True)
        if os.path.isfile(path):
            os.remove(path)
        raise HTTPException(status_code=500, detail="Could not build the backup archive.")
    return _stream_zip_and_cleanup(path, _timestamped("open-the-pantry-backup"))


@router.get("/api/backup/pdfs.zip")
def backup_pdfs(include_notes: bool = True, db: Session = Depends(get_db)):
    """Reading archive: one PDF per recipe. Cannot be restored from.

    include_notes defaults to True here, unlike the per-recipe share export.
    This bundle is for you, not for handing to someone else, and notes are
    part of what you'd want to keep.
    """
    # Relationships preloaded in two queries instead of lazily, one query per
    # recipe per relationship, while the PDFs are built.
    recipes = (
        db.query(models.Recipe)
        .options(selectinload(models.Recipe.ingredients), selectinload(models.Recipe.steps),
                 selectinload(models.Recipe.tags))
        .order_by(models.Recipe.id).all()
    )
    if not recipes:
        raise HTTPException(status_code=404, detail="There are no recipes to export.")

    path = os.path.join(BACKUP_TMP_DIR, f"pdfs-{uuid.uuid4().hex}.zip")
    try:
        _run_heavy(build_pdf_bundle, recipes, path, include_notes)
    except Exception:
        log.warning("backup_pdfs: request failed", exc_info=True)
        if os.path.isfile(path):
            os.remove(path)
        raise HTTPException(status_code=500, detail="Could not build the PDF archive.")
    return _stream_zip_and_cleanup(path, _timestamped("open-the-pantry-pdfs"))


@router.get("/api/backup/recipes-json.zip")
def backup_recipes_json(db: Session = Depends(get_db)):
    """Every recipe as a schema.org Recipe .json, photos alongside
    (interchange.py): readable by other recipe apps, and importable here
    (adds; the full backup is what restores)."""
    recipes = (
        db.query(models.Recipe)
        .options(selectinload(models.Recipe.ingredients), selectinload(models.Recipe.steps),
                 selectinload(models.Recipe.tags))
        .order_by(models.Recipe.id).all()
    )
    if not recipes:
        raise HTTPException(status_code=404, detail="There are no recipes to export.")
    path = os.path.join(BACKUP_TMP_DIR, f"json-{uuid.uuid4().hex}.zip")
    try:
        _run_heavy(interchange.build_jsonld_zip, recipes, UPLOADS_DIR, path, UPLOADS_LOCK)
    except Exception:
        log.warning("backup_recipes_json: request failed", exc_info=True)
        if os.path.isfile(path):
            os.remove(path)
        raise HTTPException(status_code=500, detail="Could not build the JSON archive.")
    return _stream_zip_and_cleanup(path, _timestamped("open-the-pantry-recipes-json"))


# ---------------------------------------------------------------------------
# Import (interchange.py): schema.org JSON, zips of it, Paprika
# ---------------------------------------------------------------------------

MAX_IMPORT_BYTES = 1024 ** 3   # a library export with every photo


def _match_tags(db: Session, names: list[str], keep_unknown: bool) -> list[schemas.TagIn]:
    """Labels from another app, matched to existing tags by name (any
    case). Unknown ones become Custom tags only when they're the person's
    own categories (Paprika), not a website's keywords."""
    if not names:
        return []
    by_name = {}
    for t in db.query(models.Tag).all():
        by_name.setdefault(t.name.casefold(), t)
    out = []
    for n in names:
        t = by_name.get(n.casefold())
        if t:
            out.append(schemas.TagIn(name=t.name, category=t.category, subgroup=t.subgroup))
        elif keep_unknown:
            out.append(schemas.TagIn(name=n, category="custom"))
    return out


def _save_imported(db: Session, r: "interchange.ImportedRecipe") -> tuple[int, Optional[dict]]:
    """Saves one imported recipe through create_recipe (the same checks and
    caps as any save). Returns (recipe id, duplicate_of or None)."""
    same_title = duplicates.find(db, title=r.title, limit=1)
    parsed = parse_ingredient_block(r.ingredients)
    if r.tags:
        tag_ins = [schemas.TagIn(**t) for t in r.tags]
    else:
        tag_ins = _match_tags(db, r.tag_names, keep_unknown=False) + _match_tags(db, r.custom_tag_names, keep_unknown=True)
        for n, c, sg in suggest_tags(r.title, [i["name"] or i["raw_line"] for i in parsed], " ".join(r.steps)):
            tag_ins.append(schemas.TagIn(name=n, category=c, subgroup=sg))
    seen, unique_tags = set(), []
    for t in tag_ins:
        if (t.name, t.category) not in seen:
            seen.add((t.name, t.category))
            unique_tags.append(t)
    image = None
    photo = r.photo()
    if photo:
        image = validate_and_save_image_bytes(photo, TMP_DIR, f"imp-{uuid.uuid4().hex}")
    try:
        payload = schemas.RecipeCreate(
            title=r.title[:500], source_type=(r.source_type or "import")[:20], source_url=r.source_url,
            servings=r.servings, prep_time=r.prep_time, cook_time=r.cook_time, total_time=r.total_time,
            image_path=image, ingredients=[schemas.IngredientIn(**i) for i in parsed], steps=r.steps,
            tags=unique_tags[:50], favorite=r.favorite, tastiness_rating=r.tastiness_rating,
            cook_time_rating=r.cook_time_rating, difficulty_rating=r.difficulty_rating,
            language=r.language, actual_cook_time=r.actual_cook_time,
        )
        recipe = create_recipe(payload, db)
    except Exception:
        log.debug("_save_imported: %r not saved; its photo draft is discarded", r.title, exc_info=True)
        _discard_temp_file(image)
        raise
    if r.notes or r.to_review:
        recipe.notes = r.notes[:50_000] if r.notes else None
        # Still to review in the library it came from: still to review here.
        recipe.needs_review = r.to_review
        db.commit()
    return recipe.id, (same_title[0] if same_title else None)


def _validation_text(e: Exception) -> str:
    if hasattr(e, "errors"):
        try:
            first = e.errors()[0]
            return f"{'.'.join(str(x) for x in first.get('loc', ()))}: {first.get('msg')}"
        except Exception:
            log.debug("_validation_text: unexpected error shape", exc_info=True)
    return str(getattr(e, "detail", None) or e) or e.__class__.__name__


def _import_and_save(db: Session, path: str, name: str):
    succeeded, failed, skipped = [], [], []
    with interchange.opened(path, name) as (recipes, problems):
        for r in recipes:
            try:
                existing = duplicates.same_link(db, r.source_url)
                if existing:
                    skipped.append({"origin": r.origin, "recipe_id": existing["id"], "title": existing["title"]})
                    continue
                rid, dup = _save_imported(db, r)
                succeeded.append({"origin": r.origin, "recipe_id": rid, "title": r.title[:500], "duplicate_of": dup})
            except Exception as e:
                db.rollback()
                log.warning("Import of %r from %s failed: %s", r.title, name, _validation_text(e))
                failed.append({"origin": r.origin, "title": r.title[:500], "error": _validation_text(e)})
        # Read last: reading photos can add one ("…the rest was left out").
        failed = [dict(p) for p in problems] + failed
    return succeeded, failed, skipped


@router.post("/api/import/recipes")
def import_recipes(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Adds the recipes in a schema.org .json, a zip of them (this app's
    "Recipes as JSON" or another app's) or a Paprika .paprikarecipes file.
    Nothing is replaced. A recipe already saved from the same link is
    skipped; one whose title is already used is saved with a note. Photos
    inside the file are kept (validated like any upload); photos only
    linked from the web aren't fetched, so a big import never means
    hundreds of downloads."""
    name = os.path.basename(file.filename or "import")
    path = os.path.join(TMP_DIR, f"import-{uuid.uuid4().hex}.upload")
    written = 0
    try:
        with open(path, "wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_IMPORT_BYTES:
                    raise HTTPException(status_code=413, detail="File too large (max 1024 MB).")
                out.write(chunk)
        if not written:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        try:
            # The file stays open while the recipes are saved: each photo is
            # read as its recipe is saved, not all of them first.
            succeeded, failed, skipped = _run_heavy(_import_and_save, db, path, name)
        except interchange.ImportFileError as e:
            raise HTTPException(status_code=422, detail=str(e))
    finally:
        if os.path.isfile(path):
            os.remove(path)
    log.info("Import from %s: %d added, %d skipped, %d failed", name, len(succeeded), len(skipped), len(failed))
    return {"succeeded": succeeded, "failed": failed, "skipped": skipped}


# ---------------------------------------------------------------------------
# Automatic backups (scheduled_backup.py)
# ---------------------------------------------------------------------------

_backup_run_lock = threading.Lock()


def _backup_schedule(db: Session) -> models.BackupSchedule:
    row = db.get(models.BackupSchedule, 1)
    if row is None:
        row = models.BackupSchedule(id=1, enabled=False, hour=3, keep=7)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _backup_status(db: Session) -> dict:
    row = _backup_schedule(db)
    return {
        "enabled": bool(row.enabled), "hour": row.hour, "keep": row.keep,
        "folder": scheduled_backup.folder(),
        "folder_in_data": os.path.abspath(scheduled_backup.folder()).startswith(os.path.abspath(DATA_DIR) + os.sep),
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "last_ok": row.last_ok, "last_message": row.last_message,
        "backups": scheduled_backup.listing(),
        "running": _backup_run_lock.locked(),
    }


def run_scheduled_backup() -> dict:
    """One automatic backup now (the daily loop, or "Back up now"); its
    result is saved for Settings. Not while a restore runs or another
    backup is being made."""
    if _restoring.is_set():
        raise scheduled_backup.BackupError("A restore is running; the backup will be made at the next check.")
    if not _backup_run_lock.acquire(blocking=False):
        raise scheduled_backup.BackupError("A backup is already being made.")
    try:
        db = SessionLocal()
        try:
            keep = _backup_schedule(db).keep
        finally:
            db.close()
        ok, message, result = True, None, None
        try:
            result = _run_heavy(scheduled_backup.make, keep, build_database_backup)
            message = result["name"]
        except scheduled_backup.BackupError as e:
            ok, message = False, str(e)
            log.error("Automatic backup failed: %s", e)
        except Exception as e:
            ok, message = False, f"The backup failed ({e.__class__.__name__}); Settings -> Logs has the details."
            log.exception("Automatic backup failed")
        db = SessionLocal()
        try:
            row = _backup_schedule(db)
            row.last_run_at, row.last_ok, row.last_message = _utc_now(), ok, message
            if ok:
                row.last_success_at = row.last_run_at
            db.commit()
        finally:
            db.close()
        if not ok:
            raise scheduled_backup.BackupError(message)
        return result
    finally:
        _backup_run_lock.release()


async def _scheduled_backup_loop():
    """Checks every 10 minutes whether a backup is due
    (scheduled_backup.due); a failure is recorded and never ends the loop."""
    while True:
        await asyncio.sleep(scheduled_backup.CHECK_EVERY)
        try:
            def _check():
                db = SessionLocal()
                try:
                    row = _backup_schedule(db)
                    return scheduled_backup.due(bool(row.enabled), row.hour, row.last_success_at,
                                                failed_at=row.last_run_at if row.last_ok is False else None)
                finally:
                    db.close()
            if await anyio.to_thread.run_sync(_check):
                await anyio.to_thread.run_sync(run_scheduled_backup)
        except scheduled_backup.BackupError:
            log.debug("_scheduled_backup_loop: recorded failure, continuing", exc_info=True)
        except Exception:
            log.exception("Automatic backup: the check failed; trying again in 10 minutes")


@router.get("/api/backups/status")
def backups_status(db: Session = Depends(get_db)):
    """The schedule, the last result and the backups in the folder."""
    return _backup_status(db)


@router.put("/api/backups/schedule")
def backups_schedule(payload: schemas.BackupScheduleIn, db: Session = Depends(get_db)):
    row = _backup_schedule(db)
    row.enabled, row.hour, row.keep = payload.enabled, payload.hour, payload.keep
    db.commit()
    log.info("Automatic backups %s (%02d:00, keep %d) in %s", "on" if payload.enabled else "off",
             payload.hour, payload.keep, scheduled_backup.folder())
    if payload.enabled:
        scheduled_backup.prune(payload.keep)
    return _backup_status(db)


@router.post("/api/backups/run")
def backups_run(db: Session = Depends(get_db)):
    """"Back up now"."""
    try:
        run_scheduled_backup()
    except scheduled_backup.BackupError as e:
        raise HTTPException(status_code=409 if "already" in str(e) or "restore is running" in str(e) else 500,
                            detail=str(e))
    db.expire_all()
    return _backup_status(db)


# ---------------------------------------------------------------------------
# Restore from backup (restore.py), behind the settings password
# ---------------------------------------------------------------------------

_restore_lock = threading.Lock()
_RESTORE_TOKEN = re.compile(r"^[0-9a-f]{32}$")


def _pending_restore_paths(token: str) -> tuple[str, str]:
    return (os.path.join(BACKUP_TMP_DIR, f"restore-{token}.zip"),
            os.path.join(BACKUP_TMP_DIR, f"restore-{token}.db"))


PENDING_RESTORE_SECONDS = 30 * 60   # a checked backup waits this long for "Replace my library"


def _remove_pending(token: str):
    for path in _pending_restore_paths(token):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("_remove_pending: couldn't remove %s", path, exc_info=True)


def _clear_pending_restores(older_than: float | None = PENDING_RESTORE_SECONDS):
    """Checked backups nobody confirmed. Only those older than
    PENDING_RESTORE_SECONDS by default: a check used to delete every
    pending backup, so a second check (another tab, two at once) made the
    first one's Replace fail or its check return a 500 (Claude review
    round 10). None removes all (after a restore)."""
    now = time.time()
    for name in os.listdir(BACKUP_TMP_DIR):
        if not name.startswith("restore-"):
            continue
        path = os.path.join(BACKUP_TMP_DIR, name)
        try:
            if older_than is None or now - os.path.getmtime(path) > older_than:
                os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            log.debug("_clear_pending_restores: couldn't remove %s", name, exc_info=True)


@router.post("/api/backup/restore/check")
def restore_check(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    """First step: what's in the file. A full backup is kept (unpacked
    database checked) until it's confirmed; anything else is for the import
    path ("kind": "import"), which adds instead of replacing.

    The path is open, because the same box imports, and an import needs no
    password. A full backup does: checking one unpacks and integrity-checks
    a database of up to 4 GB, which nobody without the password should be
    able to make the server do (Claude review round 10 left it unreviewed;
    looked at here). Without it, uploads stop at the import's 1 GB, and a
    backup gets the locked 401, which makes the page ask for the password
    and send the file again."""
    refusal = _settings_lock_refusal(request)
    limit = restore_mod.MAX_TOTAL_BYTES if refusal is None else MAX_IMPORT_BYTES
    token = uuid.uuid4().hex
    zip_path, db_path = _pending_restore_paths(token)
    _clear_pending_restores()
    written = 0
    with open(zip_path, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                out.close()
                os.remove(zip_path)
                if refusal is not None:
                    return refusal        # too big for an import: only a backup could be
                raise HTTPException(status_code=413, detail="File too large.")
            out.write(chunk)
    try:
        with open(zip_path, "rb") as f:
            head = f.read(4)
        if not head.startswith(b"PK"):
            os.remove(zip_path)
            return {"kind": "import"}     # a .json: the import path says what it makes of it
        kind = restore_mod.kind_of(zip_path)
        if kind != "backup":
            os.remove(zip_path)
            return {"kind": kind}
        if refusal is not None:
            os.remove(zip_path)
            return refusal
        info = _run_heavy(restore_mod.inspect, zip_path, db_path)
    except restore_mod.RestoreError as e:
        _remove_pending(token)
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        log.warning("Restore check failed on %s", file.filename, exc_info=True)
        _remove_pending(token)
        raise
    info.update(token=token, current_recipes=db.query(models.Recipe).count())
    return info


# Kept from the live database across a restore: HTTPS always (it belongs
# to this server), and the email settings when the backup's were blanked
# (backup.REDACTED_SETTINGS, from 0143); an older backup's are restored.
_KEPT_TABLES = ("https_settings", "email_ingest_settings", "backup_schedule")


def _keep_rows(table: str) -> list[dict]:
    assert table in _KEPT_TABLES
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT * FROM {table}")).mappings().all()
    return [dict(r) for r in rows]


def _put_back_blanked_email_fields(live: dict):
    """A blanked backup's email row keeps what it has (mail servers, ports,
    scan hour); only the fields the backup left out come from this server.
    The whole live row used to be put back, so on a new server the backup's
    mail servers were lost to its defaults (Claude review of 0148)."""
    from ..backup import REDACTED_SETTINGS
    fields = [k for k in REDACTED_SETTINGS["email_ingest_settings"] if k in live]
    with engine.begin() as conn:
        if conn.execute(text("SELECT COUNT(*) FROM email_ingest_settings")).scalar():
            conn.execute(text("UPDATE email_ingest_settings SET " + ", ".join(f"{k} = :{k}" for k in fields)),
                         {k: live[k] for k in fields})
        else:
            _put_back_rows("email_ingest_settings", [live])


def _put_back_rows(table: str, rows: list[dict]):
    assert table in _KEPT_TABLES
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).all()}
        conn.execute(text(f"DELETE FROM {table}"))
        for row in rows:
            row = {k: v for k, v in row.items() if k in cols}
            names = ", ".join(row)
            conn.execute(text(f"INSERT INTO {table} ({names}) VALUES ({', '.join(':' + k for k in row)})"), row)


def _clear_unusable_email_password() -> bool:
    db = SessionLocal()
    try:
        s = db.query(models.EmailIngestSettings).first()
        if not s or not s.password_encrypted:
            return False
        try:
            crypto.decrypt_secret(s.password_encrypted)
            return False
        except Exception:
            log.warning("Restore: the backup's email password can't be decrypted with this server's key; cleared")
            s.password_encrypted = None
            s.enabled = False
            s.last_problem = "Restored from a backup: enter the email password again (this server's encryption key can't read the one in the backup)."
            db.commit()
            return True
    finally:
        db.close()


@router.post("/api/backup/restore")
def restore_backup(payload: schemas.RestoreRequest):
    """Replaces the library with the checked backup. See restore.py."""
    if not _RESTORE_TOKEN.match(payload.token):
        raise HTTPException(status_code=422, detail="Unknown restore.")
    zip_path, db_path = _pending_restore_paths(payload.token)
    if not (os.path.isfile(zip_path) and os.path.isfile(db_path)):
        raise HTTPException(status_code=404, detail="That backup isn't waiting any more. Choose the file again.")
    if not _restore_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A restore is already running.")
    held = []
    try:
        _restoring.set()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with _inflight_lock:
                if common._inflight <= 1:       # rebound by main's restore_guard: read it here
                    break
            time.sleep(0.1)
        else:
            raise HTTPException(status_code=409, detail="The app is busy (an import or a scan is running). Try again in a minute.")
        for lock in (learning._lock, _email_scan_lock, UPLOADS_LOCK):
            if not lock.acquire(timeout=120):
                raise HTTPException(status_code=409, detail="The app is busy (an import or a scan is running). Try again in a minute.")
            held.append(lock)

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        safety_name = f"restore-safety-{stamp}.zip"
        build_database_backup(os.path.join(DATA_DIR, safety_name), blank=False)
        log.info("Restore: current data saved to %s", safety_name)
        # The automatic-backup schedule belongs to this server too: a restore
        # used to put back the backup's (usually off) without a word, and
        # changing it otherwise needs the settings password (Claude review
        # of 0148).
        kept_rows = {"https_settings": _keep_rows("https_settings"),
                     "backup_schedule": _keep_rows("backup_schedule")}
        email_kept = _keep_rows("email_ingest_settings") if settings_blanked(db_path) else None

        new_uploads = UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX
        shutil.rmtree(new_uploads, ignore_errors=True)
        kept, left_out = restore_mod.unpack_uploads(zip_path, new_uploads)

        engine.dispose()
        restore_mod.copy_into_live(db_path, DB_PATH)
        restore_mod.mark_database_replaced(DATA_DIR)  # from here, a stop is finished at start-up
        engine.dispose()
        init_db()                                     # an older backup's schema brought up to date
        for table, rows in kept_rows.items():
            _put_back_rows(table, rows)
        if email_kept:
            _put_back_blanked_email_fields(email_kept[0])
        old_uploads = restore_mod.swap_dirs(new_uploads, UPLOADS_DIR)
        restore_mod.clear_marker(DATA_DIR)
        shutil.rmtree(old_uploads, ignore_errors=True)
        email_cleared = _clear_unusable_email_password()

        db = SessionLocal()
        try:
            recipes = db.query(models.Recipe).count()
            refresh_user_keywords(db)
        finally:
            db.close()
        restore_mod.prune_safety_backups(DATA_DIR)
        log.info("Restore: %d recipes and %d files restored (%d files left out)", recipes, kept, left_out)
    except HTTPException:
        raise
    except restore_mod.RestoreError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        log.exception("Restore failed")
        raise HTTPException(status_code=500, detail="The restore failed; Settings -> Logs has the details. "
                                                    "The data from before it is in the safety backup in the data folder.")
    finally:
        # Search's word list belongs to the library just replaced (GPT
        # review of 0119); also after a failed restore, which may have
        # changed it part way.
        search.forget_vocabulary()
        picker.forget_languages()
        threading.Thread(target=picker.warm_languages, daemon=True, name="picker-languages").start()
        for lock in reversed(held):
            lock.release()
        _restoring.clear()
        _restore_lock.release()
        _remove_pending(payload.token)      # used, or failed: either way it's done with
        _clear_pending_restores()
    # After the app is open again: the learned models and the What to eat?
    # switch follow the restored data.
    try:
        learning.load()
        learning.schedule_retrain(delay=5)
        anyio.from_thread.run(picker.startup if picker.saved_enabled() else picker.stop_server)
    except Exception:
        log.exception("Restore: couldn't reapply learning or the What to eat? switch")
    return {"recipes": recipes, "files": kept, "files_left_out": left_out,
            "safety_backup": safety_name, "email_password_cleared": email_cleared}
