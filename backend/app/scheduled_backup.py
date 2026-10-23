"""Settings -> Backup & export -> Automatic backups.

Once a day, at the hour set (the container's local time, TZ), a full
backup -- the same zip as Settings -> Backup & export -> Full backup
(backup.build_database_backup) -- is written to the backup folder, and
only the newest `keep` are kept. A backup missed because the server was
off (none for 36 hours while switched on) is made at the next check.

The folder is data/backups/ by default. Since a disk that dies takes
data/ and data/backups/ with it, RECIPE_APP_BACKUP_DIR can point to a
folder on another disk mounted into the container (README, "Automatic
backups").

Only files named like the app's own backups are ever deleted, so a folder
shared with other files is safe.
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta, timezone

from .database import DATA_DIR
from .logging_setup import get_logger

log = get_logger("backup")

BACKUP_DIR = os.environ.get("RECIPE_APP_BACKUP_DIR", "").strip() or os.path.join(DATA_DIR, "backups")
NAME_FORMAT = "open-the-pantry-backup-%Y%m%d-%H%M%S.zip"
# A second backup in the same second (Back up now twice) gets -2, -3...
# rather than replacing the first.
NAME_RE = re.compile(r"^open-the-pantry-backup-(\d{8}-\d{6})(?:-(\d+))?\.zip$")


def _order(name: str):
    m = NAME_RE.match(name)
    return (m.group(1), int(m.group(2) or 1))
CHECK_EVERY = 10 * 60           # seconds between checks of the schedule
CATCH_UP_AFTER = timedelta(hours=36)
MIN_FREE_BYTES = 200 * 1024 ** 2


class BackupError(Exception):
    """A scheduled backup that couldn't be made, with a message for Settings."""


def folder() -> str:
    return BACKUP_DIR


def listing() -> list[dict]:
    """The app's backups in the folder, newest first."""
    try:
        names = [n for n in os.listdir(BACKUP_DIR) if NAME_RE.match(n)]
    except FileNotFoundError:
        return []
    out = []
    for n in sorted(names, key=_order, reverse=True):
        try:
            out.append({"name": n, "size": os.path.getsize(os.path.join(BACKUP_DIR, n))})
        except OSError:
            log.debug("listing: %s vanished", n, exc_info=True)
    return out


def prune(keep: int) -> list[str]:
    """Deletes the app's backups beyond the newest `keep`; returns their names."""
    gone = []
    for item in listing()[max(1, keep):]:
        try:
            os.remove(os.path.join(BACKUP_DIR, item["name"]))
            gone.append(item["name"])
        except OSError:
            log.warning("Automatic backup: couldn't delete old backup %s", item["name"], exc_info=True)
    return gone


def make(keep: int, build) -> dict:
    """One backup now. build(dest_path) writes the zip (backup.
    build_database_backup). Written under a temporary name and renamed when
    complete, so the folder never holds a half-written backup with a real
    name. Raises BackupError with a message for Settings."""
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
    except OSError as e:
        log.warning("Automatic backup: can't create %s", BACKUP_DIR, exc_info=True)
        raise BackupError(f"The backup folder {BACKUP_DIR} can't be created ({e.strerror or e}).")
    newest = listing()[:1]
    needed = max(MIN_FREE_BYTES, int(newest[0]["size"] * 1.5) if newest else 0)
    try:
        free = shutil.disk_usage(BACKUP_DIR).free
    except OSError as e:
        log.warning("Automatic backup: can't read %s", BACKUP_DIR, exc_info=True)
        raise BackupError(f"The backup folder {BACKUP_DIR} can't be read ({e.strerror or e}).")
    if free < needed:
        raise BackupError(f"Not enough free space in {BACKUP_DIR}: {free // 1024 ** 2} MB free, "
                          f"{needed // 1024 ** 2} MB needed.")
    stem = datetime.now().strftime(NAME_FORMAT)[:-len(".zip")]
    name, n = stem + ".zip", 1
    while os.path.exists(os.path.join(BACKUP_DIR, name)):  # runs are one at a time (the caller's lock)
        n += 1
        name = f"{stem}-{n}.zip"
    final = os.path.join(BACKUP_DIR, name)
    partial = final + ".partial"
    try:
        manifest = build(partial)
        os.replace(partial, final)
    except OSError as e:
        log.warning("Automatic backup: writing to %s failed", BACKUP_DIR, exc_info=True)
        raise BackupError(f"The backup couldn't be written to {BACKUP_DIR} ({e.strerror or e}).")
    finally:
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                log.warning("Automatic backup: couldn't remove %s", partial, exc_info=True)
    removed = prune(keep)
    size = os.path.getsize(final)
    log.info("Automatic backup: %s (%d bytes, %s recipes); removed %s", name, size,
             (manifest or {}).get("recipe_count", "?"), ", ".join(removed) or "nothing")
    return {"name": name, "size": size, "removed": removed}


def due(enabled: bool, hour: int, last_run_at: datetime | None, now: datetime | None = None) -> bool:
    """Whether a scheduled backup should run now (local time). At the set
    hour once a day; or, if none has been made for 36 hours (the server
    was off at that hour), at once."""
    if not enabled:
        return False
    now = now or datetime.now().astimezone()
    if last_run_at is not None and last_run_at.tzinfo is None:
        last_run_at = last_run_at.replace(tzinfo=timezone.utc)
    if last_run_at is None or now - last_run_at > CATCH_UP_AFTER:
        return True
    return now.hour == hour and last_run_at.astimezone(now.tzinfo).date() != now.date()
