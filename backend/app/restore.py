"""Settings -> Backup & export -> Restore from backup.

Puts a full backup (backup.build_database_backup's zip) back without
stopping the app. The manual way (stop, unzip, start; RESTORE.txt) still
works and is the fallback when the app won't start.

Steps (main.restore_backup drives them, with the app quiet):
1. inspect(): the zip is this app's full backup. Only these entries are
   read: recipes.db, manifest.json and uploads/<name> where the name is
   one the app could have stored. Nothing is written from the zip's own
   paths. Limits on entry count and sizes. recipes.db must pass SQLite's
   integrity_check and have the app's tables.
2. A safety backup of the current data is written to data/ first
   (restore-safety-<time>.zip, the newest three kept), so a wrong file can
   be undone by restoring that.
3. The database is replaced in place through SQLite's backup API (the
   same API the backups are made with): page by page into the live file,
   under SQLite's own locking, while nothing else in the app is running.
4. uploads/ is replaced by the backup's photos and PDFs, checked to the
   upload standard (_content_ok).
5. What belongs to this server, not to the backup, is kept: the HTTPS
   settings row (its certificate lives in data/https/ here), and an email
   password this server's key can't decrypt is cleared (ingest switched
   off) instead of being left unusable.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import zipfile
import zlib
from datetime import datetime

from .file_validation import PDF_MAGIC, detect_image_ext, is_safe_stored_filename, reencode_image
from .logging_setup import get_logger

log = get_logger("restore")

MAX_ENTRIES = 50_000
MAX_DB_BYTES = 4 * 1024 ** 3
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 ** 3
MAX_MANIFEST_BYTES = 1024 * 1024   # the app's own lists missing photos; a few KB in practice
REQUIRED_TABLES = {"recipes", "ingredients", "steps", "tags", "recipe_tags"}
SAFETY_KEEP = 3


class RestoreError(ValueError):
    """The file can't be restored from; nothing was changed."""


def kind_of(zip_path: str) -> str:
    """"backup" for this app's full backup, "import" for anything else the
    import path reads (schema.org JSON zip, Paprika). Raises RestoreError
    for a file that is neither."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
    except zipfile.BadZipFile:
        raise RestoreError("This file is damaged or isn't a zip.")
    if "recipes.db" in names:
        return "backup"
    return "import"


def inspect(zip_path: str, db_path: str) -> dict:
    """Checks a full backup and unpacks its database to db_path. Returns
    what the screen shows before confirming."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise RestoreError("This file is damaged or isn't a zip.")
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise RestoreError(f"This archive has {len(infos)} files; the limit is {MAX_ENTRIES}.")
        if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
            raise RestoreError("This archive unpacks to more than 16 GB.")
        try:
            db_info = zf.getinfo("recipes.db")
        except KeyError:
            raise RestoreError("This isn't a full backup from Open the Pantry (no recipes.db inside).")
        if db_info.file_size > MAX_DB_BYTES:
            raise RestoreError("The database in this backup is larger than 4 GB.")
        uploads = [i for i in infos if i.filename.startswith("uploads/") and not i.is_dir()]
        good_uploads = [i for i in uploads if is_safe_stored_filename(i.filename[len("uploads/"):])
                        and i.file_size <= MAX_FILE_BYTES]
        try:
            with zf.open(db_info) as src, open(db_path, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
        except (zipfile.BadZipFile, zlib.error, EOFError):
            log.info("inspect: recipes.db in the archive is damaged", exc_info=True)
            raise RestoreError("This backup is damaged (recipes.db can't be read from it).")
        created = None
        try:
            import json
            info = zf.getinfo("manifest.json") if "manifest.json" in zf.namelist() else None
            # Only read for its date: one that's larger than any the app
            # writes is ignored, not read into memory (GPT review of 0093).
            if info is not None and info.file_size <= MAX_MANIFEST_BYTES:
                manifest = json.loads(zf.read(info).decode("utf-8"))
                created = manifest.get("created_at") if isinstance(manifest, dict) else None
            elif info is not None:
                log.info("inspect: manifest.json is %d bytes; ignored", info.file_size)
        except Exception:
            log.debug("inspect: unreadable manifest.json, ignored", exc_info=True)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            ok = con.execute("PRAGMA integrity_check").fetchone()
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not ok or ok[0] != "ok":
                raise RestoreError("The database in this backup is damaged (integrity check failed).")
            if not REQUIRED_TABLES <= tables:
                raise RestoreError("The database in this backup isn't Open the Pantry's.")
            recipes = con.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            photos = con.execute("SELECT COUNT(*) FROM recipes WHERE image_path IS NOT NULL").fetchone()[0]
        finally:
            con.close()
    except sqlite3.DatabaseError:
        raise RestoreError("The database in this backup can't be opened.")
    return {"kind": "backup", "recipes": recipes, "photos": photos, "files": len(good_uploads),
            "files_left_out": len(uploads) - len(good_uploads), "created_at": created}


# What a whole image file ends with, by format. A file the app stored was
# re-encoded on the way in, so it ends exactly there.
_IMAGE_END = {".jpg": b"\xff\xd9", ".png": b"IEND\xaeB`\x82", ".gif": b";"}


def _ends_clean(path: str, ext: str) -> bool:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if ext == ".webp":
            head = f.read(8)
            return int.from_bytes(head[4:8], "little") + 8 == size
        tail = _IMAGE_END[ext]
        f.seek(max(0, size - len(tail)))
        return f.read() == tail


# What the upload re-encoding itself leaves in a file (reencode_image):
# format and timing fields, no content about the photo.
_KEPT_INFO = {"jfif", "jfif_density", "jfif_unit", "jfif_version", "dpi", "progressive", "progression",
              "background", "version", "duration", "loop", "timestamp", "transparency", "gamma", "aspect"}


def _metadata(img) -> list[str]:
    """Metadata a re-encoded upload wouldn't have (EXIF with its camera and
    location fields, XMP, colour profiles, comments, text chunks), by name;
    empty for a photo the app stored (GPT review of 0095: a JPEG ending
    cleanly kept its EXIF)."""
    found = sorted(k for k in img.info if k not in _KEPT_INFO)
    found += [m for m, data in getattr(img, "applist", []) if not (m == "APP0" and data.startswith(b"JFIF"))]
    if getattr(img, "text", None):
        found.append("text")
    return sorted(set(found))


def _content_ok(path: str, name: str) -> bool:
    """The same standard as an upload (GPT review of 0093: only the first
    bytes were checked). The type read from the content must match the
    name's extension, and a PDF must be named .pdf. An image must decode
    in full; one with anything after its image data (trailing bytes, a
    polyglot) or metadata (_metadata) goes through the upload re-encoding,
    which drops it. A file
    the app stored was already re-encoded, so it's kept byte for byte:
    re-saving every JPEG on each restore would lose a little each time."""
    ext = os.path.splitext(name)[1].lower()
    with open(path, "rb") as f:
        head = f.read(16)
    if head.startswith(PDF_MAGIC):
        return ext == ".pdf"
    kind = detect_image_ext(head)
    if kind is None or kind != ext:
        return False
    try:
        from PIL import Image
        with Image.open(path, formats=["JPEG", "PNG", "GIF", "WEBP"]) as img:
            img.load()
            extra = _metadata(img)
        if extra or not _ends_clean(path, kind):
            log.info("Restore: %s has %s; re-encoded like an upload", name,
                     ", ".join(extra) if extra else "data after the image")
            reencode_image(path)
    except Exception:
        log.info("Restore: %s isn't a readable image; left out", name, exc_info=True)
        return False
    return True


def unpack_uploads(zip_path: str, dest_dir: str) -> tuple[int, int]:
    """The backup's photos and PDFs into dest_dir (empty, new): only names
    the app could have stored, only content that is an image or a PDF.
    Returns (kept, left out)."""
    kept = skipped = 0
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith("uploads/"):
                continue
            name = info.filename[len("uploads/"):]
            if not is_safe_stored_filename(name) or info.file_size > MAX_FILE_BYTES:
                skipped += 1
                continue
            target = os.path.join(dest_dir, name)
            try:
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
            except (zipfile.BadZipFile, zlib.error, EOFError):
                log.warning("Restore: %s is damaged in the backup, left out", name, exc_info=True)
                if os.path.exists(target):
                    os.remove(target)
                skipped += 1
                continue
            if not _content_ok(target, name):
                os.remove(target)
                skipped += 1
                continue
            kept += 1
    return kept, skipped


def copy_into_live(source_db: str, live_db: str):
    """Replaces the live database's content with source_db's, through the
    SQLite backup API, inside SQLite's locking. The live file stays in WAL
    mode; page sizes must match for that, so the source is rewritten to the
    live page size first when they differ."""
    live = sqlite3.connect(live_db, timeout=30)
    try:
        live_page = live.execute("PRAGMA page_size").fetchone()[0]
        src = sqlite3.connect(source_db)
        try:
            if src.execute("PRAGMA page_size").fetchone()[0] != live_page:
                src.execute("PRAGMA journal_mode=DELETE")
                src.execute(f"PRAGMA page_size={int(live_page)}")
                src.execute("VACUUM")
            src.backup(live)
        finally:
            src.close()
        live.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        live.close()


def swap_dirs(new_dir: str, live_dir: str) -> str:
    """Moves live_dir aside and new_dir into its place. Returns where the
    old one went (deleted by the caller once the restore succeeded)."""
    old = f"{live_dir}-before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    os.rename(live_dir, old)
    try:
        os.rename(new_dir, live_dir)
    except OSError:
        log.error("Restore: couldn't move the restored uploads into place; put the old ones back", exc_info=True)
        os.rename(old, live_dir)
        raise
    return old


def prune_safety_backups(data_dir: str, keep: int = SAFETY_KEEP):
    names = sorted(n for n in os.listdir(data_dir) if n.startswith("restore-safety-") and n.endswith(".zip"))
    for n in names[:-keep] if keep else names:
        try:
            os.remove(os.path.join(data_dir, n))
        except OSError:
            log.warning("Couldn't remove old safety backup %s", n, exc_info=True)
