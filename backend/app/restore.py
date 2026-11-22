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

import json
import os
import shutil
import sqlite3
import zipfile
import zlib
from datetime import datetime

from .file_validation import IMAGE_FORMATS, PDF_MAGIC, check_zip_size, detect_image_ext, is_safe_stored_filename, reencode_image
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
    check_zip_size(zip_path, MAX_ENTRIES, RestoreError)
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
    check_zip_size(zip_path, MAX_ENTRIES, RestoreError)
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
            # A backup from 0109 on can hold recipes in Recently deleted:
            # they come back there, and aren't counted with the library.
            live = ("WHERE deleted_at IS NULL"
                    if any(r[1] == "deleted_at" for r in con.execute("PRAGMA table_info(recipes)")) else "")
            recipes = con.execute(f"SELECT COUNT(*) FROM recipes {live}").fetchone()[0]
            photos = con.execute(f"SELECT COUNT(*) FROM recipes {live} {'AND' if live else 'WHERE'} "
                                 "image_path IS NOT NULL").fetchone()[0]
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
        with Image.open(path, formats=IMAGE_FORMATS) as img:
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
            # file_size is the zip's own claim, but it's also where reading
            # stops: zipfile reads an entry only up to its stated size and
            # raises BadZipFile (bad CRC) if more data follows, so a zip
            # can't unpack bigger than it says (test_security.py).
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
    old = base = f"{live_dir}-before-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    n = 1
    while os.path.exists(old):          # two restores in one second (a retry)
        old = f"{base}-{n}"
        n += 1
    os.rename(live_dir, old)
    try:
        os.rename(new_dir, live_dir)
    except OSError:
        log.error("Restore: couldn't move the restored uploads into place; put the old ones back", exc_info=True)
        os.rename(old, live_dir)
        raise
    return old


# Where a restore unpacks the backup's photos before swap_dirs moves them
# into place (routes.backup.restore_backup). A dot for this short-lived
# folder, a dash for the dated copy of the old photos kept after a swap.
STAGED_SUFFIX = ".restore-new"
# Written once the live database is the backup's, removed when the photos
# are in place too: at start-up it says which side of that line a stopped
# restore was on (DeepSeek review of 0145: stopped after the database copy
# but before the swap, the restored recipes pointed at photos still in
# the staged folder, and nothing noticed).
MARKER = "restore-in-progress.json"


def mark_database_replaced(data_dir: str) -> None:
    path = os.path.join(data_dir, MARKER)
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"stage": "database replaced", "at": datetime.now().isoformat(timespec="seconds")}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(path + ".tmp", path)


def set_aside(live_dir: str) -> set:
    """The names of live_dir's moved-aside copies (swap_dirs) there now."""
    parent, base = os.path.split(live_dir.rstrip("/"))
    return {n for n in os.listdir(parent or ".") if n.startswith(base + "-before-restore-")}


def finish_swap(new_dir: str, live_dir: str, aside_before: set) -> str:
    """After a swap_dirs that raised: the staged photos into place, whether
    it hadn't moved anything or stopped between its two renames (live_dir
    gone, one new aside copy since aside_before). Returns the old photos'
    folder. For the restore itself, which knows which copies were already
    there; start-up has to guess (recover_interrupted_restore)."""
    live = live_dir.rstrip("/")
    new_aside = sorted(set_aside(live) - aside_before)
    if not os.path.isdir(live) and len(new_aside) == 1:
        os.rename(new_dir, live)
        return os.path.join(os.path.dirname(live), new_aside[0])
    if new_aside:
        raise RestoreError("the photos were moved part way; check the uploads folders by hand")
    return swap_dirs(new_dir, live)


def clear_marker(data_dir: str) -> None:
    try:
        os.remove(os.path.join(data_dir, MARKER))
    except FileNotFoundError:
        pass


class RestoreUnresolved(Exception):
    """An interrupted restore start-up can't finish safely: which photos go
    with the library is unclear, or the folders couldn't be checked. The app
    stays closed (main.lifespan) until the owner sorts it out; the message
    is for the log."""


def _library_photos(data_dir: str) -> set[str] | None:
    """The photo names the library's recipes use (Recently deleted
    included), or None when the database can't be read."""
    path = os.path.join(data_dir, "recipes.db")
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return {r[0] for r in con.execute("SELECT image_path FROM recipes WHERE image_path IS NOT NULL")}
        finally:
            con.close()
    except sqlite3.Error:
        log.warning("Start-up: couldn't read the library's photo names", exc_info=True)
        return None


def _matching_folder(data_dir: str, folders: list[str], among: list[str] | None = None) -> str | None:
    """The first of folders holding every photo the library uses that exists
    in any of among (folders when not given): GPT review of 0221, an
    interrupted restore without its marker can often be settled this way
    rather than guessed. A photo missing from all of them (Library check's
    "photo file missing") says nothing about which folder goes with the
    library, so it doesn't count (Claude review of 0225: one such photo
    kept the app closed). When none of the library's photos exists, no
    folder fits it better than another: the first is returned. None when
    the library can't be read or no folder holds them all."""
    used = _library_photos(data_dir)
    if used is None:
        return None
    listings = {}
    for folder in dict.fromkeys(list(folders) + list(among or [])):
        try:
            listings[folder] = set(os.listdir(folder)) if os.path.isdir(folder) else None
        except OSError:
            log.warning("Start-up: couldn't list %s", folder, exc_info=True)
            listings[folder] = None
    found = set().union(*(names for names in listings.values() if names))
    existing = used & found
    for folder in folders:
        if listings[folder] is not None and existing <= listings[folder]:
            return folder
    return None


def settle_marker(data_dir: str, live_dir: str) -> bool:
    """At start-up, first: staged photos without the marker that match the
    library mean the restore had replaced the database and stopped before
    writing the marker. The marker is written now, so the settings the
    restore kept (restore-kept.json) are put back like any interrupted
    restore's before recover_interrupted_restore finishes the photos (GPT
    review of 0222: put_back_after_restart removed them unapplied, seeing
    no marker). Returns whether it wrote one. Anything it can't decide is
    left to recover_interrupted_restore."""
    staged = live_dir.rstrip("/") + STAGED_SUFFIX
    if os.path.exists(os.path.join(data_dir, MARKER)) or not os.path.isdir(staged):
        return False
    if _matching_folder(data_dir, [live_dir.rstrip("/"), staged]) != staged:
        return False
    mark_database_replaced(data_dir)
    log.warning("Start-up: a restore stopped after the library was replaced but before its marker (its staged "
                "photos match the library); marked as replaced so it's finished")
    return True


def _photos_differ(data_dir: str, a: str, b: str) -> list[str]:
    """The library's photos present in both folders whose files differ
    (size, then every byte). Matching names don't prove matching photos: a
    restored database can name the same files with other pictures (GPT
    review of 0227; the app's own names are random, so this takes a foreign
    or hand-made backup). Read in full, but only at a start-up that finds a
    restore's staged folder."""
    import filecmp
    used = _library_photos(data_dir) or set()
    differ = []
    for name in sorted(used):
        pa, pb = os.path.join(a, name), os.path.join(b, name)
        if os.path.isfile(pa) and os.path.isfile(pb) and not filecmp.cmp(pa, pb, shallow=False):
            differ.append(name)
    return differ


def staged_photos_fit(data_dir: str, live_dir: str) -> bool:
    """Staged photos are there and hold every photo the library uses. With
    restore-kept.json present that means a restore stopped at this start
    (both are removed once one is settled), possibly after replacing the
    database even when uploads/ holds the same photos (a library restored
    from its own backup), so its kept settings are put back: written just
    before the restore, they match this server either way."""
    staged = live_dir.rstrip("/") + STAGED_SUFFIX
    return os.path.isdir(staged) and _matching_folder(data_dir, [staged], [live_dir.rstrip("/"), staged]) == staged


def recover_interrupted_restore(data_dir: str, live_dir: str) -> str | None:
    """At start-up: a restore stopped part way (the container killed, the
    power cut) is finished or cleaned up so the photos match the database.

    - The database had been replaced (the marker is present): the staged
      photos go into place, as the swap would have done, whether the swap
      hadn't started or stopped between its two renames. The old photos are
      kept in uploads-before-restore-<time>, named in the log.
    - Staged photos without a marker: most likely stopped before the
      database copy, but a stop just after the copy, before the marker was
      written, looks the same. The library's own photo names decide: if
      uploads/ holds them all, the library is the old one and the staged
      folder is left (named in the log); if only the staged folder does,
      the restore had replaced it and its photos go into place. Deleting the
      staged photos could lose the restored ones (DeepSeek review of 0146).
    - Anything still unclear, or folders that can't be checked or moved,
      raises RestoreUnresolved: the app stays closed rather than run with
      photos that may not match (GPT review of 0221; it used to open).

    0144 moved the old photos back instead (wrong: the database was
    already the backup's). Returns the staged folder's former path when
    its photos were moved into live_dir (it no longer exists by then),
    otherwise None."""
    live = live_dir.rstrip("/")
    parent, base = os.path.split(live)
    staged = live + STAGED_SUFFIX
    marked = os.path.exists(os.path.join(data_dir, MARKER))
    try:
        left = sorted(n for n in os.listdir(parent or ".") if n.startswith(base + "-before-restore-"))
        empty = not os.path.isdir(live) or not os.listdir(live)
        has_staged = os.path.isdir(staged)
    except OSError as e:
        log.error("Start-up: couldn't check for an interrupted restore", exc_info=True)
        raise RestoreUnresolved(f"couldn't check the photo folders ({e})") from e
    try:
        if has_staged and not marked:
            # Settle it from the library's photo names, if they do.
            match = _matching_folder(data_dir, [live, staged])
            if match == live and _matching_folder(data_dir, [staged], [live, staged]) == staged:
                differ = _photos_differ(data_dir, live, staged)
                if differ:
                    log.error("Start-up: %s and uploads/ both hold the library's photos, but %d of them differ "
                              "(%s); the app stays closed: stop it, check which folder belongs with recipes.db, "
                              "and move or delete %s.", os.path.basename(staged), len(differ),
                              ", ".join(differ[:5]), os.path.basename(staged))
                    raise RestoreUnresolved("uploads/ and the staged folder name the same photos with different files")
            if match == live:
                log.warning("Start-up: %s holds staged photos from a restore that stopped before the library "
                            "was replaced (uploads/ has every photo of the library's that exists); delete the "
                            "folder when you like", os.path.basename(staged))
                return None
            if match == staged:
                log.error("Start-up: a restore stopped after the library was replaced but before its marker; "
                          "its photos match the library, so they're being put in place")
                marked = True
            else:
                log.error("Start-up: %s holds staged photos from a restore that stopped, and neither it nor "
                          "uploads/ has every photo of the library's that exists. The app stays closed: stop "
                          "it, check which folder belongs with recipes.db (or restore a backup again by hand, "
                          "see the README), and move or delete %s.", os.path.basename(staged),
                          os.path.basename(staged))
                raise RestoreUnresolved("staged photos without a marker, matching neither the library nor uploads/")
        if has_staged and empty and len(left) == 1 and marked:
            # Between the two renames: the old photos are already aside.
            if os.path.isdir(live):
                os.rmdir(live)
            os.rename(staged, live)
            log.error("Start-up: finished a restore that was interrupted: the backup's photos are in place; "
                      "the photos from before it are in %s (delete it once the library looks right)", left[0])
            clear_marker(data_dir)
            return staged
        if has_staged and empty and left:
            # More than one old folder (GPT review of 0147: moving the
            # staged photos could pair them with the wrong library).
            log.error("Start-up: an interrupted restore left %s and %s with uploads/ empty; not moved, and "
                      "the app stays closed: check which belongs with recipes.db by hand",
                      os.path.basename(staged), ", ".join(left))
            raise RestoreUnresolved("staged photos and more than one old photo folder, uploads/ empty")
        if has_staged and marked:
            # The database was replaced, the swap hadn't started.
            old = swap_dirs(staged, live)
            log.error("Start-up: finished a restore that was interrupted before its photos were moved: the "
                      "backup's photos are in place; the photos from before it are in %s (delete it once the "
                      "library looks right)", os.path.basename(old))
            clear_marker(data_dir)
            return staged
        if marked:
            clear_marker(data_dir)          # the photos had been moved; only the clean-up was left
    except OSError as e:
        log.error("Start-up: couldn't finish an interrupted restore (%s); the app stays closed", staged,
                  exc_info=True)
        raise RestoreUnresolved(f"couldn't move the photo folders ({e})") from e
    for name in left:
        # After a finished restore whose old-photo copy wasn't removed:
        # expected, so info; without the marker, someone's folders: warning.
        (log.info if marked else log.warning)(
            "Start-up: %s left by an earlier restore (the restore itself had finished); delete it once "
            "you've checked the photos", name)
    return None


def prune_safety_backups(data_dir: str, keep: int = SAFETY_KEEP):
    names = sorted(n for n in os.listdir(data_dir) if n.startswith("restore-safety-") and n.endswith(".zip"))
    for n in names[:-keep] if keep else names:
        try:
            os.remove(os.path.join(data_dir, n))
        except OSError:
            log.warning("Couldn't remove old safety backup %s", n, exc_info=True)
