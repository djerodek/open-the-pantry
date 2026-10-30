"""Settings -> Backup & export -> Automatic backups.

Once a day, at the hour set (the container's local time, TZ), a full
backup -- the same zip as Settings -> Backup & export -> Full backup
(backup.build_database_backup) -- is written to the backup folder, and
only the newest `keep` are kept. A backup missed because the server was
off (none for 36 hours while switched on) is made at the next check. A
failed one is tried again every hour until one works.

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
RETRY_AFTER = timedelta(hours=1)  # after a failed backup
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


def _aware(t: datetime | None) -> datetime | None:
    """SQLite gives the times back without a zone; they're stored in UTC."""
    return t.replace(tzinfo=timezone.utc) if t is not None and t.tzinfo is None else t


def due(enabled: bool, hour: int, last_success_at: datetime | None, now: datetime | None = None,
        failed_at: datetime | None = None) -> bool:
    """Whether a scheduled backup should run now (local time).

    Counted from the last backup that worked, not the last attempt
    (Claude review round 13): at the set hour when none has been made
    since that hour today (a "Back up now" earlier in the day doesn't
    count for it); at once if none has worked for 36 hours (the server was
    off at that hour); and after a failure (failed_at, the last attempt
    when it failed), again every hour until one works.

    Once a day: the set hour is compared on the clock, not as an instant,
    so the hour repeated when clocks go back doesn't make a second one
    (GPT review of 0134: 1:00 set, made at the first 1:30, due again at the
    second). The last success is read in local time with that day's
    offset: the system's zone, or now's zone when now is given."""
    if not enabled:
        return False
    to_local = (lambda d: d.astimezone(now.tzinfo)) if now is not None else (lambda d: d.astimezone())
    now = now or datetime.now().astimezone()
    last_success_at, failed_at = _aware(last_success_at), _aware(failed_at)
    if failed_at is not None and now - failed_at < RETRY_AFTER:
        return False
    # A failure after the last success is tried again, whatever the time:
    # a success earlier in the day mustn't hold the retries back until
    # tomorrow (GPT review of 0129: success at 3:00, Back up now failing at
    # 4:00, nothing more that day).
    if failed_at is not None and (last_success_at is None or failed_at > last_success_at):
        return True
    if last_success_at is None or now - last_success_at > CATCH_UP_AFTER:
        return True
    wall_now = now.replace(tzinfo=None)
    slot = wall_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > wall_now:
        slot -= timedelta(days=1)
    if to_local(last_success_at).replace(tzinfo=None) >= slot:
        return False                       # made since the latest set hour, on the clock
    return now.hour == hour or failed_at is not None
