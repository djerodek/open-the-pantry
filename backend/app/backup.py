"""Backup / export bundles.

Two shapes, for two different jobs:

  * database.zip -- the SQLite database plus every uploaded file. This is the
    one you restore from: put it back and you have the app exactly as it was,
    ratings, notes, showcase photos and all.

  * pdfs.zip -- one PDF per recipe. This one does NOT restore; it is the
    archive you keep so the recipes outlive the app. PDFs open on anything,
    forever, with no Docker and no SQLite.

Both are written to a temp file and streamed, never assembled in memory: a
library with a few hundred photos is comfortably larger than it would be
sensible to hold in RAM on a NAS.
"""

import csv
import io
import json
import os
import re
import sqlite3
import threading
import zipfile
from datetime import datetime, timezone

from .database import DB_PATH, UPLOADS_DIR
from .file_validation import safe_join
from .export import render_recipe_pdf
from . import i18n
from .logging_setup import get_logger

log = get_logger("backup")


# Keeps a recipe title usable as a filename on Windows, macOS and Linux
# alike: the Windows-reserved set is the strictest, so satisfying it
# satisfies the others.
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_pdf_filename(title: str, recipe_id: int) -> str:
    """A filename that survives extraction on any OS.

    The id prefix keeps the archive stably ordered and, more importantly,
    keeps two recipes with the same title from colliding inside the zip --
    "Mum's Pie" twice would otherwise silently become one file.
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("", title or "").strip().strip(".")
    cleaned = re.sub(r"\s+", " ", cleaned)
    # Leave room for the id prefix and the extension inside the common
    # 255-byte filename limit.
    cleaned = cleaned[:120] or "recipe"
    return f"{recipe_id:05d} - {cleaned}.pdf"


# Never written into a backup, downloaded or automatic (owner's decision
# after the Claude security audit of 0140: the backup download needs no
# password, and held what the settings password guards): the email login
# and its encrypted password, the subject keyword and allowed senders that
# decide which emails are read, the notification address and the people
# recipes were emailed to, and the HTTPS name and addresses. A restore
# keeps the server's own (routes.backup); a new server needs them entered
# again. Other settings (mail server names and ports, scan hour) stay; a
# restore uses them only on a server with no email login (routes.backup).
REDACTED_SETTINGS = {
    "email_ingest_settings": {"enabled": 0, "username": None, "password_encrypted": None, "notify_email": None,
                              "subject_keyword": "", "allowed_senders": "", "recent_recipients": "[]",
                              "last_problem": None},
    "https_settings": {"domain": None, "lan_address": None, "email": None, "a_record_value": None,
                       "last_error": None},
}


# In a backup's database only: version 1, this backup's settings were
# blanked; version 0 (from 0156), a full copy (a restore's safety copy),
# which an unconfigured server's would otherwise look like (GPT review of
# 0155). init_db drops it from the live database after a restore.
REDACTED_MARK_TABLE = "backup_settings_blanked"


def _write_mark(conn, version: int) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {REDACTED_MARK_TABLE} (version INTEGER NOT NULL)")
    conn.execute(f"DELETE FROM {REDACTED_MARK_TABLE}")
    conn.execute(f"INSERT INTO {REDACTED_MARK_TABLE} (version) VALUES (?)", (version,))


def blank_settings(db_path: str) -> None:
    """REDACTED_SETTINGS applied to a snapshot, then VACUUM, so the old
    values aren't left in the file's free pages either."""
    conn = sqlite3.connect(db_path)
    try:
        for table, values in REDACTED_SETTINGS.items():
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            values = {k: v for k, v in values.items() if k in cols}
            if values:
                conn.execute(f"UPDATE {table} SET " + ", ".join(f"{k} = ?" for k in values), list(values.values()))
        # Waiting result lines quote email subjects, keyword included
        # (Claude review of 0148).
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'email_notification_queue'").fetchone():
            conn.execute("DELETE FROM email_notification_queue")
        # Says so in the backup itself, for a restore to read (GPT review of
        # 0147: guessing from four blank fields took an older backup with
        # no keyword or sender list for a blanked one).
        _write_mark(conn, 1)
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def settings_blanked(db_path: str) -> bool:
    """Whether a backup's email settings were blanked: REDACTED_MARK_TABLE
    says so (0148 on; 0 = a full copy, anything else is taken as blanked,
    which keeps this server's settings). Without it (an older backup), every
    field blank_settings clears must be as it leaves them, so a backup that
    merely had no keyword or sender list keeps its notification address
    and recipients on restore (GPT review of 0147)."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (REDACTED_MARK_TABLE,)).fetchone():
            mark = conn.execute(f"SELECT version FROM {REDACTED_MARK_TABLE} LIMIT 1").fetchone()
            return not (mark and mark[0] == 0)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(email_ingest_settings)")}
        if not cols:
            return True
        fields = [k for k in REDACTED_SETTINGS["email_ingest_settings"] if k in cols]
        row = conn.execute(f"SELECT {', '.join(fields)} FROM email_ingest_settings LIMIT 1").fetchone()
    except sqlite3.Error:
        log.info("Restore: couldn't read the backup's email settings", exc_info=True)
        return True
    finally:
        conn.close()
    blank = REDACTED_SETTINGS["email_ingest_settings"]
    return row is None or all(v == blank[k] for k, v in zip(fields, row))


def snapshot_database(dest_path: str) -> None:
    """Write a consistent copy of the live database to dest_path.

    This uses SQLite's online backup API rather than copying recipes.db,
    and that distinction is the whole reason this function exists. The app
    runs in WAL mode, so at any moment an unknown amount of committed data
    lives in recipes.db-wal and not in recipes.db. Copying the main file
    alone yields a backup that is silently missing recent recipes, or -- if
    a checkpoint happens to be in flight -- torn.

    The backup API reads through the WAL and produces a single, internally
    consistent file, while readers and writers carry on. It is the only
    approach here that is safe without stopping the app.

    The destination is then explicitly checkpointed and taken out of WAL
    mode before closing. The backup inherits the source's journal mode, so
    without this the snapshot is a recipes.db plus its own -wal/-shm
    sidecars -- and the archive only carries recipes.db. Relying on "SQLite
    checkpoints when the last connection closes" would make the completeness
    of the backup an implementation detail; doing it here makes it a
    guarantee.
    """
    source = sqlite3.connect(DB_PATH)
    try:
        dest = sqlite3.connect(dest_path)
        try:
            source.backup(dest)
            dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # DELETE mode removes the -wal/-shm files outright, leaving one
            # self-contained file to put in the zip.
            dest.execute("PRAGMA journal_mode=DELETE")
        finally:
            dest.close()
    finally:
        source.close()

    # Belt and braces: if the pragmas above were ever skipped or failed, the
    # sidecars would otherwise accumulate in the data directory.
    for suffix in ("-wal", "-shm"):
        sidecar = dest_path + suffix
        if os.path.isfile(sidecar):
            os.remove(sidecar)


RESTORE_INSTRUCTIONS = """\
Open the Pantry -- restoring this backup
========================================

This archive contains a complete copy of your recipe library:

  recipes.db    the database (recipes, tags, ratings, notes, settings)
  uploads/      every showcase photo and uploaded PDF
  manifest.json what was in it, and when it was taken

Left out on purpose: the email login and password, subject keyword,
allowed senders and recipients, and the HTTPS name. Restoring on this
server keeps its own; on a new server, enter them again in Settings.

To restore
----------

The easy way, while the app is running: Settings -> Backup & export ->
Restore or import, choose this file, then Replace my library. A safety copy
of the current library is written to the data folder first
(restore-safety-<date>.zip).

By hand (when the app won't start, or onto a new install):

1. Stop the app:

       docker compose down

2. Find the data folder: it is the LEFT side of the "volumes:" line in
   docker-compose.yml (./data by default). Unpack this archive into it,
   replacing what is there, so that recipes.db and uploads/ sit directly
   inside that folder -- not in a subfolder:

       unzip -o open-the-pantry-backup-YYYYMMDD-HHMMSS.zip -d ./data

3. Start the app again:

       docker compose up -d

The app is stopped for step 2 on purpose. Replacing the database underneath
a running app is how you corrupt it -- the running process still holds open
handles to the old file.

Restoring onto a different drive or path
----------------------------------------

Same steps, but unzip into the new folder and change the left side of the
volumes line to point at it. Leave the right side (/app/data) alone:

    volumes:
      - /path/to/new/folder:/app/data

Notes
-----

* Restoring REPLACES the current library. Anything added since this backup
  was taken is gone. If you are unsure, copy your existing data folder
  somewhere else first.
* recipes.db is an ordinary SQLite database. You can open it with any SQLite
  tool if you ever want the data out without running this app.
* Email ingest and HTTPS: this archive holds no email login or password and
  no cPanel token (see above), so encryption.key isn't needed to restore it.
  On a new server, restore first, then enter the email login and password
  in Settings: the mail server names and ports come back from the backup
  while the server has no email login. One that has keeps all its own.
"""


# The French copy of RESTORE_INSTRUCTIONS. Change both together;
# test_ui_consistency.py checks that they give the same commands.
RESTORE_INSTRUCTIONS_FR = """\
Open the Pantry -- restaurer cette sauvegarde
=============================================

Cette archive contient une copie complète de votre bibliothèque de recettes :

  recipes.db    la base de données (recettes, étiquettes, notes, évaluations, paramètres)
  uploads/      toutes les photos et tous les PDF envoyés
  manifest.json son contenu, et la date de la sauvegarde

Laissés de côté exprès : l'identifiant et le mot de passe de courriel, le
mot-clé d'objet, les expéditeurs permis et destinataires, et le nom HTTPS.
Une restauration sur ce serveur garde les siens; sur un nouveau serveur,
entrez-les de nouveau dans les Paramètres.

Pour restaurer
--------------

La façon simple, pendant que l'application fonctionne : Paramètres ->
Sauvegarde et exportation -> Restaurer ou importer, choisissez ce fichier,
puis Remplacer ma bibliothèque. Une copie de sécurité de la bibliothèque
actuelle est d'abord écrite dans le dossier de données
(restore-safety-<date>.zip).

À la main (quand l'application ne démarre pas, ou sur une nouvelle
installation) :

1. Arrêtez l'application :

       docker compose down

2. Trouvez le dossier de données : c'est le côté GAUCHE de la ligne « volumes: »
   dans docker-compose.yml (./data par défaut). Décompressez cette archive
   dedans, en remplaçant ce qui s'y trouve, pour que recipes.db et uploads/
   soient directement dans ce dossier, et non dans un sous-dossier :

       unzip -o open-the-pantry-backup-AAAAMMJJ-HHMMSS.zip -d ./data

3. Redémarrez l'application :

       docker compose up -d

L'application est arrêtée à l'étape 2 exprès. Remplacer la base de données
pendant que l'application fonctionne peut la corrompre : le processus garde
le fichier précédent ouvert.

Restaurer vers un autre disque ou un autre chemin
-------------------------------------------------

Mêmes étapes, mais décompressez dans le nouveau dossier et modifiez le côté
gauche de la ligne du volume pour qu'il y mène. Ne touchez pas au côté droit
(/app/data) :

    volumes:
      - /chemin/vers/nouveau/dossier:/app/data

Remarques
---------

* La restauration REMPLACE la bibliothèque actuelle. Tout ce qui a été ajouté
  depuis cette sauvegarde disparaît. En cas de doute, copiez d'abord votre
  dossier de données actuel ailleurs.
* recipes.db est une base de données SQLite ordinaire. N'importe quel outil
  SQLite peut l'ouvrir si vous voulez récupérer les données sans cette
  application.
* Réception par courriel et HTTPS : cette archive ne contient ni identifiant
  ni mot de passe de courriel, ni jeton cPanel (voir plus haut); encryption.key
  n'est donc pas nécessaire pour la restaurer. Sur un nouveau serveur,
  restaurez d'abord, puis entrez l'identifiant et le mot de passe de courriel
  dans les Paramètres : les noms et ports des serveurs de courriel reviennent
  de la sauvegarde tant que le serveur n'a pas d'identifiant de courriel.
  Celui qui en a un garde tous les siens.
"""


# Held while a full backup is built, and by every removal of a file from
# uploads/. Without it, a recipe deleted between the database snapshot and
# the copying of photos left a backup whose database pointed at a photo the
# archive didn't contain. Deletes wait (seconds, at most) rather than the
# backup being inconsistent. RLock: harmless if a holder re-enters.
UPLOADS_LOCK = threading.RLock()


def remove_upload_file(path: str) -> None:
    """The only way a file in uploads/ should be deleted -- see UPLOADS_LOCK."""
    with UPLOADS_LOCK:
        if os.path.isfile(path):
            os.remove(path)


# The safety copy a restore writes first (blank=False) keeps the settings,
# credentials included, encrypted with this server's encryption.key: its
# RESTORE.txt says so instead of "left out" (DeepSeek review of 0149).
_SAFETY_COPY_NOTE = {
    "en": ("This is the safety copy written before a restore: unlike a downloaded\n"
           "backup, its database keeps the email login and password (encrypted with\n"
           "this server's encryption.key) and the other settings. Restored on this\n"
           "server they work as before; onto another server, also copy\n"
           "encryption.key from this server's data folder, or enter the email\n"
           "password again. The HTTPS certificate and the cPanel login aren't in\n"
           "it (data/https/, with cpanel-login.json): set HTTPS up again in Settings\n"
           "-> HTTPS on another server."),
    "fr": ("Ceci est la copie de sécurité écrite avant une restauration : contrairement\n"
           "à une sauvegarde téléchargée, sa base de données garde l'identifiant et le\n"
           "mot de passe de courriel (chiffré avec le fichier encryption.key de ce\n"
           "serveur) et les autres paramètres. Restaurés sur ce serveur, ils\n"
           "fonctionnent comme avant; sur un autre serveur, copiez aussi encryption.key\n"
           "depuis le dossier de données de ce serveur, ou entrez de nouveau le mot de\n"
           "passe de courriel. Le certificat HTTPS et l'identifiant cPanel n'y sont pas\n"
           "(data/https/, avec cpanel-login.json) : configurez de nouveau HTTPS dans\n"
           "Paramètres -> HTTPS sur un autre serveur."),
}
_LEFT_OUT = {
    "en": ("Left out on purpose: the email login and password, subject keyword,\n"
           "allowed senders and recipients, and the HTTPS name. Restoring on this\n"
           "server keeps its own; on a new server, enter them again in Settings."),
    "fr": ("Laissés de côté exprès : l'identifiant et le mot de passe de courriel, le\n"
           "mot-clé d'objet, les expéditeurs permis et destinataires, et le nom HTTPS.\n"
           "Une restauration sur ce serveur garde les siens; sur un nouveau serveur,\n"
           "entrez-les de nouveau dans les Paramètres."),
}
_EMAIL_BULLET = {
    "en": ("* Email ingest and HTTPS: this archive holds no email login or password and\n"
           "  no cPanel token (see above), so encryption.key isn't needed to restore it.\n"
           "  On a new server, restore first, then enter the email login and password\n"
           "  in Settings: the mail server names and ports come back from the backup\n"
           "  while the server has no email login. One that has keeps all its own.\n"),
    "fr": ("* Réception par courriel et HTTPS : cette archive ne contient ni identifiant\n"
           "  ni mot de passe de courriel, ni jeton cPanel (voir plus haut); encryption.key\n"
           "  n'est donc pas nécessaire pour la restaurer. Sur un nouveau serveur,\n"
           "  restaurez d'abord, puis entrez l'identifiant et le mot de passe de courriel\n"
           "  dans les Paramètres : les noms et ports des serveurs de courriel reviennent\n"
           "  de la sauvegarde tant que le serveur n'a pas d'identifiant de courriel.\n"
           "  Celui qui en a un garde tous les siens.\n"),
}
_SAFETY_EMAIL_BULLET = {
    "en": ("* Email ingest and HTTPS: see the note on the safety copy above.\n"),
    "fr": ("* Réception par courriel et HTTPS : voir la note sur la copie de sécurité\n"
           "  plus haut.\n"),
}


def restore_instructions(lang: str, blank: bool = True) -> str:
    """RESTORE.txt / RESTAURER.txt for a backup, or for a restore's safety copy."""
    key = "fr" if lang == "fr" else "en"
    text = RESTORE_INSTRUCTIONS_FR if key == "fr" else RESTORE_INSTRUCTIONS
    if blank:
        return text
    for old in (_LEFT_OUT[key], _EMAIL_BULLET[key]):
        if old not in text:
            raise RuntimeError("restore_instructions: RESTORE text changed; update _LEFT_OUT/_EMAIL_BULLET")
    return (text.replace(_LEFT_OUT[key], _SAFETY_COPY_NOTE[key])
            .replace(_EMAIL_BULLET[key], _SAFETY_EMAIL_BULLET[key]))


def build_database_backup(dest_zip_path: str, blank: bool = True) -> dict:
    """Database + uploads, as a restorable zip. Returns manifest counts."""
    tmp_db = dest_zip_path + ".db.tmp"
    try:
        with UPLOADS_LOCK:
            return _build_database_backup_locked(dest_zip_path, tmp_db, blank)
    finally:
        if os.path.isfile(tmp_db):
            os.remove(tmp_db)


def _build_database_backup_locked(dest_zip_path: str, tmp_db: str, blank: bool = True) -> dict:
    """The archive contains exactly the photos the snapshot refers to.

    It used to contain whatever was in uploads/ when the folder was listed,
    after the snapshot: a photo promoted after the snapshot went in with
    nothing referring to it, and one deleted in between was referenced but
    missing. Now the list comes from the snapshot itself, and deletes wait
    on UPLOADS_LOCK until the copy is done. Anything referenced but absent
    anyway (removed outside the app) is listed in the manifest and logged,
    not silently dropped.
    """
    from .file_validation import is_safe_stored_filename

    snapshot_database(tmp_db)
    # Not for restore's own safety copy (blank=False): it stays in the data
    # folder beside encryption.key, and is what puts this server's settings
    # back if the restore is undone (Claude review of 0148).
    if blank:
        blank_settings(tmp_db)
    else:
        with sqlite3.connect(tmp_db) as full:
            _write_mark(full, 0)
    with sqlite3.connect(tmp_db) as snap:
        # Recipes in Recently deleted are in the backup (and come back there
        # on a restore) but aren't counted as the library's.
        recipe_count = snap.execute("SELECT COUNT(*) FROM recipes WHERE deleted_at IS NULL").fetchone()[0]
        referenced = sorted({
            r[0] for r in snap.execute("SELECT image_path FROM recipes WHERE image_path IS NOT NULL")
        })

    upload_names, missing = [], []
    for name in referenced:
        path = os.path.join(UPLOADS_DIR, name)
        if is_safe_stored_filename(name) and os.path.isfile(path):
            upload_names.append(name)
        else:
            missing.append(name)
    on_disk = {n for n in os.listdir(UPLOADS_DIR) if os.path.isfile(os.path.join(UPLOADS_DIR, n))} \
        if os.path.isdir(UPLOADS_DIR) else set()
    unreferenced = len(on_disk - set(upload_names))

    # ZIP_DEFLATED on a SQLite file is worth it (they compress well);
    # already-compressed JPEGs and PDFs simply won't shrink much, which
    # costs a little CPU and no correctness.
    with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(tmp_db, "recipes.db")
        # Deletes through the app wait on UPLOADS_LOCK, but a file removed
        # outside the app between the check above and here is recorded as
        # missing instead of failing the whole backup.
        copied, uploads_bytes = [], 0
        for name in upload_names:
            path = os.path.join(UPLOADS_DIR, name)
            try:
                size = os.path.getsize(path)
                zf.write(path, f"uploads/{name}")
            except FileNotFoundError:
                missing.append(name)
                continue
            copied.append(name)
            uploads_bytes += size
        upload_names = copied
        if missing:
            log.warning("Backup: %d referenced photo(s) not found in uploads/: %s", len(missing), missing[:20])
        manifest = _manifest(recipe_count, upload_names, uploads_bytes, tmp_db, missing, unreferenced)
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        # In the language of the request that asked for the backup.
        lang = "fr" if i18n.current_lang.get() == "fr" else "en"
        zf.writestr("RESTAURER.txt" if lang == "fr" else "RESTORE.txt", restore_instructions(lang, blank))

    return manifest


def _manifest(recipe_count, upload_names, uploads_bytes, tmp_db, missing, unreferenced) -> dict:
    return {
        "application": "open-the-pantry",
        "backup_type": "database",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "recipe_count": recipe_count,
        "upload_count": len(upload_names),
        "uploads_bytes": uploads_bytes,
        "database_bytes": os.path.getsize(tmp_db),
        "missing_uploads": missing,
        "unreferenced_uploads_left_out": unreferenced,
        "restores_with": "voir RESTAURER.txt" if i18n.current_lang.get() == "fr" else "see RESTORE.txt",
    }


# A cell a spreadsheet would read as a formula: =, +, -, @, tab, CR, LF,
# their full-width forms (some locales), also after leading spaces (GPT
# review of 0155; OWASP CSV injection). Prefixed with ' so it's shown as
# text.
_CSV_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n", "＝", "＋", "－", "＠")


def _csv_safe(value):
    if isinstance(value, str) and (value[:1] in _CSV_FORMULA_START or value.lstrip(" ")[:1] in _CSV_FORMULA_START):
        return "'" + value
    return value


def build_pdf_bundle(recipes, dest_zip_path: str, include_notes: bool = True) -> dict:
    """One PDF per recipe, plus an index. Does not restore -- see module docstring.

    A recipe that fails to render is recorded in the index and skipped rather
    than aborting the archive: one malformed recipe should not cost you a
    backup of the other two hundred.
    """
    index_rows = []
    failures = 0

    with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for recipe in recipes:
            filename = safe_pdf_filename(recipe.title, recipe.id)
            try:
                image_path = None
                if recipe.image_path:
                    # safe_join, like every other place a stored filename
                    # meets the filesystem: image_path is validated on the
                    # way in, but a database restored from an older backup
                    # or edited by hand isn't.
                    candidate = safe_join(UPLOADS_DIR, recipe.image_path)
                    if candidate and os.path.isfile(candidate):
                        image_path = candidate
                # The showcase image IS included here, unlike the share
                # export: this is your own archive, not something handed to
                # someone else, and a recipe card reads better with its photo.
                pdf_bytes = render_recipe_pdf(
                    recipe, image_path, include_notes=include_notes, include_image=True,
                )
                zf.writestr(filename, pdf_bytes)
                index_rows.append({
                    "id": recipe.id, "title": recipe.title or "",
                    "file": filename, "status": "ok",
                    "source_type": recipe.source_type or "",
                })
            except Exception as exc:  # noqa: BLE001 - deliberately broad, see docstring
                log.warning("build_pdf_bundle: caught error, continuing", exc_info=True)
                failures += 1
                index_rows.append({
                    "id": recipe.id, "title": recipe.title or "",
                    "file": "", "status": f"failed: {type(exc).__name__}",
                    "source_type": recipe.source_type or "",
                })

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["id", "title", "file", "status", "source_type"])
        writer.writeheader()
        # A title from a web page or an email starting with = + - @ (or a tab
        # or CR) would run as a formula in a spreadsheet (Claude review of
        # 0148): such cells start with an apostrophe.
        writer.writerows({k: _csv_safe(v) for k, v in row.items()} for row in index_rows)
        zf.writestr("index.csv", buf.getvalue())

        manifest = {
            "application": "open-the-pantry",
            "backup_type": "pdfs",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "recipe_count": len(index_rows),
            "pdf_count": len(index_rows) - failures,
            "failed_count": failures,
            "includes_notes": include_notes,
            "restorable": False,
            "note": "A reading archive, not a backup you can restore. "
                    "Use the database backup to restore into the app.",
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    return manifest
