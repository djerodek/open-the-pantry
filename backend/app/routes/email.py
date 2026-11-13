"""Email: inbox settings, the scan (scheduled and Scan now), saving emailed recipes, the result
email, and Share -> Email PDF."""
import asyncio
import re
import threading
import time
from collections import deque
from typing import Optional
from datetime import datetime, timedelta, timezone
import anyio
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session
from .. import models, schemas
from ..database import get_db, SessionLocal, UPLOADS_DIR, TMP_DIR
from ..ingestion.email_processing import process_tagged_email
from ..ingestion.url_ingest import redact_urls
from ..export import render_recipe_pdf
from .. import email_client
from .. import crypto
from .. import i18n
from .. import duplicates
from ..ml import service as learning
from ..file_validation import safe_join
from .common import _apply_tags, _attachment_disposition, _demote_to_tmp, _discard_temp_file, _iso_utc, _promote_temp_file, _restoring, _run_heavy, _utc_now
from fastapi import APIRouter
from ..logging_setup import get_logger

log = get_logger("app")
scan_log = get_logger("scan")
router = APIRouter()


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


def _pending_notification_count(settings: models.EmailIngestSettings) -> int:
    """Queued result lines. Every caller passes a settings row attached to
    a session; the 0 for a detached one only keeps the settings response
    from failing."""
    from sqlalchemy.orm import object_session
    s = object_session(settings)
    return s.query(models.EmailNotificationQueueItem).count() if s is not None else 0


def _record_scan_problem(db: Session, settings: models.EmailIngestSettings, text: str):
    # The only writer, so the 1000-character cap here is the cap on what
    # Settings displays.
    settings.last_problem = text[:1000]
    settings.last_problem_at = _utc_now()
    db.commit()


def _flush_notifications(db: Session, settings: models.EmailIngestSettings, force: bool = False) -> Optional[bool]:
    """Sends one batched notification email covering everything queued
    since the last one, if the cooldown has elapsed (or force=True).
    Returns True if an email was sent, False if there was nothing to send
    yet (empty queue, cooldown, not configured), and None if sending was
    attempted and failed -- the case the caller reports to the user.

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
        elapsed = _utc_now() - settings.last_notification_sent_at.replace(tzinfo=None)
        if elapsed < timedelta(minutes=settings.cooldown_minutes):
            return False  # still cooling down; items stay queued for the next flush

    if not (settings.smtp_host and settings.username and settings.password_encrypted and settings.notify_email):
        return False

    successes = [q for q in queued if q.success]
    failures = [q for q in queued if not q.success]
    # The [PARTIAL]/[FAILURE]/[SUCCESS] markers stay the same in both
    # languages, so a mail filter written for them keeps working.
    lang = settings.language or "en"
    fr = lang == "fr"
    if failures and successes:
        subject = (f"[PARTIAL] Open the Pantry : {len(successes)} reçue(s), {len(failures)} en échec" if fr else
                   f"[PARTIAL] Open the Pantry: {len(successes)} ingested, {len(failures)} failed")
    elif failures:
        subject = (f"[FAILURE] Open the Pantry : impossible de traiter {len(failures)} courriel(s)" if fr else
                   f"[FAILURE] Open the Pantry: unable to parse {len(failures)} email(s)")
    else:
        subject = (f"[SUCCESS] Open the Pantry : {len(successes)} recette(s) reçue(s)" if fr else
                   f"[SUCCESS] Open the Pantry: {len(successes)} recipe(s) ingested")

    body_lines = []
    if successes:
        body_lines.append(i18n.t("Ingested:", lang))
        body_lines.extend(f"  - {i18n.translate(q.message, lang)}" for q in successes)
        body_lines.append("")
    if failures:
        body_lines.append(i18n.t("Failed:", lang))
        body_lines.extend(f"  - {i18n.translate(q.message, lang)}" for q in failures)

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
                log.debug("_flush_notifications: caught error, continuing", exc_info=True)
                pass
    except Exception as e:
        scan_log.warning("Result email not sent; %d item(s) stay queued", len(queued), exc_info=True)
        _record_scan_problem(db, settings, f"The result email couldn't be sent: {e}")
        return None  # leave everything queued; next scan retries

    for q in queued:
        db.delete(q)
    settings.last_notification_sent_at = _utc_now()
    db.commit()
    return True


def _save_email_recipe(db: Session, result: dict, subject: str) -> int:
    """Persists a successfully-parsed emailed recipe. Mirrors the
    auto-save batch endpoints (no manual review stage) since there's no
    one at the keyboard during a 3am scan."""
    # result["image_path"] is a name process_tagged_email generated, never
    # one from the email itself.
    image_path = _promote_temp_file(result.get("image_path")) if result.get("image_path") else None
    # Held to RecipeCreate's limits, as a recipe saved through the API is
    # (DeepSeek review of 0114; ingredients and steps too since 0159).
    fields, ingredients, steps = schemas.fit_recipe(
        {k: result.get(k) for k in ("title", "source_url", "servings", "prep_time", "cook_time", "total_time",
                                     "raw_text")}, result["ingredients"], result["steps"])
    try:
        recipe = models.Recipe(**fields, source_type="email", ocr_confidence=result.get("ocr_confidence"),
                               image_path=image_path, needs_review=True)
        db.add(recipe)
        db.flush()
        for pos, ing in enumerate(ingredients):
            db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **ing))
        for pos, step in enumerate(steps):
            db.add(models.Step(recipe_id=recipe.id, position=pos, text=step))
        # A suggested tag that doesn't fit TagIn (a name over 100 characters
        # from a restored library) is left off, not the whole recipe
        # (DeepSeek review of 0132).
        tag_ins = []
        for t in result["tags"]:
            try:
                tag_ins.append(schemas.TagIn(**t))
            except ValueError:
                # Expected for such a tag: the name is enough, no traceback
                # (GPT review of 0134).
                log.warning("Email: suggested tag %r left off: not a valid tag", str(t.get("name"))[:60])
        _apply_tags(db, recipe, tag_ins)
        db.commit()
    except Exception:
        log.debug("_save_email_recipe: caught error, continuing", exc_info=True)
        db.rollback()
        _demote_to_tmp(image_path)
        raise
    return recipe.id


_email_scan_lock = threading.Lock()


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
    # One scan at a time. The daily scan and "Scan inbox now" (or two taps
    # on it) could otherwise both fetch the same UNSEEN message before
    # either marks it Seen, and save the recipe twice. Non-blocking: a
    # second caller is told a scan is running instead of queueing behind it.
    if _restoring.is_set():          # running or unfinished (GPT review of 0155)
        return {"scanned": 0, "succeeded": 0, "failed": 0,
                "messages": ["A backup is being restored. Try again in a minute."]}
    if not _email_scan_lock.acquire(blocking=False):
        return {"scanned": 0, "succeeded": 0, "failed": 0,
                "messages": ["A scan is already running. Try again in a moment."]}
    db = SessionLocal()
    try:
        return _run_email_scan_inner(db, force_notify)
    finally:
        db.close()
        _email_scan_lock.release()


def _parse_allowed_senders(raw: str) -> list[str]:
    """"example.com" was silently ignored: _sender_allowed only matches a
    full address or an explicit "@domain" entry, so an address at that
    domain (the entry's obvious intent) never matched and every sender
    was rejected. A bare domain (no "@") is normalized to "@domain" here,
    the one place both saving and matching read the list from."""
    out = []
    for p in re.split(r"[,;\s]+", raw or ""):
        p = p.strip().lower()
        if not p:
            continue
        out.append(p if "@" in p else f"@{p}")
    return out


def _sender_allowed(from_header: str, allowed: list[str]) -> bool:
    """Empty list = anyone. Otherwise the From address must match an entry
    exactly, or end with an "@domain" entry. The From header can be forged,
    so this keeps out strangers who learn the address and keyword -- not a
    determined attacker; a dedicated, unguessable address still matters."""
    if not allowed:
        return True
    import email.utils
    addr = email.utils.parseaddr(from_header or "")[1].lower()
    if not addr:
        return False
    return any(addr == a or (a.startswith("@") and addr.endswith(a)) for a in allowed)


def _run_email_scan_inner(db: Session, force_notify: bool) -> dict:
    settings = _get_email_settings(db)
    if not settings.enabled:
        return {"scanned": 0, "succeeded": 0, "failed": 0, "messages": ["Email ingest is disabled."]}
    if not (settings.imap_host and settings.username and settings.password_encrypted):
        return {"scanned": 0, "succeeded": 0, "failed": 0, "messages": ["Email ingest is not fully configured."]}

    messages = []
    succeeded = failed = 0
    saved = []          # what this scan added, for its results in Settings (with Delete)

    try:
        password = crypto.decrypt_secret(settings.password_encrypted)
    except Exception as e:
        log.warning("_run_email_scan_inner: password could not be decrypted", exc_info=True)
        _record_scan_problem(db, settings, f"The saved password couldn't be decrypted: {e}")
        return {"scanned": 0, "succeeded": 0, "failed": 0, "messages": [str(e)]}

    try:
        imap = email_client.connect_imap(
            settings.imap_host, settings.imap_port, settings.username, password, settings.imap_use_ssl
        )
    except Exception as e:
        scan_log.warning("Scan: IMAP connection failed: %s", e)
        _record_scan_problem(db, settings, f"Couldn't connect to the inbox: {e}")
        return {"scanned": 0, "succeeded": 0, "failed": 0, "messages": [f"Could not connect: {e}"]}

    try:
        msg_ids = email_client.search_unseen_by_subject(imap, settings.subject_keyword)
        scan_log.info("Scan: %d unread email(s) matching %r", len(msg_ids), settings.subject_keyword)
        for msg_id in msg_ids:
            subject = "(unknown subject)"
            try:
                allowed = _parse_allowed_senders(settings.allowed_senders)
                try:
                    # The size first: nothing of a message over the limit is
                    # fetched, its From header included (GPT review of 0160).
                    email_client.check_message_size(imap, msg_id)
                    sender = email_client.fetch_sender(imap, msg_id) if allowed else ""
                    if sender and not _sender_allowed(sender, allowed):
                        # Refused on the header alone, before the download.
                        scan_log.warning("Scan: ignored message %s from %s: sender not in the allowed list",
                                         msg_id.decode(errors="replace"), sender)
                        messages.append(f"IGNORED: an email from {sender}: sender isn't on the allowed list.")
                        try:
                            email_client.mark_seen(imap, msg_id)
                        except Exception:
                            scan_log.debug("mark_seen failed for ignored message", exc_info=True)
                        continue
                    msg = email_client.fetch_full_message(imap, msg_id, size_checked=True)
                except email_client.MessageTooLargeError as e:
                    # Too large or unreadable (MessageUnreadableError): won't
                    # change on a retry. Report and mark read.
                    failed += 1
                    line = f"(message {msg_id.decode(errors='replace')}): {e}"
                    messages.append(f"FAILED: {line}")
                    _queue_notification(db, False, line)
                    try:
                        email_client.mark_seen(imap, msg_id)
                    except Exception:
                        log.debug("_run_email_scan_inner: caught error, continuing", exc_info=True)
                        pass
                    continue
                subject = email_client.decode_subject(msg.get("Subject", "")) or subject
                if not _sender_allowed(msg.get("From", ""), _parse_allowed_senders(settings.allowed_senders)):
                    shown = str(msg.get("From", "?"))[:email_client.SENDER_SHOWN]
                    scan_log.warning("Scan: ignored %r from %s: sender not in the allowed list", subject, shown)
                    messages.append(f"IGNORED: \"{subject}\" from {shown}: sender isn't on the allowed list.")
                    try:
                        email_client.mark_seen(imap, msg_id)
                    except Exception:
                        scan_log.debug("mark_seen failed for ignored message", exc_info=True)
                    continue
                scan_log.info("Scan: processing %r from %s", subject, msg.get("From", "?"))
                result = _run_heavy(process_tagged_email, msg, TMP_DIR)
                existing = duplicates.same_link(db, result.get("source_url")) if result["success"] else None
                if existing:
                    # Already saved from this link: reported, not saved again.
                    _discard_temp_file(result.get("image_path"))
                    line = f"{existing['title']} (from \"{subject}\", via {result['source_detail']}) -- already in your library; not saved again"
                    scan_log.info("Scan: %s", redact_urls(line))
                    messages.append(f"OK: {line}")
                    _queue_notification(db, True, line)
                elif result["success"]:
                    same_title = duplicates.find(db, title=result["title"], limit=1)
                    recipe_id = _save_email_recipe(db, result, subject)
                    saved.append({"recipe_id": recipe_id, "title": result["title"]})
                    succeeded += 1
                    line = f"{result['title']} (from \"{subject}\", via {result['source_detail']})"
                    if not result["steps"]:
                        line += " -- no steps were found; check the recipe"
                    if same_title:
                        line += " -- a recipe with this title was already in your library"
                    scan_log.info("Scan: saved recipe %s: %s", recipe_id, redact_urls(line))
                    messages.append(f"OK: {line}")
                    _queue_notification(db, True, line)
                else:
                    failed += 1
                    line = f"\"{subject}\": {result['error']}"
                    messages.append(f"FAILED: {line}")
                    _queue_notification(db, False, line)
            except Exception as e:
                scan_log.exception("Scan: error handling message %r (%r); left unread", msg_id, subject)
                # Something went wrong AROUND the email rather than IN it:
                # fetching it, saving the recipe, the disk. That can be
                # temporary, so the email is left unread and the next scan
                # tries again. If it keeps failing, a [FAILURE] notice goes
                # out each time, which is noisy but visible.
                db.rollback()
                failed += 1
                line = f"\"{subject}\": {e} (left unread; the next scan will retry it)"
                messages.append(f"FAILED: {line}")
                _queue_notification(db, False, line)
                continue
            # Reached for success and for "no recipe in this email". Marked
            # read in both cases -- otherwise an email that can never be
            # parsed would be retried on every future scan.
            try:
                email_client.mark_seen(imap, msg_id)
            except Exception:
                log.debug("_run_email_scan_inner: caught error, continuing", exc_info=True)
                pass
    finally:
        try:
            imap.logout()
        except Exception:
            log.debug("_run_email_scan_inner: caught error, continuing", exc_info=True)
            pass

    settings.last_scan_at = _utc_now()
    db.commit()

    sent = _flush_notifications(db, settings, force=force_notify)
    if sent is None:
        messages.append(
            "The result email couldn't be sent (sending isn't working -- use Send test email "
            "to see why). It stays queued and goes out once sending works."
        )
    elif settings.last_problem and (sent or _pending_notification_count(settings) == 0):
        # The inbox was read, and nothing is stuck waiting to be emailed.
        # (sent=False with items still queued is a cooldown or missing
        # notify address: a send problem may not be fixed yet.)
        settings.last_problem = None
        settings.last_problem_at = None
        db.commit()

    return {"scanned": succeeded + failed, "succeeded": succeeded, "failed": failed, "messages": messages,
            "saved": saved}


def _scan_due(now, scan_hour: int, last_scan_at) -> bool:
    """At the scan hour; or later the same day when no scan has run today
    (local time): the hour skipped by a spring-forward change, or a
    container that was down then (review of 0158; backups have a
    catch-up too). Never scanned at all: only at the hour. A manual "Scan
    now" sets last_scan_at as well, so after one the catch-up waits for
    tomorrow; the scan at the hour itself still runs."""
    if now.hour == scan_hour:
        return True
    if now.hour < scan_hour or last_scan_at is None:
        return False
    when = last_scan_at if last_scan_at.tzinfo else last_scan_at.replace(tzinfo=timezone.utc)
    return when.astimezone().date() < now.date()


async def _daily_email_scan_loop():
    """Runs one scan per day at the configured hour. Checks every 15
    minutes rather than sleeping until the target time, so a changed
    scan hour (or a container that was asleep/suspended through the
    window) is picked up without needing a restart. A day-stamp guard
    ensures at most one scan per calendar day even though the check
    itself runs frequently."""
    last_scan_day = None
    retry_at = None
    while True:
        await asyncio.sleep(15 * 60)
        try:
            # Read the schedule config in a worker thread too -- no
            # Session is created on (or crosses) the event-loop thread.
            def _due_check():
                db = SessionLocal()
                try:
                    settings = _get_email_settings(db)
                    return bool(settings.enabled), settings.daily_scan_hour, settings.last_scan_at
                finally:
                    db.close()

            enabled, scan_hour, last_scan_at = await anyio.to_thread.run_sync(_due_check)
            if not enabled:
                continue
            now = datetime.now()
            today = now.date()
            if last_scan_day == today:
                continue
            if retry_at is not None:
                if now < retry_at:
                    continue
                if retry_at.date() < today:
                    retry_at = None          # a new day: the hour decides again
            if retry_at is None and not _scan_due(now, scan_hour, last_scan_at):
                continue
            await anyio.to_thread.run_sync(run_email_scan)
            # The day counts as done only once the scan reached the inbox
            # (last_scan_at moved): one that couldn't (the NAS rebooting, the
            # network down) is tried again in an hour, as automatic backups
            # are, not tomorrow (review of 0169).
            _, _, after = await anyio.to_thread.run_sync(_due_check)
            if after is not None and after != last_scan_at:
                last_scan_day, retry_at = today, None
                learning.schedule_retrain()
            else:
                retry_at = now + timedelta(hours=1)
                log.warning("Scheduled email scan didn't reach the inbox; trying again in an hour")
        except Exception:
            # A scheduled scan failing must never kill the loop -- the
            # next day's attempt should still happen. Logged in full, so it
            # shows in Settings -> Logs (it was at DEBUG; Claude review 13).
            # Not left to the 15-minute check either: _scan_due stays true
            # all day until a scan completes (review of 0176).
            retry_at = datetime.now() + timedelta(hours=1)   # `now` isn't set if the check itself failed
            log.exception("Scheduled email scan failed; trying again in an hour")


# ---------------------------------------------------------------------------
# Email ingest (optional feature)
# ---------------------------------------------------------------------------

def _can_send_email(settings: models.EmailIngestSettings) -> bool:
    return bool(settings.smtp_host and settings.username and settings.password_encrypted)


def _recent_recipients(settings: models.EmailIngestSettings) -> list[str]:
    import json
    try:
        value = json.loads(settings.recent_recipients or "[]")
    except ValueError:
        return []
    return [a for a in value if isinstance(a, str)] if isinstance(value, list) else []


def _email_settings_out(settings: models.EmailIngestSettings, password_cleared: bool = False) -> schemas.EmailSettingsOut:
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
        password_cleared=password_cleared,
        notify_email=settings.notify_email,
        subject_keyword=settings.subject_keyword,
        allowed_senders=settings.allowed_senders or "",
        daily_scan_hour=settings.daily_scan_hour,
        cooldown_minutes=settings.cooldown_minutes,
        last_scan_at=_iso_utc(settings.last_scan_at),
        last_problem=settings.last_problem,
        last_problem_at=_iso_utc(settings.last_problem_at),
        pending_notifications=_pending_notification_count(settings),
        encryption_configured=crypto.encryption_configured(),
        encryption_source=crypto.key_source(),
        server_timezone=datetime.now().astimezone().tzname() or "",
        can_send=_can_send_email(settings),
        recent_recipients=_recent_recipients(settings),
    )


@router.get("/api/email-settings", response_model=schemas.EmailSettingsOut)
def get_email_settings(db: Session = Depends(get_db)):
    return _email_settings_out(_get_email_settings(db))


@router.post("/api/email-settings/encryption-key", response_model=schemas.EmailSettingsOut)
def create_encryption_key(db: Session = Depends(get_db)):
    """Generate the credential-encryption key inside the data directory, so
    email ingest can be set up without editing docker-compose.yml. Refuses
    (409) if a key already exists in either form -- see
    crypto.generate_key_file for why replacing one is never done here."""
    try:
        crypto.generate_key_file()
    except (crypto.KeyAlreadyConfiguredError, FileExistsError):
        # FileExistsError: lost the O_EXCL race to a simultaneous request.
        # Same outcome as the pre-check, so same status.
        log.warning("create_encryption_key: request failed", exc_info=True)
        raise HTTPException(status_code=409, detail="An encryption key is already set up.")
    except OSError:
        log.warning("create_encryption_key: request failed", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Couldn't write the key file to the data folder. Check that the folder is writable.",
        )
    return _email_settings_out(_get_email_settings(db))


@router.put("/api/email-settings", response_model=schemas.EmailSettingsOut)
def update_email_settings(payload: schemas.EmailSettingsIn, db: Session = Depends(get_db)):
    settings = _get_email_settings(db)

    # A saved password belongs to one account on one server. If the server
    # or username changes without a new password in the same request, the
    # old one is dropped: otherwise anyone who can reach this API could
    # point the hosts at their own server, press "Send test email", and
    # receive the password in the login. (Opus 5 review, reproduced.)
    moved = (
        (settings.imap_host or None) != (payload.imap_host or None)
        or (settings.smtp_host or None) != (payload.smtp_host or None)
        or (settings.username or None) != (payload.username or None)
    )
    # Kept separate from the act of clearing below: if this request also
    # fails the "enabled needs a password" check further down, the error
    # needs to say a password was just cleared rather than the generic
    # "a password is required" -- that read as though the save had failed
    # and lost the stored password outright, when the stored one was
    # actually untouched (nothing is committed until the end of this
    # request).
    password_will_clear = bool(moved and settings.password_encrypted and not payload.password)
    if password_will_clear:
        settings.password_encrypted = None
        log.warning("Email server or username changed without a new password; saved password cleared")

    if payload.password:
        # Fails closed if no encryption key is configured -- a credential is
        # never written in plaintext as a fallback.
        try:
            settings.password_encrypted = crypto.encrypt_secret(payload.password)
        except crypto.EncryptionNotConfiguredError as e:
            raise HTTPException(status_code=400, detail=str(e))
    # An omitted/blank password leaves the stored one untouched.

    if payload.enabled and not (payload.imap_host and payload.username and settings.password_encrypted):
        if password_will_clear:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Changing the mail server or username clears the saved password. "
                    "Enter the password again to keep email ingest enabled."
                ),
            )
        raise HTTPException(
            status_code=400,
            detail="IMAP host, username, and a password are all required before enabling email ingest.",
        )

    settings.enabled = payload.enabled
    settings.imap_host = payload.imap_host
    settings.imap_port = payload.imap_port
    # Derived from the port here too, not only in the browser: the stored
    # flag is what every later connection reads, and a client that sends
    # the wrong one (an old cached app.js, a script) shouldn't be able to
    # store a combination that can only time out. See email_client.
    settings.imap_use_ssl = email_client.imap_uses_implicit_tls(payload.imap_port, payload.imap_use_ssl)
    settings.smtp_host = payload.smtp_host
    settings.smtp_port = payload.smtp_port
    settings.smtp_use_tls = not email_client.smtp_uses_implicit_tls(payload.smtp_port, payload.smtp_use_tls)
    settings.username = payload.username
    settings.notify_email = payload.notify_email
    settings.subject_keyword = payload.subject_keyword or "[RECIPE]"
    settings.allowed_senders = ", ".join(_parse_allowed_senders(payload.allowed_senders))
    settings.daily_scan_hour = payload.daily_scan_hour
    settings.cooldown_minutes = payload.cooldown_minutes
    settings.language = i18n.current_lang.get()

    db.commit()
    db.refresh(settings)
    return _email_settings_out(settings, password_cleared=password_will_clear)


@router.delete("/api/email-settings/password")
def clear_email_password(db: Session = Depends(get_db)):
    """Removes the stored credential (and disables the feature, since it
    can't run without one)."""
    settings = _get_email_settings(db)
    settings.password_encrypted = None
    settings.enabled = False
    db.commit()
    return {"ok": True}


@router.post("/api/email-settings/test", response_model=schemas.EmailTestResult)
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
        log.debug("test_email_settings: caught error, continuing", exc_info=True)
        return schemas.EmailTestResult(success=False, message=str(e))

    # Both halves are checked every time and reported separately. This used
    # to stop at the first failure, so a broken SMTP setup hid whether IMAP
    # worked at all -- and IMAP is the half that actually ingests recipes.
    smtp_ok, smtp_msg = True, f"Sending: OK -- a [TEST] email went to {settings.notify_email}."
    try:
        smtp = email_client.connect_smtp(
            settings.smtp_host, settings.smtp_port, settings.username, password, settings.smtp_use_tls
        )
        try:
            if i18n.current_lang.get() == "fr":
                subject, body = (
                    "[TEST] Réception par courriel d'Open the Pantry",
                    "Ceci est un message de test d'Open the Pantry.\n\n"
                    "Si vous le recevez, l'envoi des avis fonctionne. Open the Pantry "
                    f"vérifiera une fois par jour, à {settings.daily_scan_hour:02d} h 00, les courriels "
                    f"dont l'objet contient « {settings.subject_keyword} ».",
                )
            else:
                subject, body = (
                    "[TEST] Open the Pantry email ingest",
                    "This is a test message from Open the Pantry.\n\n"
                    "Receiving it means outgoing notifications are working. "
                    f"Open the Pantry will scan for emails whose subject contains "
                    f"\"{settings.subject_keyword}\" once daily at "
                    f"{settings.daily_scan_hour:02d}:00.",
                )
            email_client.send_email(smtp, settings.username, settings.notify_email, subject, body)
        finally:
            try:
                smtp.quit()
            except Exception:
                log.debug("test_email_settings: caught error, continuing", exc_info=True)
                pass
    except Exception as e:
        log.warning("test_email_settings: caught error, continuing", exc_info=True)
        smtp_ok, smtp_msg = False, f"Sending (SMTP) failed: {e}"

    imap_ok, imap_msg = True, "Reading: OK -- logged in to the inbox."
    try:
        imap = email_client.connect_imap(
            settings.imap_host, settings.imap_port, settings.username, password, settings.imap_use_ssl
        )
        try:
            imap.logout()
        except Exception:
            log.debug("test_email_settings: caught error, continuing", exc_info=True)
            pass
    except Exception as e:
        log.warning("test_email_settings: caught error, continuing", exc_info=True)
        imap_ok, imap_msg = False, f"Reading (IMAP) failed: {e}"

    if smtp_ok and imap_ok and settings.last_problem:
        # Both halves work now; an old scan problem would only mislead.
        settings.last_problem = None
        settings.last_problem_at = None
        db.commit()

    if smtp_ok and imap_ok:
        message = f"{smtp_msg} {imap_msg} Check that the test email arrived."
    elif imap_ok:
        message = (f"{smtp_msg}\n{imap_msg} Recipes can still be ingested; only the "
                   "result notifications can't be sent until sending works.")
    else:
        message = f"{smtp_msg}\n{imap_msg}"
    return schemas.EmailTestResult(success=smtp_ok and imap_ok, message=message)


@router.get("/api/email-status")
def email_status(db: Session = Depends(get_db)):
    """Open (no password): only whether the Inbox button and Share -> Email
    PDF should be offered. The settings themselves are behind the password."""
    settings = _get_email_settings(db)
    return {"enabled": bool(settings.enabled), "can_send": _can_send_email(settings)}


@router.post("/api/email-settings/scan", response_model=schemas.EmailScanResult)
def scan_inbox_now():
    """On-demand equivalent of the daily scheduled scan -- identical
    logic, just triggered manually. force_notify bypasses the cooldown
    since the user is watching and explicitly asked for this."""
    result = run_email_scan(force_notify=True)
    return schemas.EmailScanResult(**result)


# ---------------------------------------------------------------------------
# Share -> Email PDF
# ---------------------------------------------------------------------------
# Sent through the email-ingest account's SMTP login, From that account. Up
# to MAX_EMAIL_RECIPIENTS addresses per send and MAX_RECIPE_EMAILS_PER_HOUR
# sends per hour across the app: enough for sharing with family, and a cap
# on what anyone who can reach the app could send through your account.
MAX_EMAIL_RECIPIENTS = 5
MAX_RECIPE_EMAILS_PER_HOUR = 10
MAX_RECENT_RECIPIENTS = 20
_recipe_email_times: deque = deque()   # (monotonic time, token) per send in the last hour
_recipe_email_lock = threading.Lock()
# Deliberately plain: one @, no spaces, commas, angle brackets or control
# characters, and a dot in the domain. The mail server is the real judge.
_EMAIL_ADDRESS_RE = re.compile(r"^[^@\s,;<>\"()\[\]\\]+@[^@\s,;<>\"()\[\]\\]+\.[^@\s,;<>\"()\[\]\\]+$")


def _clean_addresses(raw: list[str]) -> list[str]:
    """Trimmed, de-duplicated (case-insensitively), in the order given.
    Raises 400 naming the first entry that isn't an address."""
    out, seen = [], set()
    for a in raw:
        a = (a or "").strip()
        if not a:
            continue
        if len(a) > 254 or not _EMAIL_ADDRESS_RE.match(a):
            shown = re.sub(r"[\x00-\x1f\x7f]+", " ", a)[:80]
            raise HTTPException(status_code=400, detail=f'"{shown}" isn\'t an email address.')
        if a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def _remember_recipients(settings: models.EmailIngestSettings, addresses: list[str]):
    import json
    lowered = {a.lower() for a in addresses}
    merged = addresses + [a for a in _recent_recipients(settings) if a.lower() not in lowered]
    settings.recent_recipients = json.dumps(merged[:MAX_RECENT_RECIPIENTS])


@router.put("/api/email-settings/recipients", response_model=schemas.RecentRecipients)
def update_recent_recipients(payload: schemas.RecentRecipients, db: Session = Depends(get_db)):
    """Settings -> Email ingest: edit the list of recent recipients."""
    import json
    settings = _get_email_settings(db)
    addresses = _clean_addresses(payload.recipients)[:MAX_RECENT_RECIPIENTS]
    settings.recent_recipients = json.dumps(addresses)
    db.commit()
    return {"recipients": addresses}


@router.post("/api/recipes/{recipe_id}/email")
def email_recipe_pdf(recipe_id: schemas.DbId, payload: schemas.RecipeEmailRequest, db: Session = Depends(get_db)):
    """The same PDF as Share -> Download PDF, sent as an attachment."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    settings = _get_email_settings(db)
    if not _can_send_email(settings):
        raise HTTPException(status_code=400, detail=(
            "Sending email isn't set up. Fill in the SMTP host, username and password under "
            "Settings → Email ingest."))
    to = _clean_addresses(payload.to)
    if not to:
        raise HTTPException(status_code=400, detail="Add at least one recipient.")
    if len(to) > MAX_EMAIL_RECIPIENTS:
        raise HTTPException(status_code=400, detail=f"At most {MAX_EMAIL_RECIPIENTS} recipients at a time.")

    now = time.monotonic()
    with _recipe_email_lock:
        while _recipe_email_times and now - _recipe_email_times[0][0] > 3600:
            _recipe_email_times.popleft()
        if len(_recipe_email_times) >= MAX_RECIPE_EMAILS_PER_HOUR:
            raise HTTPException(status_code=429, detail=(
                f"That's {len(_recipe_email_times)} recipe emails in the last hour; the limit is "
                f"{MAX_RECIPE_EMAILS_PER_HOUR}. Try again later."))
        # Counted up front so two sends at once can't both slip under the cap.
        # Each entry is unique (time, token), so _uncount() takes back this
        # send's entry even if another one has the same time.
        entry = (now, object())
        _recipe_email_times.append(entry)

    def _uncount():
        # Only sends that went out count against the hour: a run of
        # failures while the mail server was down shouldn't lock you out
        # once it's back.
        with _recipe_email_lock:
            try:
                _recipe_email_times.remove(entry)
            except ValueError:
                pass

    abs_image = safe_join(UPLOADS_DIR, recipe.image_path) if recipe.image_path else None
    try:
        pdf_bytes = render_recipe_pdf(recipe, abs_image, include_notes=payload.include_notes, include_image=False)
    except Exception:
        log.warning("email_recipe_pdf: PDF render failed for recipe %s", recipe_id, exc_info=True)
        _uncount()
        raise HTTPException(status_code=500, detail="Couldn't build the PDF.")

    title = re.sub(r"[\r\n\t]+", " ", recipe.title or "").strip() or "Recipe"
    fr = i18n.current_lang.get() == "fr"
    subject = f"Recette : {title}" if fr else f"Recipe: {title}"
    footer = "Envoyé depuis Open the Pantry." if fr else "Sent from Open the Pantry."
    message = payload.message.strip()
    body = f"{message}\n\n-- \n{footer}\n" if message else f"{footer}\n"
    filename = _attachment_disposition(recipe.title, "pdf").split("filename*=UTF-8''", 1)[1]
    from urllib.parse import unquote
    filename = unquote(filename)

    try:
        password = crypto.decrypt_secret(settings.password_encrypted)
        smtp = email_client.connect_smtp(
            settings.smtp_host, settings.smtp_port, settings.username, password, settings.smtp_use_tls
        )
        try:
            email_client.send_email_with_attachment(
                smtp, settings.username, to, subject, body, pdf_bytes, filename,
            )
        finally:
            try:
                smtp.quit()
            except Exception:
                log.debug("email_recipe_pdf: quit failed", exc_info=True)
    except Exception as e:
        log.warning("email_recipe_pdf: send failed for recipe %s", recipe_id, exc_info=True)
        _uncount()
        raise HTTPException(status_code=502, detail=f"The email wasn't sent: {e}")

    _remember_recipients(settings, to)
    db.commit()
    log.info("Recipe %s emailed to %d recipient(s)", recipe_id, len(to))
    return {"sent_to": to, "recent_recipients": _recent_recipients(settings)}
