import asyncio
import fcntl
import hmac
import json
import re
import os
import shutil
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Literal, Optional
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
import anyio
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query, Request, Response
from fastapi.responses import Response, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from sqlalchemy import text, case, asc, desc, func
from sqlalchemy.orm import Session, selectinload

from . import models, schemas
from .database import get_db, engine, SessionLocal, DATA_DIR, UPLOADS_DIR, TMP_DIR, DB_PATH
from .logging_setup import setup_logging, get_logger

setup_logging(DATA_DIR)
log = get_logger("app")
scan_log = get_logger("scan")
from .init_db import init_db
from .ingestion.completeness import incomplete_info, incomplete_message, missing_parts as missing_sections
from .ingestion.url_ingest import (ingest_url, safe_get, UrlValidationError, IncompleteRecipeError,
                                   require_complete, missing_parts)
from .ingestion.pdf_ingest import extract_pdf_text, segment_raw_text, PdfTooLargeError, extract_largest_embedded_image
from .ingestion.image_ingest import ingest_and_segment, ingest_image
from .ingestion.ingredient_parser import parse_ingredient_block
from .ingestion.tagger import suggest_tags, explain_tags, set_user_keywords, meat_tag_names
from .ingestion.email_processing import process_tagged_email
from .export import render_recipe_html, render_recipe_pdf
from .backup import build_database_backup, build_pdf_bundle, remove_upload_file, UPLOADS_LOCK
from .time_utils import ddhhmm_to_minutes, available_time_buckets
from . import email_client
from . import review
from . import scheduled_backup
from . import crypto
from . import i18n
from . import https_setup
from . import admin_lock
from . import picker
from . import search
from . import duplicates
from . import interchange
from . import restore as restore_mod
from .ml import service as learning

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
        log.debug("_acquire_single_instance_lock: caught error, continuing", exc_info=True)
        raise RuntimeError(
            "Another instance of this app appears to already be running against "
            f"this data directory ({DATA_DIR}). SQLite doesn't support concurrent "
            "writers safely -- refusing to start a second instance against the "
            "same data."
        )


_acquire_single_instance_lock()
init_db()


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


refresh_user_keywords()

TMP_FILE_MAX_AGE_SECONDS = 24 * 60 * 60
TMP_SWEEP_INTERVAL_SECONDS = 6 * 60 * 60  # re-sweep periodically, not just at startup

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


from .file_validation import (
    MAX_UPLOAD_BYTES, PDF_MAGIC, IMAGE_MAGICS,
    reencode_image, validate_and_save_image_bytes, validate_and_save_pdf_bytes, detect_image_ext,
    is_safe_stored_filename, safe_join,
)


# Backup archives are staged here, NOT in TMP_DIR. TMP_DIR is mounted at
# /tmp-preview so the review screen can show a draft's captured image, which
# would put a complete copy of the database under a publicly reachable path.
# The filenames are unguessable either way, but there is no reason to serve
# them at all.
BACKUP_TMP_DIR = os.path.join(DATA_DIR, "backup-tmp")
os.makedirs(BACKUP_TMP_DIR, exist_ok=True)


def _sweep_stale_tmp_files():
    """Safety net for draft files left behind when the add-recipe modal is
    closed without saving and the frontend's explicit discard call didn't
    fire (e.g. tab closed mid-flow), or a browser/tab crash. Runs at startup
    and periodically thereafter (see lifespan below) -- a container can run
    for weeks, so a startup-only sweep isn't enough on its own.

    Also sweeps abandoned backup archives: the streaming response deletes its
    own file, but a download that is cancelled or dies mid-flight can leave
    one behind, and those are large.
    """
    now = time.time()
    for directory in (TMP_DIR, BACKUP_TMP_DIR):
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > TMP_FILE_MAX_AGE_SECONDS:
                    os.remove(path)
            except OSError:
                log.debug("_sweep_stale_tmp_files: caught error, continuing", exc_info=True)
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
        # Recently deleted recipes keep their photos until deleted for good.
        referenced = {
            r[0] for r in db.query(models.Recipe.image_path).execution_options(include_deleted=True)
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
                remove_upload_file(path)
        except OSError:
            log.debug("_sweep_orphaned_upload_files: caught error, continuing", exc_info=True)
            pass


async def _run_sweeps():
    """Both sweeps, in a worker thread. They're blocking work (a database
    query, folder listings, deletes), and a delete in uploads/ waits on
    backup.UPLOADS_LOCK, which a full backup holds for its whole archive
    build: run on the event loop, a sweep that met a backup froze every
    request on both ports until the backup ended (DeepSeek review of
    0083)."""
    await asyncio.to_thread(_sweep_stale_tmp_files)
    await asyncio.to_thread(purge_recently_deleted)
    await asyncio.to_thread(_sweep_orphaned_upload_files)


async def _periodic_tmp_sweep():
    while True:
        await asyncio.sleep(TMP_SWEEP_INTERVAL_SECONDS)
        try:
            await _run_sweeps()
        except Exception:
            # One failed sweep mustn't end the loop (it used to, silently).
            log.exception("Periodic tmp/uploads sweep failed")


def _warn_unprotected_credentials():
    """An email password or HTTPS set up while no settings password exists
    (an upgrade from before 0052, or a deleted password file): those
    sections are open to the network until someone creates one."""
    if admin_lock.password_set():
        return False
    db = SessionLocal()
    try:
        s = db.query(models.EmailIngestSettings).first()
        email = bool(s and s.password_encrypted)
    finally:
        db.close()
    https_on = bool(https_setup.status().get("enabled"))
    if email or https_on:
        log.warning("No settings password yet, but %s set up: open Settings and create one now -- until then "
                    "anyone on the network can open those sections.",
                    " and ".join(n for n, on in (("the email password is", email), ("HTTPS is", https_on)) if on))
        return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Sync ingestion endpoints (OCR/PDF work) and lightweight CRUD endpoints
    # share FastAPI's default worker thread pool. The default cap (40) is
    # plenty for a personal app under normal use, but bumping it gives real
    # headroom so a burst of heavy ingestion requests can't starve simple
    # requests (list/search/get) of a thread. MAX_CONCURRENT_HEAVY_JOBS
    # above is the actual CPU-thrashing guard; this is just pool headroom.
    anyio.to_thread.current_default_thread_limiter().total_tokens = 100

    await _run_sweeps()
    sweep_task = asyncio.create_task(_periodic_tmp_sweep())
    email_scan_task = asyncio.create_task(_daily_email_scan_loop())
    backup_task = asyncio.create_task(_scheduled_backup_loop())
    # Optional HTTPS (Settings -> HTTPS): serve it if it was set up, and
    # renew the certificate in time.
    try:
        await https_setup.startup(app)
    except Exception:
        log.exception("HTTPS listener couldn't start")
    https_renew_task = asyncio.create_task(https_setup.renewal_loop(app))
    # Settings -> What to eat?: the read-only picker page on its own port,
    # if it was switched on. A busy port is logged and shown in Settings;
    # it never stops the app.
    try:
        await picker.startup()
    except Exception:
        log.exception("What to eat? listener couldn't start")
    # Settings -> Learning: use the saved models, then retrain from the
    # recipes as they are now (in the background).
    try:
        learning.load()
    except Exception:
        log.exception("Learning: couldn't load the saved models")
    learning.schedule_retrain(delay=learning.STARTUP_DELAY)
    try:
        _warn_unprotected_credentials()
    except Exception:
        log.exception("Couldn't check whether the settings password is set")
    yield
    sweep_task.cancel()
    email_scan_task.cancel()
    backup_task.cancel()
    https_renew_task.cancel()
    await https_setup.stop_server()
    await picker.stop_server()
    engine.dispose()


app = FastAPI(title="Recipe App", lifespan=lifespan)

# Request bodies that aren't file uploads (JSON: recipes, settings, batch
# lists) are read whole into memory before validation, and uvicorn sets no
# limit. 32 MB is far above any real recipe (DeepSeek review of 0114).
MAX_JSON_BODY_BYTES = 32 * 1024 * 1024
# Uploads (multipart): Starlette parses the whole body, spooling files to
# the temporary folder, before the route sees it, so the routes' own limits
# came too late to stop a larger upload filling the disk (GPT review of
# 0119). Each upload route's limit is applied here as the body arrives;
# the routes keep theirs per file. The margin is for the form's own
# headers and boundaries.
MULTIPART_MARGIN_BYTES = 1024 * 1024


def _multipart_limit(scope):
    """(limit in bytes, response to send past it, or None for the 413)."""
    path = scope.get("path", "")
    if path == "/api/backup/restore/check":
        # A full backup can be up to 16 GB unpacked, but only with the
        # settings password; without it the box only imports (1 GB), and
        # past that the page is told to ask for the password and resend.
        refusal = _settings_lock_refusal(Request(scope))
        if refusal is None:
            return restore_mod.MAX_TOTAL_BYTES + MULTIPART_MARGIN_BYTES, None
        return MAX_IMPORT_BYTES + MULTIPART_MARGIN_BYTES, refusal
    limit = {
        "/api/ingest/pdf": MAX_UPLOAD_BYTES,
        "/api/ingest/image": MAX_UPLOAD_BYTES,
        "/api/upload-image": MAX_UPLOAD_BYTES,
        "/api/ingest/images": MAX_COMBINED_IMAGES * MAX_UPLOAD_BYTES,
        "/api/ingest/pdf/batch": MAX_BATCH_PDF_BYTES,
        "/api/import/recipes": MAX_IMPORT_BYTES,
    }.get(path, MAX_JSON_BODY_BYTES)
    return limit + MULTIPART_MARGIN_BYTES, None


class BodySizeLimit:
    """ASGI middleware: refuses a body over its limit, by its Content-Length
    or, without one, as it arrives: 32 MB for JSON and other bodies, the
    route's own limit for an upload."""

    def __init__(self, app, limit: int = MAX_JSON_BODY_BYTES):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        limit, refusal = self.limit, None
        if headers.get(b"content-type", b"").lower().startswith(b"multipart/form-data"):
            limit, refusal = _multipart_limit(scope)
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            return await self._refuse(scope, receive, send, refusal, limit)
        seen, over, answered = 0, False, False

        # Past the limit, reading stops (the app sees the client go away)
        # and whatever the app answers to that is replaced by the refusal.
        # An exception raised here would be turned into FastAPI's own 400.
        async def counted():
            nonlocal seen, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    over = True
                    return {"type": "http.disconnect"}
            return message

        async def tracked(message):
            nonlocal answered
            if not over:
                await send(message)
            elif not answered:
                answered = True
                await self._refuse(scope, receive, send, refusal, limit)

        await self.app(scope, counted, tracked)
        if over and not answered:
            await self._refuse(scope, receive, send, refusal, limit)

    async def _refuse(self, scope, receive, send, refusal, limit):
        if refusal is not None:
            return await refusal(scope, receive, send)
        if limit == self.limit:
            detail = "Request too large."
        else:
            detail = f"File too large (max {(limit - MULTIPART_MARGIN_BYTES) // (1024 * 1024)} MB)."
        body = json.dumps({"detail": detail}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


app.add_middleware(BodySizeLimit)


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
if API_KEY:
    log.warning("RECIPE_APP_API_KEY is set: every request needs an X-API-Key header, and the app's own "
                "pages don't send one. The web app in a browser won't work unless a reverse proxy adds it.")


# Settings -> Backup & export -> Restore: while a backup is put back, every
# other request on either port gets a 503 (the database and photos are
# being replaced under them), and the restore waits for requests already
# running to finish before it starts. (restore_backup)
_restoring = threading.Event()
_inflight = 0
_inflight_lock = threading.Lock()


@app.middleware("http")
async def restore_guard(request: Request, call_next):
    global _inflight
    if _restoring.is_set() and request.url.path != "/healthz":
        return JSONResponse(status_code=503, content={"detail": "A backup is being restored. Try again in a minute."},
                            headers={"Retry-After": "30"})
    with _inflight_lock:
        _inflight += 1
    try:
        return await call_next(request)
    finally:
        with _inflight_lock:
            _inflight -= 1


# Changes the learned models train on: recipes saved, edited or deleted,
# tags changed, batch and email imports.
_RETRAIN_AFTER = re.compile(r"^/api/(recipes|tags|tag-groups|ingest/(url|pdf)/batch|email-settings/scan|import|recently-deleted)(/|$)")


@app.middleware("http")
async def retrain_after_changes(request: Request, call_next):
    response = await call_next(request)
    if (request.method in ("POST", "PUT", "PATCH", "DELETE") and 200 <= response.status_code < 300
            and _RETRAIN_AFTER.match(request.url.path)
            and not request.url.path.endswith(("/rating", "/notes", "/email", "/suggest"))
            and request.query_params.get("dry_run", "").lower() not in ("1", "true")):
        learning.schedule_retrain()
        # Search's cached word list: a word just added mustn't be taken for
        # a typo and "corrected" into another one.
        search.forget_vocabulary()
    return response


@app.middleware("http")
async def log_unhandled_errors(request: Request, call_next):
    """Anything that escapes a handler as a 500 gets its traceback logged.

    Where it sits (DeepSeek review of 0096: this said "registered first,
    so innermost", which stopped being true when restore_guard and
    retrain_after_changes were added above it). In Starlette the last
    middleware registered is the outermost, so this one wraps the route
    handlers plus the two registered before it, and every check registered
    after it (settings password, cross-site, API key, rate limit, headers,
    language) runs outside it. Those don't raise by design; if one ever
    did, uvicorn still logs the traceback ("Exception in ASGI application").
    On the picker port it's the first in the list, so innermost there."""
    try:
        return await call_next(request)
    except Exception:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        raise


# --- Browser-borne requests from other sites -------------------------------
# "Only people on my network can reach it" doesn't cover web pages those
# people visit: a page elsewhere can send requests to the NAS through their
# browser. Two checks close the common routes (Opus 5 review):
#
# 1. Host header. DNS rebinding points an attacker's own domain at the NAS's
#    IP, so the browser treats the app as that domain and lets the page read
#    responses. Such requests arrive with the attacker's hostname in Host.
#    Accepted by default: IP addresses, localhost, and local-style names
#    (no dot, or ending .local / .lan / .home.arpa / .internal). Anything
#    else -- e.g. a reverse-proxy name like pantry.example.com -- must be
#    listed in RECIPE_APP_ALLOWED_HOSTS (comma-separated).
# 2. A custom header on every state-changing API request. A cross-site page
#    can POST a form or an empty body without the browser asking first; it
#    can't add a custom header without a CORS preflight, which this app never
#    approves. The app's own pages add it automatically (see app.js).
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("RECIPE_APP_ALLOWED_HOSTS", "").split(",") if h.strip()}
_LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localhost")
WRITE_HEADER = "x-requested-with"


def _host_allowed(host_header: str) -> bool:
    import ipaddress as _ip
    host = (host_header or "").strip().lower()
    if host.startswith("["):                      # [::1]:8090
        host = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:                    # name:port / 1.2.3.4:port
        host = host.rsplit(":", 1)[0]
    if not host:
        return False
    if "*" in ALLOWED_HOSTS or host in ALLOWED_HOSTS:
        return True
    # The name set up in Settings -> HTTPS.
    if host == https_setup.active_domain():
        return True
    try:
        _ip.ip_address(host)
        return True
    except ValueError:
        pass
    return "." not in host or host.endswith(_LOCAL_SUFFIXES)


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


@app.exception_handler(_HttpsOnly)
async def _https_only_handler(request: Request, exc: _HttpsOnly):
    return _https_only_response(exc.url)


@app.middleware("http")
async def settings_password_gate(request: Request, call_next):
    """Email, HTTPS, Logs and Email PDF need the settings password (see
    admin_lock.py for the list and the reasons). Everything else is open.

    Defined above cross_site_guard and api_key_auth in this file, so
    Starlette runs it after them (the last middleware declared is the
    outermost): an unknown host or a write without X-Requested-With is
    refused before this one says anything about the lock."""
    if not admin_lock.is_locked_path(request.url.path):
        return await call_next(request)
    refused = _settings_lock_refusal(request)
    return refused if refused is not None else await call_next(request)


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
_rate_limit_last_prune = 0.0  # when idle IPs were last dropped from _request_log


@app.middleware("http")
async def cross_site_guard(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    if not _host_allowed(request.headers.get("host", "")):
        log.warning("Refused request for host %r from %s (not in RECIPE_APP_ALLOWED_HOSTS)",
                    request.headers.get("host"), request.client.host if request.client else "?")
        return JSONResponse(status_code=400, content={
            "detail": "This host name isn't allowed. Add it to RECIPE_APP_ALLOWED_HOSTS in docker-compose.yml."})
    if (request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/")
            and not request.headers.get(WRITE_HEADER)):
        log.warning("Refused %s %s without the %s header", request.method, request.url.path, WRITE_HEADER)
        return JSONResponse(status_code=403, content={
            "detail": "Missing X-Requested-With header. Requests that change data must send it."})
    return await call_next(request)


@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    if not API_KEY or request.url.path == "/healthz":
        return await call_next(request)
    # Constant-time comparison: != stops at the first differing byte, which
    # in principle leaks how much of a guess was right.
    if not hmac.compare_digest(request.headers.get("X-API-Key", "").encode(), API_KEY.encode()):
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid X-API-Key header."})
    return await call_next(request)


@app.middleware("http")
async def rate_limiter(request: Request, call_next):
    """Per-IP request budget for the API only.

    It used to count every request, including thumbnails from /uploads and
    the app's own static files. Scrolling a library of ~100 photo recipes
    used up the budget, after which /api/recipes answered 429 and the list
    read "No recipes match". Photos and static files cost nothing to serve
    and aren't what the limit is for.

    Addresses idle for a full window are forgotten, so the table doesn't
    grow with every address that has ever connected.
    """
    path = request.url.path
    if not path.startswith("/api/") or path == "/api/client-error":
        return await call_next(request)
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    with _rate_limit_lock:
        log_ = _request_log[client_ip]
        while log_ and now - log_[0] > RATE_LIMIT_WINDOW_SECONDS:
            log_.popleft()
        if len(log_) >= RATE_LIMIT_MAX_REQUESTS:
            if len(log_) == RATE_LIMIT_MAX_REQUESTS:
                log.warning("Rate limit reached for %s (%d requests/min)", client_ip, RATE_LIMIT_MAX_REQUESTS)
                log_.append(now)  # marks "already logged" for this window
            return JSONResponse(status_code=429, content={"detail": "Too many requests, slow down."})
        log_.append(now)
        global _rate_limit_last_prune
        if now - _rate_limit_last_prune > RATE_LIMIT_WINDOW_SECONDS:
            for ip in [ip for ip, q in _request_log.items() if not q or now - q[-1] > RATE_LIMIT_WINDOW_SECONDS]:
                del _request_log[ip]
            _rate_limit_last_prune = now
    return await call_next(request)


_client_log = get_logger("client")
_client_error_times: list = []
_client_error_lock = threading.Lock()


@app.post("/api/client-error", status_code=204)
def report_client_error(report: schemas.ClientErrorReport):
    """Uncaught JavaScript errors and rejected promises from the app, written
    to the server log so they can be read alongside everything else. At most
    20 a minute are kept, so a page stuck in an error loop can't fill the
    disk."""
    now = time.monotonic()
    with _client_error_lock:  # handlers run on several threads
        _client_error_times[:] = [t for t in _client_error_times if now - t < 60]
        if len(_client_error_times) >= 20:
            return None
        _client_error_times.append(now)

    def one_line(v):
        # The browser supplies these; a newline in them could fake log lines.
        return str(v).replace("\r", " ").replace("\n", " ") if v else "?"

    stack = "\n".join("    | " + line for line in report.stack.splitlines()) if report.stack else ""
    _client_log.warning(
        "Browser error on %s: %s (at %s:%s:%s) [%s]%s",
        one_line(report.page), one_line(report.message), one_line(report.source), report.line, report.column,
        one_line(report.user_agent), ("\n" + stack) if stack else "",
    )
    return None


# Browser features the app never uses, switched off for its pages. Keeping
# the screen awake is the one it does use (Settings -> Keep screen awake).
# Only features Chromium recognises: an unknown name ("bluetooth") is
# logged as an error in the browser console on every page load.
PERMISSIONS_POLICY = ", ".join([
    "camera=()", "microphone=()", "geolocation=()", "payment=()", "usb=()", "serial=()", "hid=()",
    "midi=()", "accelerometer=()", "gyroscope=()", "magnetometer=()",
    "display-capture=()", "browsing-topics=()", "screen-wake-lock=(self)",
])


def _security_headers(request: Request, response):
    """On every response from either port:
    - Content-Security-Policy: only this server's own scripts, styles,
      images and connections, so a script that ever got into a page (a
      crafted recipe, say) wouldn't run or reach anywhere else. Inline
      style attributes are allowed (the app sets some), inline scripts are
      not (it has none). Images may also be data: and blob: (the HTML
      export, photo previews). The main page may also reach the What to
      eat? port: Settings checks whether it answers.
    - frame-ancestors 'none' and X-Frame-Options: DENY: never shown inside
      another site's page, where a hidden frame could start an import
      (/?share_url=) or trick taps (Claude review round 9).
    - Referrer-Policy: no-referrer: "View original" doesn't tell the
      recipe's site this server's address.
    - Permissions-Policy: features the app never uses are off.
    Not sent: Strict-Transport-Security. It covers every port of a host
    name, and the app deliberately keeps plain http:// working beside its
    HTTPS address (8090, and What to eat? on 8091, which has no HTTPS)."""
    connect = "'self'"
    if request.scope.get("app") is not picker_app:
        try:
            parts = urlsplit(picker.picker_url(request))
            connect += f" {parts.scheme}://{parts.netloc}"
        except Exception:
            log.debug("CSP: no picker address for connect-src", exc_info=True)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob:; font-src 'self'; "
        f"connect-src {connect}; manifest-src 'self'; worker-src 'self'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = PERMISSIONS_POLICY


# The app's own files (index.html, app.js, styles.css, the service worker)
# are sent with Cache-Control: no-cache: the browser keeps them but asks the
# server each time (a 304 when nothing changed). Without it iOS kept old
# copies for hours after an update. Photos (unique file names) and API
# responses are left alone.
# Every response, on both ports (picker.create_app gets this middleware
# too), also carries the security headers below (_security_headers).
@app.middleware("http")
async def app_files_revalidate(request: Request, call_next):
    response = await call_next(request)
    _security_headers(request, response)
    path = request.url.path
    if request.method == "GET" and not path.startswith(("/api/", "/uploads/", "/tmp-preview/")) and path != "/healthz":
        response.headers["Cache-Control"] = "no-cache"
    return response


# Interface language (see i18n.py). Registered last, so it is the outermost
# middleware: the language is known to everything inside, and messages the
# other middlewares return (a 429, a refused write) are translated too.
# Only JSON bodies of a modest size are rewritten; recipe lists and files
# pass straight through. A response is translated only when it states its
# length (Content-Length) and that is under this: a streamed response, or
# one without a length, passes through in English even if it's small JSON.
# Every JSON endpoint today returns a normal JSONResponse, which has one; a
# new endpoint that streams messages for the page must set it to be
# translated (DeepSeek review of 0096).
_TRANSLATE_MAX_BYTES = 256 * 1024


@app.middleware("http")
async def interface_language(request: Request, call_next):
    lang = i18n.normalize(request.headers.get("x-app-lang") or request.query_params.get("lang"))
    token = i18n.current_lang.set(lang)
    try:
        response = await call_next(request)
    finally:
        i18n.current_lang.reset(token)
    if lang == "en" or not response.headers.get("content-type", "").startswith("application/json"):
        return response
    length = response.headers.get("content-length")
    if not length or int(length) > _TRANSLATE_MAX_BYTES:
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        import json as _json
        payload = i18n.translate_payload(_json.loads(body), lang)
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except ValueError:
        log.debug("interface_language: response wasn't JSON after all", exc_info=True)
    # Raw header pairs, not a dict: a dict keeps only one of several
    # Set-Cookie headers (DeepSeek review of 0073).
    out = Response(content=body, status_code=response.status_code, background=response.background)
    out.raw_headers = [(k, v) for k, v in response.raw_headers if k.lower() != b"content-length"] + \
        [(b"content-length", str(len(body)).encode())]
    return out


def _frontend_version() -> str:
    """A fingerprint of the app's own files, so the page can tell whether
    the server has a newer version than the one it's running (pull to
    refresh)."""
    import hashlib
    h = hashlib.sha256()
    static = os.path.join(os.path.dirname(__file__), "..", "static")
    for root, _, files in sorted(os.walk(static)):
        for f in sorted(files):
            if f.endswith((".js", ".css", ".html", ".json")):
                try:
                    with open(os.path.join(root, f), "rb") as fh:
                        h.update(f.encode()); h.update(fh.read())
                except OSError as e:
                    # The page can't load a file the server can't read either,
                    # so this is worth seeing in the log, not only at DEBUG.
                    # The name and the reason are enough to fix it; no
                    # traceback per file (DeepSeek review of 0096).
                    log.warning("_frontend_version: unreadable %s: %s", os.path.join(root, f), e.strerror or e)
    return h.hexdigest()[:16]


FRONTEND_VERSION = _frontend_version()


@app.get("/api/version")
def app_version():
    return {"frontend": FRONTEND_VERSION}


@app.get("/healthz")
def healthz():
    """Liveness check with no auth requirement and no DB dependency --
    used by the Dockerfile HEALTHCHECK. Deliberately separate from any
    endpoint that returns real data."""
    return {"status": "ok"}


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
    # (DeepSeek review of 0114): a page's heading or a link can be longer.
    cap = lambda v, n: (v[:n] if isinstance(v, str) else v)       # noqa: E731
    url = result.get("source_url")
    try:
        recipe = models.Recipe(
            title=cap(result["title"], 500),
            source_type="email",
            source_url=url if isinstance(url, str) and len(url) <= 2000 else None,
            servings=cap(result.get("servings"), schemas.SERVINGS_MAX),
            prep_time=cap(result.get("prep_time"), 200),
            cook_time=cap(result.get("cook_time"), 200),
            total_time=cap(result.get("total_time"), 200),
            raw_text=cap(result.get("raw_text"), 1_000_000),
            ocr_confidence=result.get("ocr_confidence"),
            image_path=image_path,
            needs_review=True,
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
                try:
                    msg = email_client.fetch_full_message(imap, msg_id)
                except email_client.MessageTooLargeError as e:
                    # Won't get smaller on a retry: report and mark read.
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
                    scan_log.warning("Scan: ignored %r from %s: sender not in the allowed list",
                                     subject, msg.get("From", "?"))
                    messages.append(f"IGNORED: \"{subject}\" from {msg.get('From', '?')}: sender isn't on the allowed list.")
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
                    scan_log.info("Scan: %s", line)
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
                    scan_log.info("Scan: saved recipe %s: %s", recipe_id, line)
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
            learning.schedule_retrain()
        except Exception:
            # A scheduled scan failing must never kill the loop -- the
            # next day's attempt should still happen. Logged in full, so it
            # shows in Settings -> Logs (it was at DEBUG; Claude review 13).
            log.exception("Scheduled email scan failed; trying again tomorrow")


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
        log.warning("Add from URL failed for %s: %s", payload.url, e,
                    exc_info=not isinstance(e, UrlValidationError))
        raise HTTPException(status_code=422, detail=str(e))

    # A half recipe (no ingredients, or no steps) is reported as a failed
    # import, with the partial draft attached so "Continue anyway" can still
    # open it in the review screen.
    incomplete = None
    try:
        require_complete(result)
    except IncompleteRecipeError as e:
        log.info("Add from URL: incomplete recipe from %s: %s", payload.url, e)
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


@app.post("/api/ingest/url/batch")
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
                needs_review=True,
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
            succeeded.append({"url": url, "recipe_id": recipe.id, "title": result.title,
                              "duplicate_of": same_title[0] if same_title else None})
        except Exception as e:
            log.warning("Batch URL %s failed: %s", url, e, exc_info=not isinstance(e, UrlValidationError))
            db.rollback()
            if image_path:
                stray_path = safe_join(UPLOADS_DIR, image_path)
                if stray_path and os.path.isfile(stray_path):
                    remove_upload_file(stray_path)
            failed.append({"url": url, "error": str(e)})
    return {"succeeded": succeeded, "failed": failed, "skipped": skipped}


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


@app.post("/api/ingest/pdf/batch")
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
            recipe = models.Recipe(
                title=segmented["title_guess"],
                source_type="pdf",
                servings=segmented.get("servings"),
                raw_text=pdf_result.raw_text,
                ocr_confidence=pdf_result.avg_ocr_confidence,
                needs_review=True,
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


@app.post("/api/ingest/image")
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
    # Only names this app gives drafts, as everywhere else a name meets the
    # disk (DeepSeek review of 0073).
    if not is_safe_stored_filename(safe_name):
        return {"ok": False}
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
            language=payload.language,
            actual_cook_time_minutes=_logged_minutes(payload.actual_cook_time),
        )
        db.add(recipe)
        db.flush()

        for pos, ing in enumerate(payload.ingredients):
            db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **_ingredient_fields(ing)))

        for pos, step_text in enumerate(payload.steps):
            db.add(models.Step(recipe_id=recipe.id, position=pos, text=step_text))

        _apply_tags(db, recipe, payload.tags)

        db.commit()
    except Exception:
        log.debug("create_recipe: caught error, continuing", exc_info=True)
        db.rollback()
        _demote_to_tmp(promoted_image)
        raise

    db.refresh(recipe)
    return recipe


@app.get("/api/recipes", response_model=list[schemas.RecipeSummaryOut])
def list_recipes(
    response: Response,
    q: str | None = Query(default=None, max_length=500),
    tags: list[str] | None = Query(default=None),        # repeated ?tags=A&tags=B, AND logic
    max_minutes: Optional[schemas.DbInt] = None,   # actual_cook_time_minutes <= max_minutes
    # Pace rating: repeated ?pace=quick&pace=moderate, OR logic (a recipe has
    # one pace, so AND across values could never match).
    pace: list[Literal["quick", "moderate", "long"]] | None = Query(default=None),
    favorite: bool | None = None,
    to_review: bool | None = None,
    sort: Literal["created", "title", "rating", "cook_time", "difficulty", "relevance"] = "created",
    direction: Literal["asc", "desc"] = "desc",
    db: Session = Depends(get_db),
):
    matched_via: dict[int, set[str]] = {}
    found = None

    if q and q.strip():
        # search.py: words to find (typos corrected when nothing matches),
        # "-word" to leave out, best-match ranks and excerpts. Every word
        # reaches FTS5 quoted; column sets are fixed literals there.
        found = search.run(db, q)
        query = db.query(models.Recipe)
        if found["matched"] is not None:
            matched_via = found["matched"]
            if not matched_via:
                if found["corrected"]:
                    response.headers["X-Search-Corrected"] = json.dumps(found["corrected"])
                return []
            query = query.filter(models.Recipe.id.in_(matched_via.keys()))
        if found["excluded"]:
            query = query.filter(models.Recipe.id.notin_(found["excluded"]))
        if found["corrected"]:
            # ASCII JSON, so it fits a header; the page shows "Showing results for".
            response.headers["X-Search-Corrected"] = json.dumps(found["corrected"])
    else:
        query = db.query(models.Recipe)

    if favorite:
        query = query.filter(models.Recipe.favorite.is_(True))

    if to_review:
        query = query.filter(models.Recipe.needs_review.is_(True))

    if pace:
        query = query.filter(models.Recipe.cook_time_rating.in_(pace))

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

    # selectinload: the summaries below read each recipe's tags, which
    # otherwise costs one extra query per recipe in the list.
    # "relevance" is best match first, while searching for words; without
    # any it's the default order.
    by_rank = sort == "relevance" and found is not None and found["matched"] is not None
    results = query.options(selectinload(models.Recipe.tags)).order_by(
        *_sort_clauses("created" if sort == "relevance" else sort, "desc" if sort == "relevance" else direction)).all()
    if by_rank:
        results.sort(key=lambda r: found["rank"].get(r.id, 0.0))     # stable: ties keep newest first

    summaries = []
    for r in results:
        summary = schemas.RecipeSummaryOut.model_validate(r)
        summary.has_notes = bool((r.notes or "").strip())
        if r.id in matched_via:
            summary.matched_via = sorted(matched_via[r.id])
        if found and r.id in found["snippets"]:
            summary.match_snippet = found["snippets"][r.id][:300]
        summaries.append(summary)
    return summaries


def _sort_clauses(sort: str, direction: str):
    """ORDER BY terms for the recipe list.

    Two things worth knowing about this:

    Unrated / untimed recipes always sort LAST, in both directions. Most
    recipes have no rating and no logged cook time, so a plain ascending
    sort by rating opens with a wall of blanks -- correct, and useless. The
    null-flag term below is deliberately not reversed with the direction,
    which means ascending and descending are not strict mirrors of each
    other. That is the intended trade.

    `NULLS LAST` is avoided in favour of an `x IS NULL` flag column: the
    former needs SQLite 3.30+, and this app ships wherever the host's
    Python happens to link its SQLite from.
    """
    is_desc = direction == "desc"
    order = desc if is_desc else asc

    if sort == "title":
        # Case-insensitive, or "apple" sorts after "Zucchini".
        primary = order(func.lower(models.Recipe.title))
        return (primary, models.Recipe.id.asc())

    if sort == "rating":
        col = models.Recipe.tastiness_rating
    elif sort == "cook_time":
        col = models.Recipe.actual_cook_time_minutes
    elif sort == "difficulty":
        # Stored as text, so it needs an explicit ordinal -- alphabetically
        # "hard" < "medium", which is not the order anyone means.
        col = case(
            (models.Recipe.difficulty_rating == "easy", 1),
            (models.Recipe.difficulty_rating == "medium", 2),
            (models.Recipe.difficulty_rating == "hard", 3),
            else_=None,
        )
    else:  # "created"
        # created_at is never null, so no null-flag term is needed.
        return (order(models.Recipe.created_at), models.Recipe.id.asc())

    # (col IS NULL) sorts 0 before 1, i.e. real values first, always.
    return (asc(col.is_(None)), order(col), models.Recipe.id.asc())


@app.get("/api/recipes/duplicates")
def recipe_duplicates(title: str | None = Query(default=None, max_length=500),
                      url: str | None = Query(default=None, max_length=4000),
                      exclude: Optional[schemas.DbInt] = None,
                      db: Session = Depends(get_db)):
    """Saved recipes with the same link or title (duplicates.py), for the
    warning on the review screen and Add manually. Declared before
    /api/recipes/{recipe_id}, which would otherwise take "duplicates"."""
    return duplicates.find(db, title=title, url=url, exclude_id=exclude)


@app.get("/api/time-buckets")
def get_time_buckets(db: Session = Depends(get_db)):
    """Available '<= X' time-filter options, generated only from data on
    hand (see time_utils.available_time_buckets)."""
    rows = db.query(models.Recipe.actual_cook_time_minutes).filter(
        models.Recipe.actual_cook_time_minutes.isnot(None)
    ).all()
    logged = [r[0] for r in rows]
    return available_time_buckets(logged)


@app.get("/api/library-check")
def library_check(db: Session = Depends(get_db)):
    """Settings -> Library check (0111): recipes missing something, and
    sets that look like the same recipe (duplicates.groups). Read only."""
    ing = dict(db.query(models.Ingredient.recipe_id, func.count()).group_by(models.Ingredient.recipe_id).all())
    steps = dict(db.query(models.Step.recipe_id, func.count()).group_by(models.Step.recipe_id).all())
    tagged = {rid for (rid,) in db.query(models.recipe_tags.c.recipe_id).distinct()}
    checks = {k: [] for k in ("no_ingredients", "no_steps", "no_tags", "no_photo", "photo_missing", "to_review")}
    rows = db.query(models.Recipe.id, models.Recipe.title, models.Recipe.image_path, models.Recipe.needs_review).all()
    for rid, title, image, review in sorted(rows, key=lambda r: (r[1] or "").casefold()):
        item = {"id": rid, "title": title}
        if not ing.get(rid):
            checks["no_ingredients"].append(item)
        if not steps.get(rid):
            checks["no_steps"].append(item)
        if rid not in tagged:
            checks["no_tags"].append(item)
        if not image:
            checks["no_photo"].append(item)
        else:
            path = safe_join(UPLOADS_DIR, image)
            if not path or not os.path.isfile(path):
                checks["photo_missing"].append(item)
        if review:
            checks["to_review"].append(item)
    return {"total": len(rows), "checks": checks, "duplicates": duplicates.groups(db)}


@app.get("/api/review-queue")
def review_queue(db: Session = Depends(get_db)):
    """How many recipes are still to review (the sidebar's "To review")."""
    return {"count": db.query(models.Recipe).filter(models.Recipe.needs_review.is_(True)).count()}


@app.get("/api/recipes/{recipe_id}/review-hints")
def recipe_review_hints(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """What to check in a recipe saved without review (review.py)."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    uncertain = learning.uncertain_lines(recipe.raw_text) if recipe.raw_text else None
    return review.hints(recipe, uncertain)


@app.post("/api/recipes/{recipe_id}/reviewed", response_model=schemas.RecipeOut)
def mark_reviewed(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """"Looks right": off the review list, and a training example from the
    next retrain (scheduled by retrain_after_changes)."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    recipe.needs_review = False
    db.commit()
    db.refresh(recipe)
    return recipe


@app.post("/api/recipes/{recipe_id}/duplicate", response_model=schemas.RecipeOut)
def duplicate_recipe(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """A copy to make a variant from: everything the recipe has (title with
    " (copy)", ingredients, steps, tags, times, servings, notes, ratings,
    source and original text), with its own copy of the photo so deleting
    one leaves the other's. Not copied: favourite and the "To review" mark.
    The page opens the copy in Edit."""
    src = db.get(models.Recipe, recipe_id)
    if not src:
        raise HTTPException(status_code=404, detail="Recipe not found")
    suffix = " (copie)" if i18n.current_lang.get() == "fr" else " (copy)"
    image = None
    if src.image_path:
        old_path = safe_join(UPLOADS_DIR, src.image_path)
        if old_path and os.path.isfile(old_path):
            image = f"copy-{uuid.uuid4().hex}{os.path.splitext(src.image_path)[1]}"
            shutil.copyfile(old_path, os.path.join(UPLOADS_DIR, image))
    try:
        copy = models.Recipe(
            title=(src.title or "")[:500 - len(suffix)] + suffix,
            source_type=src.source_type, source_url=src.source_url, servings=src.servings,
            prep_time=src.prep_time, cook_time=src.cook_time, total_time=src.total_time,
            image_path=image, raw_text=src.raw_text, notes=src.notes, ocr_confidence=src.ocr_confidence,
            autotag_ignored=src.autotag_ignored, tastiness_rating=src.tastiness_rating,
            cook_time_rating=src.cook_time_rating, difficulty_rating=src.difficulty_rating,
            language=src.language, actual_cook_time_minutes=src.actual_cook_time_minutes,
        )
        db.add(copy)
        db.flush()
        for ing in src.ingredients:
            db.add(models.Ingredient(recipe_id=copy.id, position=ing.position, raw_line=ing.raw_line,
                                     quantity=ing.quantity, unit=ing.unit, name=ing.name))
        for step in src.steps:
            db.add(models.Step(recipe_id=copy.id, position=step.position, text=step.text))
        _apply_tags(db, copy, [schemas.TagIn(name=t.name, category=t.category, subgroup=t.subgroup) for t in src.tags])
        db.commit()
    except Exception:
        log.warning("duplicate_recipe: copy of %s not saved", recipe_id, exc_info=True)
        db.rollback()
        if image:
            remove_upload_file(os.path.join(UPLOADS_DIR, image))
        raise
    db.refresh(copy)
    log.info("Recipe %s duplicated as %s", recipe_id, copy.id)
    return copy


NOTES_MAX = 50_000   # schemas.NotesUpdate


@app.post("/api/recipes/merge", response_model=schemas.RecipeOut)
def merge_recipes(payload: schemas.MergeRequest, db: Session = Depends(get_db)):
    """Two recipes into one (0110). The "keep" recipe is updated in place
    with the part chosen from each; the other goes to Recently deleted, so
    it can be put back. Tags: both sets. Notes or tags that together go
    over a recipe's limits refuse the merge rather than being cut. A photo taken from the other recipe is copied, since
    the other keeps its own while it waits. The result counts as checked
    (not "To review")."""
    if payload.keep_id == payload.other_id:
        raise HTTPException(status_code=400, detail="Pick two different recipes.")
    keep, other = db.get(models.Recipe, payload.keep_id), db.get(models.Recipe, payload.other_id)
    if keep is None or other is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    take = payload.take
    pick = lambda part: other if getattr(take, part) == "other" else keep    # noqa: E731

    notes = {"keep": keep.notes, "other": other.notes,
             "both": "\n\n".join(n.strip() for n in (keep.notes, other.notes) if n and n.strip())}[take.notes]
    # Nothing is cut off without saying so (GPT review of 0119): the merge
    # is refused and the page shows why.
    if notes and len(notes) > NOTES_MAX:
        raise HTTPException(status_code=400, detail=f"The two recipes' notes together are longer than "
                            f"{NOTES_MAX:,} characters. Keep one recipe's notes, or shorten them first.")
    tag_count = len(keep.tags) + len([t for t in other.tags if t not in keep.tags])
    if tag_count > MAX_TAGS_PER_RECIPE:
        raise HTTPException(status_code=400, detail=f"Together these recipes have {tag_count} tags; a recipe "
                            f"can have at most {MAX_TAGS_PER_RECIPE}. Remove some tags first.")

    # The photo is copied only once the merge can go ahead: a refusal above
    # left the copy behind with nothing pointing at it (GPT review of 0123).
    new_image, dropped_image = keep.image_path, None
    if take.photo == "other" and other.image_path:
        src = safe_join(UPLOADS_DIR, other.image_path)
        if src and os.path.isfile(src):
            new_image = f"merge-{uuid.uuid4().hex}{os.path.splitext(other.image_path)[1]}"
            shutil.copyfile(src, os.path.join(UPLOADS_DIR, new_image))
    elif take.photo == "other" or take.photo == "none":
        new_image = None
    if new_image != keep.image_path:
        dropped_image = keep.image_path
    try:
        keep.title = pick("title").title
        keep.image_path = new_image
        if take.content == "other":
            db.query(models.Ingredient).filter_by(recipe_id=keep.id).delete()
            db.query(models.Step).filter_by(recipe_id=keep.id).delete()
            for ing in other.ingredients:
                db.add(models.Ingredient(recipe_id=keep.id, position=ing.position, raw_line=ing.raw_line,
                                         quantity=ing.quantity, unit=ing.unit, name=ing.name))
            for step in other.steps:
                db.add(models.Step(recipe_id=keep.id, position=step.position, text=step.text))
            keep.raw_text, keep.source_type, keep.ocr_confidence = other.raw_text, other.source_type, other.ocr_confidence
            # A language set by hand describes the text it came with.
            keep.language = other.language
        d = pick("details")
        keep.servings, keep.prep_time, keep.cook_time, keep.total_time = d.servings, d.prep_time, d.cook_time, d.total_time
        keep.source_url = pick("source").source_url
        r = pick("ratings")
        (keep.favorite, keep.tastiness_rating, keep.cook_time_rating, keep.difficulty_rating,
         keep.actual_cook_time_minutes) = (r.favorite, r.tastiness_rating, r.cook_time_rating,
                                           r.difficulty_rating, r.actual_cook_time_minutes)
        keep.notes = notes or None
        keep.tags = list(keep.tags) + [t for t in other.tags if t not in keep.tags]
        keep.tags_text = " ".join(t.name for t in keep.tags)
        keep.needs_review = False
        other.deleted_at = _utc_now()
        db.commit()
    except Exception:
        log.warning("merge_recipes: %s + %s not saved", keep.id, other.id, exc_info=True)
        db.rollback()
        if new_image and new_image != keep.image_path and new_image.startswith("merge-"):
            remove_upload_file(os.path.join(UPLOADS_DIR, new_image))
        raise
    if dropped_image:
        path = safe_join(UPLOADS_DIR, dropped_image)
        if path and os.path.isfile(path):
            try:
                remove_upload_file(path)
            except OSError:
                log.debug("merge_recipes: old photo not removed", exc_info=True)
    db.refresh(keep)
    log.info("Recipes merged: %s kept, %s to Recently deleted", keep.id, other.id)
    return keep


@app.get("/api/recipes/{recipe_id}", response_model=schemas.RecipeOut)
def get_recipe(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


@app.delete("/api/recipes/{recipe_id}")
def delete_recipe(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """To Recently deleted: hidden everywhere, restorable for
    RECENTLY_DELETED_DAYS, then deleted for good (with its files) by the
    sweep, or sooner from Recently deleted."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    recipe.deleted_at = _utc_now()
    db.commit()
    return {"ok": True, "days": RECENTLY_DELETED_DAYS}


@app.post("/api/recipes/batch-delete")
def batch_delete_recipes(payload: schemas.BatchDeleteRequest, db: Session = Depends(get_db)):
    """To Recently deleted, like a single delete. Per-item commit, so a
    failure mid-batch leaves the ones before it done and reported."""
    deleted, missing, failed = [], [], []
    for recipe_id in payload.ids:
        recipe = db.get(models.Recipe, recipe_id)
        if not recipe:
            missing.append(recipe_id)
            continue
        try:
            recipe.deleted_at = _utc_now()
            db.commit()
        except Exception as e:
            log.warning("batch_delete_recipes: caught error, continuing", exc_info=True)
            db.rollback()
            failed.append({"id": recipe_id, "error": str(e)})
            continue
        deleted.append(recipe_id)
    return {"deleted": deleted, "missing": missing, "failed": failed, "days": RECENTLY_DELETED_DAYS}


# ---------------------------------------------------------------------------
# Recently deleted (0109). A deleted recipe keeps its row and files, with
# deleted_at set, and is hidden from every query (models._hide_recently_
# deleted). It can be put back for RECENTLY_DELETED_DAYS; after that the
# sweep (start-up and every 6 hours) deletes it for good, files included.
# ---------------------------------------------------------------------------
RECENTLY_DELETED_DAYS = 30


def _binned(db: Session):
    return (db.query(models.Recipe).execution_options(include_deleted=True)
            .filter(models.Recipe.deleted_at.isnot(None)))


def _delete_for_good(db: Session, recipes: list) -> int:
    """Rows first, files once the commit has landed (a file deleted before
    a failed commit would leave a row pointing at nothing)."""
    for r in recipes:
        db.delete(r)
    db.commit()
    for r in recipes:
        _delete_recipe_files(r)
    return len(recipes)


def purge_recently_deleted() -> int:
    """The sweep's part: recipes deleted more than RECENTLY_DELETED_DAYS ago."""
    db = SessionLocal()
    try:
        cutoff = _utc_now() - timedelta(days=RECENTLY_DELETED_DAYS)
        gone = _delete_for_good(db, _binned(db).filter(models.Recipe.deleted_at < cutoff).all())
    finally:
        db.close()
    if gone:
        log.info("Recently deleted: %d recipe(s) older than %d days deleted for good", gone, RECENTLY_DELETED_DAYS)
    return gone


@app.get("/api/recently-deleted")
def recently_deleted(db: Session = Depends(get_db)):
    now = _utc_now()
    out = []
    for r in _binned(db).order_by(models.Recipe.deleted_at.desc(), models.Recipe.id.desc()).all():
        left = r.deleted_at + timedelta(days=RECENTLY_DELETED_DAYS) - now
        out.append({"id": r.id, "title": r.title, "image_path": r.image_path,
                    "deleted_at": _iso_utc(r.deleted_at),
                    "days_left": max(0, left.days + (1 if left.seconds or left.microseconds else 0))})
    return {"days": RECENTLY_DELETED_DAYS, "recipes": out}


@app.post("/api/recently-deleted/{recipe_id}/restore")
def restore_deleted_recipe(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    recipe = _binned(db).filter(models.Recipe.id == recipe_id).first()
    if recipe is None:
        raise HTTPException(status_code=404, detail="Not in Recently deleted")
    recipe.deleted_at = None
    # A tag deleted meanwhile left its name in the search column.
    recipe.tags_text = " ".join(t.name for t in recipe.tags)
    db.commit()
    return {"ok": True, "id": recipe.id, "title": recipe.title}


@app.delete("/api/recently-deleted/{recipe_id}")
def delete_recipe_for_good(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    recipe = _binned(db).filter(models.Recipe.id == recipe_id).first()
    if recipe is None:
        raise HTTPException(status_code=404, detail="Not in Recently deleted")
    _delete_for_good(db, [recipe])
    return {"ok": True}


@app.delete("/api/recently-deleted")
def empty_recently_deleted(db: Session = Depends(get_db)):
    return {"deleted": _delete_for_good(db, _binned(db).all())}


# A recipe holds at most this many tags (RecipeCreate.tags).
MAX_TAGS_PER_RECIPE = 50


@app.post("/api/recipes/batch-tags")
def batch_tag_recipes(payload: schemas.BatchTagRequest, db: Session = Depends(get_db)):
    """Select mode -> Tags: add one existing tag to, or remove it from,
    every selected recipe, in one commit. Reports which recipes changed,
    which already had it (or didn't), which have no room for another tag
    and which no longer exist. The retrain middleware picks this path up."""
    tag = db.get(models.Tag, payload.tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    changed, unchanged, full, missing = [], [], [], []
    for recipe_id in dict.fromkeys(payload.ids):
        recipe = db.get(models.Recipe, recipe_id)
        if recipe is None:
            missing.append(recipe_id)
            continue
        has = any(t.id == tag.id for t in recipe.tags)
        if payload.action == "add" and not has:
            if len(recipe.tags) >= MAX_TAGS_PER_RECIPE:
                full.append(recipe_id)
                continue
            recipe.tags.append(tag)
        elif payload.action == "remove" and has:
            recipe.tags = [t for t in recipe.tags if t.id != tag.id]
        else:
            unchanged.append(recipe_id)
            continue
        recipe.tags_text = " ".join(t.name for t in recipe.tags)   # search column, see _apply_tags
        changed.append(recipe_id)
    db.commit()
    log.info("Batch tags: %s %r on %d recipe(s)", payload.action, tag.name, len(changed))
    return {"changed": changed, "unchanged": unchanged, "full": full, "missing": missing}


@app.put("/api/recipes/{recipe_id}", response_model=schemas.RecipeOut)
def update_recipe(recipe_id: schemas.DbId, payload: schemas.RecipeCreate, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    new_image = _promote_temp_file(payload.image_path, current=recipe.image_path)
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
        # Left out of the request: unchanged (schemas.RecipeCreate).
        if "language" in payload.model_fields_set:
            recipe.language = payload.language
        recipe.actual_cook_time_minutes = _logged_minutes(payload.actual_cook_time)
        # Saved from Edit: someone has looked at it.
        recipe.needs_review = False

        db.query(models.Ingredient).filter_by(recipe_id=recipe.id).delete()
        db.query(models.Step).filter_by(recipe_id=recipe.id).delete()
        for pos, ing in enumerate(payload.ingredients):
            db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **_ingredient_fields(ing)))
        for pos, step_text in enumerate(payload.steps):
            db.add(models.Step(recipe_id=recipe.id, position=pos, text=step_text))

        _apply_tags(db, recipe, payload.tags)

        db.commit()
    except Exception:
        log.debug("update_recipe: caught error, continuing", exc_info=True)
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
            remove_upload_file(old_path)

    db.refresh(recipe)
    return recipe


@app.patch("/api/recipes/{recipe_id}/rating", response_model=schemas.RecipeOut)
def update_rating(recipe_id: schemas.DbId, payload: schemas.RatingUpdate, db: Session = Depends(get_db)):
    """Lightweight endpoint for the card-level quick controls (favorite
    toggle, tastiness/pace/difficulty popovers) -- avoids resending the
    full recipe payload just to change one rating."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    # The three ratings are cleared by sending an explicit null, so "omitted"
    # and "set to null" have to be told apart -- `is not None` collapses them
    # and makes clearing silently do nothing (200 OK, no change). Pydantic's
    # model_fields_set only contains keys the client actually sent.
    provided = payload.model_fields_set

    # favorite is a non-nullable bool: absent means "leave alone", and there
    # is no null case to honour.
    if payload.favorite is not None:
        recipe.favorite = payload.favorite
    if "tastiness_rating" in provided:
        recipe.tastiness_rating = payload.tastiness_rating
    if "cook_time_rating" in provided:
        recipe.cook_time_rating = payload.cook_time_rating
    if "difficulty_rating" in provided:
        recipe.difficulty_rating = payload.difficulty_rating
    # Like the ratings: null or "" clears, absent leaves it alone.
    if "actual_cook_time" in provided:
        recipe.actual_cook_time_minutes = _logged_minutes(payload.actual_cook_time)
    if "servings" in provided:
        recipe.servings = (payload.servings or "").strip() or None

    db.commit()
    db.refresh(recipe)
    return recipe


@app.patch("/api/recipes/{recipe_id}/notes", response_model=schemas.RecipeOut)
def update_notes(recipe_id: schemas.DbId, payload: schemas.NotesUpdate, db: Session = Depends(get_db)):
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
def update_image(recipe_id: schemas.DbId, payload: schemas.ImageUpdate, db: Session = Depends(get_db)):
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
        log.debug("update_image: caught error, continuing", exc_info=True)
        db.rollback()
        if new_image and new_image != old_image:
            _demote_to_tmp(new_image)
        raise

    # Only remove the superseded file once the swap has actually committed.
    if old_image and old_image != new_image:
        old_path = safe_join(UPLOADS_DIR, old_image)
        if old_path and os.path.isfile(old_path):
            remove_upload_file(old_path)

    db.refresh(recipe)
    return recipe


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@app.post("/api/tags/auto-apply", response_model=schemas.AutoTagResult)
def auto_apply_tags(dry_run: bool = False, payload: Optional[schemas.AutoTagApply] = None,
                    db: Session = Depends(get_db)):
    """Run the tagger over every recipe and add the suggested tags each one
    is missing. Only adds -- tags already on a recipe, including ones added
    by hand, are left as they are, and removing unwanted ones is up to the
    user. dry_run=true reports what would be added without saving, for the
    confirmation step in Settings.

    Suggestions are otherwise made only when a recipe is added, so recipes
    saved before a tagger fix keep the old result (e.g. clams tagged
    Vegetarian before shellfish counted as Fish). The saved steps stand in
    for the page text used at import; imported page text includes site
    navigation that would add noise."""
    recipes = (db.query(models.Recipe)
               .options(selectinload(models.Recipe.ingredients), selectinload(models.Recipe.steps),
                        selectinload(models.Recipe.tags))
               .order_by(models.Recipe.id).all())
    picks = {s.id: set(s.tags) for s in (payload.selections or [])} if payload else {}
    meat_names = meat_tag_names()
    ignored = [r for r in recipes if r.autotag_ignored]
    recipes = [r for r in recipes if not r.autotag_ignored]
    changes = []
    for r in recipes:
        have = {(t.name, t.category) for t in r.tags}
        # Vegetarian is the tagger's "no meat keyword found"; if a meat or fish
        # tag is already on the recipe (say, added by hand), it's wrong.
        has_meat = any(t.category == "main_ingredient" and t.name in meat_names for t in r.tags)
        suggested = suggest_tags(
            r.title, [i.name or i.raw_line for i in r.ingredients], " ".join(s.text for s in r.steps)
        )
        new = [(n, c, sg) for n, c, sg in suggested
               if (n, c) not in have and not (n == "Vegetarian" and has_meat)]
        if payload is not None and payload.selections is not None and not dry_run:
            # Only what was left ticked in the preview. Names are checked
            # against the fresh suggestions, so the category always comes
            # from the tagger and a stale or made-up entry does nothing.
            picked = picks.get(r.id, set())
            new = [t for t in new if t[0] in picked]
        if new:
            changes.append((r, new))

    if not dry_run and changes:
        for r, new in changes:
            r.tags.extend(_get_or_create_tags(
                db, [schemas.TagIn(name=n, category=c, subgroup=sg) for n, c, sg in new]))
            r.tags_text = " ".join(t.name for t in r.tags)   # search column, see _apply_tags
        db.commit()
        log.info("Auto-tag: added %d tag(s) to %d recipe(s)", sum(len(n) for _, n in changes), len(changes))

    return schemas.AutoTagResult(
        dry_run=dry_run,
        recipes_ignored=len(ignored),
        recipes_scanned=len(recipes),
        recipes_changed=len(changes),
        tags_added=sum(len(n) for _, n in changes),
        changes=[schemas.AutoTagChange(id=r.id, title=r.title, added=[n for n, _, _ in new]) for r, new in changes],
    )


@app.patch("/api/recipes/{recipe_id}/autotag-ignore", response_model=schemas.RecipeOut)
def set_autotag_ignored(recipe_id: schemas.DbId, payload: schemas.AutoTagIgnore, db: Session = Depends(get_db)):
    """Leave a recipe out of (or put it back into) the Settings tag scan, so
    the list doesn't fill up with suggestions already turned down."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    recipe.autotag_ignored = payload.ignored
    db.commit()
    db.refresh(recipe)
    return recipe


@app.post("/api/tags/suggest", response_model=list[schemas.TagSuggestionOut])
def suggest_tags_for_form(payload: schemas.TagSuggestRequest):
    """The edit form's Suggest tags button: suggestions for what's typed in
    the form (saved or not), minus tags it already lists, each with where
    it came from. Saves nothing. Works for recipes skipped by the Settings
    scan too."""
    current = {t.strip().lower() for t in payload.current_tags}
    suggested = explain_tags(payload.title, payload.ingredients, " ".join(payload.steps))
    meat = meat_tag_names()
    has_meat = any(t in {m.lower() for m in meat} for t in current) or any(t["name"] in meat for t in suggested)
    return [t for t in suggested
            if t["name"].lower() not in current and not (t["name"] == "Vegetarian" and has_meat)]


# ---------------------------------------------------------------------------
# Settings -> Learning: models trained on the user's own recipes (app/ml)
# ---------------------------------------------------------------------------

@app.get("/api/learning")
def learning_status():
    return learning.status()


@app.post("/api/learning/train")
def learning_train():
    """Retrain now (takes seconds); returns the new status."""
    if not learning.enabled():
        raise HTTPException(status_code=409, detail="Learning is turned off.")
    result = learning.train_now()
    if result is None:
        raise HTTPException(status_code=409, detail="Training is already running.")
    return result


@app.put("/api/learning")
def learning_settings(payload: schemas.LearningSettingsIn):
    return learning.set_enabled(payload.enabled)


# ---------------------------------------------------------------------------
# Tag groups and tag definitions (Settings -> Tag groups)
# ---------------------------------------------------------------------------

@app.get("/api/tag-groups", response_model=list[schemas.TagGroupOut])
def list_tag_groups(db: Session = Depends(get_db)):
    return db.query(models.TagGroup).order_by(models.TagGroup.position, models.TagGroup.label).all()


@app.post("/api/tag-groups", response_model=schemas.TagGroupOut)
def create_tag_group(payload: schemas.TagGroupIn, db: Session = Depends(get_db)):
    label = payload.label.strip()
    if db.query(models.TagGroup).filter(func.lower(models.TagGroup.label) == label.lower()).first():
        raise HTTPException(status_code=409, detail=f'There is already a group called "{label}".')
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "group"
    key, n = f"g-{slug}", 2
    while db.get(models.TagGroup, key):
        key, n = f"g-{slug}-{n}", n + 1
    group = models.TagGroup(key=key, label=label, builtin=False,
                            position=100 + db.query(models.TagGroup).count())
    db.add(group)
    db.commit()
    return group


@app.delete("/api/tag-groups/{key}")
def delete_tag_group(key: str, db: Session = Depends(get_db)):
    """Deletes a group added in Settings and its tags, which are removed
    from any recipes that have them. Built-in groups can't be deleted."""
    group = db.get(models.TagGroup, key)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group.builtin:
        raise HTTPException(status_code=400, detail="Built-in groups can't be deleted.")
    tags = db.query(models.Tag).filter_by(category=key).all()
    learning.drop_known_tags([(t.name, t.category) for t in tags])
    touched = _remove_tags(db, tags)
    db.delete(group)
    db.commit()
    refresh_user_keywords(db)
    return {"deleted_tags": len(tags), "recipes_changed": touched}


def _remove_tags(db: Session, tags: list) -> int:
    """Detach tags from their recipes (keeping each recipe's search column
    right) and delete them. Returns how many recipes changed."""
    touched = set()
    for tag in tags:
        for r in list(tag.recipes):
            r.tags.remove(tag)
            r.tags_text = " ".join(t.name for t in r.tags)
            touched.add(r.id)
        db.delete(tag)
    return len(touched)


@app.post("/api/tags", response_model=schemas.TagOut)
def create_tag(payload: schemas.TagDefIn, db: Session = Depends(get_db)):
    """Add a tag to a group, with the words the tagger should look for.
    If the tag already exists there, its keywords are updated instead."""
    if not db.get(models.TagGroup, payload.category):
        raise HTTPException(status_code=400, detail="No such group.")
    name = payload.name.strip()
    tag = db.query(models.Tag).filter(models.Tag.category == payload.category,
                                      func.lower(models.Tag.name) == name.lower()).first()
    if not tag:
        tag = models.Tag(name=name, category=payload.category, subgroup=None, user_defined=True)
        db.add(tag)
    tag.keywords = ", ".join(_split_keywords(payload.keywords))
    tag.rules_out_vegetarian = payload.rules_out_vegetarian and payload.category == "main_ingredient"
    db.commit()
    db.refresh(tag)
    refresh_user_keywords(db)
    return tag


@app.put("/api/tags/{tag_id}", response_model=schemas.TagOut)
def update_tag(tag_id: schemas.DbId, payload: schemas.TagDefUpdate, db: Session = Depends(get_db)):
    """Change the extra keywords of any tag, built-in ones included."""
    tag = db.get(models.Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    tag.keywords = ", ".join(_split_keywords(payload.keywords))
    tag.rules_out_vegetarian = payload.rules_out_vegetarian and tag.category == "main_ingredient"
    db.commit()
    db.refresh(tag)
    refresh_user_keywords(db)
    return tag


@app.delete("/api/tags/{tag_id}")
def delete_tag(tag_id: schemas.DbId, db: Session = Depends(get_db)):
    """Delete a tag added in Settings, removing it from any recipes."""
    tag = db.get(models.Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    if not tag.user_defined:
        raise HTTPException(status_code=400, detail="Only tags added in Settings can be deleted here.")
    learning.drop_known_tags([(tag.name, tag.category)])
    touched = _remove_tags(db, [tag])
    db.commit()
    refresh_user_keywords(db)
    return {"recipes_changed": touched}


@app.get("/api/tags", response_model=list[schemas.TagOut])
def list_tags(db: Session = Depends(get_db)):
    return db.query(models.Tag).order_by(models.Tag.category, models.Tag.subgroup, models.Tag.name).all()


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


@app.get("/api/email-settings", response_model=schemas.EmailSettingsOut)
def get_email_settings(db: Session = Depends(get_db)):
    return _email_settings_out(_get_email_settings(db))


@app.post("/api/email-settings/encryption-key", response_model=schemas.EmailSettingsOut)
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


@app.put("/api/email-settings", response_model=schemas.EmailSettingsOut)
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


# ---------------------------------------------------------------------------
# Settings -> Logs. The same file `docker logs` mirrors, data/logs/app.log
# plus its rotated copies.
# ---------------------------------------------------------------------------

@app.get("/api/logs")
def view_logs(lines: int = Query(300, ge=1, le=2000), problems: bool = False):
    from .logging_setup import read_records
    return read_records(DATA_DIR, limit=lines, problems_only=problems)


@app.get("/api/logs/download")
def download_logs():
    from .logging_setup import log_files
    files = log_files(DATA_DIR)
    body = b"".join(open(f, "rb").read() for f in files)
    name = f"open-the-pantry-log-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    return Response(content=body, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/api/logs")
def clear_logs_endpoint():
    from .logging_setup import clear_logs
    clear_logs(DATA_DIR)
    log.info("Log cleared from Settings")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Optional HTTPS (Settings -> HTTPS). See https_setup.py.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Settings password (admin_lock.py)
# ---------------------------------------------------------------------------
def _admin_status(token: str | None, request: Request | None = None) -> dict:
    left = admin_lock.seconds_left(token)
    return {"password_set": admin_lock.password_set(), "unlocked": left > 0, "seconds_left": left,
            "reset": admin_lock.was_reset(),
            # Set when this request came over plain http:// while HTTPS is on:
            # the page then links to the secure address instead of asking.
            "https_url": _https_only_url(request) if request is not None else None}


def _with_session(request: Request, token: str) -> JSONResponse:
    res = JSONResponse(_admin_status(token, request))
    res.set_cookie(admin_lock.COOKIE_NAME, token, max_age=admin_lock.UNLOCK_SECONDS, path="/api",
                   httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return res


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _too_many(e: admin_lock.TooManyTries):
    raise HTTPException(status_code=429, detail=f"Too many wrong passwords. Try again in {e.wait} seconds.",
                        headers={"Retry-After": str(e.wait)})


def _refuse_plain_http(request: Request):
    url = _https_only_url(request)
    if url:
        # Same body as the middleware's refusal, so the page can offer the
        # link whichever of the two refused (HTTPS turned on mid-session).
        raise _HttpsOnly(url)


@app.get("/api/admin/status")
def admin_status(request: Request):
    return _admin_status(request.cookies.get(admin_lock.COOKIE_NAME), request)


async def _clear_credentials_after_reset():
    """The settings password was reset (its file deleted): whoever creates
    the new one sets the email password and HTTPS up again. Runs with the
    HTTPS setup lock held throughout: refused before anything is cleared if
    a setup is running, and no setup can start until it's done."""
    with https_setup.reserved():
        db = SessionLocal()
        try:
            settings = _get_email_settings(db)
            settings.password_encrypted = None
            settings.enabled = False
            db.commit()
        finally:
            db.close()
        await https_setup.stop_server()
        https_setup.forget_certificate()
        https_setup.forget_token()


@app.post("/api/admin/password")
async def admin_create_password(payload: schemas.AdminPasswordIn, request: Request):
    """First use only: once a password exists, this is refused. After a
    reset, the saved email password and HTTPS are cleared first.

    Clearing first is a trade-off, not an oversight (reviewed three times).
    Password first, clearing second: if the clearing then failed, the new
    password would open the old credentials -- the one thing a reset must
    not allow. Clearing first: if saving the password then fails, the
    credentials are gone and no password exists yet; the error says so and
    a retry works. The second failure only costs re-entering credentials."""
    _refuse_plain_http(request)
    if admin_lock.password_set():
        raise HTTPException(status_code=409, detail="A settings password already exists.")
    try:
        admin_lock.check_new_password(payload.password)
    except admin_lock.PasswordError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cleared = admin_lock.was_reset()
    if cleared:
        try:
            await _clear_credentials_after_reset()
        except https_setup.HttpsError as e:
            raise HTTPException(status_code=409, detail=str(e))
    try:
        token = admin_lock.create(payload.password)
    except admin_lock.PasswordError as e:
        raise HTTPException(status_code=409 if admin_lock.password_set() else 400, detail=str(e))
    except OSError as e:
        # Clearing comes first on purpose: if it ran second and failed, the
        # new password would inherit the old credentials. So a failure here
        # leaves them cleared; say so, and that trying again is all it takes.
        log.exception("Couldn't save the new settings password")
        raise HTTPException(status_code=500, detail=(
            "The new password couldn't be saved. The email password and HTTPS were already cleared; "
            f"try again. ({e.strerror or e})" if cleared else
            f"The new password couldn't be saved; try again. ({e.strerror or e})"))
    if cleared:
        log.warning("Settings password was reset and created again from %s: the email password was "
                    "cleared and HTTPS turned off (certificate deleted)", _client(request))
    else:
        log.info("Settings password created from %s", _client(request))
    return _with_session(request, token)


@app.post("/api/admin/unlock")
def admin_unlock(payload: schemas.AdminPasswordIn, request: Request):
    _refuse_plain_http(request)
    if not admin_lock.password_set():
        raise HTTPException(status_code=400, detail="Create a settings password first.")
    try:
        token = admin_lock.unlock(payload.password, _client(request))
    except admin_lock.TooManyTries as e:
        _too_many(e)
    if not token:
        log.warning("Wrong settings password from %s", _client(request))
        raise HTTPException(status_code=401, detail="Wrong password.")
    return _with_session(request, token)


@app.post("/api/admin/password/change")
def admin_change_password(payload: schemas.AdminPasswordChangeIn, request: Request):
    _refuse_plain_http(request)
    try:
        admin_lock.check_new_password(payload.new_password)
        token = admin_lock.change(payload.current_password, payload.new_password, _client(request))
    except admin_lock.PasswordError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except admin_lock.TooManyTries as e:
        _too_many(e)
    if not token:
        log.warning("Wrong settings password (change) from %s", _client(request))
        raise HTTPException(status_code=401, detail="Wrong password.")
    log.info("Settings password changed from %s", _client(request))
    return _with_session(request, token)


@app.post("/api/admin/lock")
def admin_lock_now(request: Request):
    admin_lock.lock(request.cookies.get(admin_lock.COOKIE_NAME))
    res = JSONResponse(_admin_status(None))
    res.delete_cookie(admin_lock.COOKIE_NAME, path="/api")
    return res


@app.get("/api/email-status")
def email_status(db: Session = Depends(get_db)):
    """Open (no password): only whether the Inbox button and Share -> Email
    PDF should be offered. The settings themselves are behind the password."""
    settings = _get_email_settings(db)
    return {"enabled": bool(settings.enabled), "can_send": _can_send_email(settings)}


@app.get("/api/https/summary")
def https_summary():
    """Open (no password): the line under Settings -> HTTPS."""
    st = https_setup.status()
    return {"state": st.get("state"), "enabled": bool(st.get("enabled")), "url": st.get("url")}


@app.get("/api/https")
def get_https_status():
    return https_setup.status()


@app.put("/api/https/credentials")
def save_https_login(payload: schemas.HttpsLoginIn):
    """The cPanel login and the name, entered in Settings -> HTTPS (behind
    the settings password like every /api/https route)."""
    try:
        https_setup.save_login(payload.username, payload.base_url, payload.domain, payload.token)
    except (https_setup.HttpsError, crypto.EncryptionNotConfiguredError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return https_setup.status()


@app.delete("/api/https/credentials")
def clear_https_login():
    if https_setup.status()["state"] == "working":
        raise HTTPException(status_code=409, detail="HTTPS setup is running. Try again when it has finished.")
    https_setup.clear_login()
    return https_setup.status()


@app.post("/api/https/setup")
async def setup_https(payload: schemas.HttpsSetupIn):
    try:
        domain, lan, email, port = https_setup.validate(payload.domain, payload.lan_address,
                                                        payload.email, payload.port)
    except https_setup.HttpsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cred = https_setup.credentials_status()
    if not cred["found"]:
        raise HTTPException(status_code=400, detail=str(https_setup.missing_settings_error(cred["missing"])))
    request = {"domain": domain, "lan_address": lan, "email": email, "port": port}
    if not https_setup.start_job(asyncio.get_running_loop(), app, request):
        raise HTTPException(status_code=409, detail="HTTPS setup is already running.")
    return https_setup.status()


@app.post("/api/https/renew")
async def renew_https():
    st = https_setup.status()
    if not st["domain"]:
        raise HTTPException(status_code=400, detail="Set up HTTPS first.")
    if not https_setup.start_job(asyncio.get_running_loop(), app):
        raise HTTPException(status_code=409, detail="HTTPS setup is already running.")
    return https_setup.status()


@app.delete("/api/https")
async def turn_off_https():
    await https_setup.stop_server()
    https_setup.turn_off()
    return https_setup.status()


# ---------------------------------------------------------------------------
# What to eat? (picker.py): its API on this port too, for the top-bar link;
# the on/off switch on this port only; and the read-only app the picker's
# own port serves. That app gets this port's host check, rate limit, error
# logging, cache headers and language middleware, added in the same order
# as here, but not the API key or the settings password: it has no write
# or admin route for them to protect.
# ---------------------------------------------------------------------------
app.include_router(picker.router)
app.include_router(picker.settings_router)
picker_app = picker.create_app([log_unhandled_errors, restore_guard, cross_site_guard, rate_limiter,
                                app_files_revalidate, interface_language])
picker.set_api_key_check(lambda: bool(API_KEY))


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


@app.get("/api/recipes/{recipe_id}/export.pdf")
def export_pdf(recipe_id: schemas.DbId, include_notes: bool = False, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    abs_image = safe_join(UPLOADS_DIR, recipe.image_path) if recipe.image_path else None
    # The showcase image is deliberately never included in the shared PDF --
    # not a query param/user choice, unlike include_notes.
    pdf_bytes = render_recipe_pdf(recipe, abs_image, include_notes=include_notes, include_image=False)
    disposition = _attachment_disposition(recipe.title, "pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


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


@app.put("/api/email-settings/recipients", response_model=schemas.RecentRecipients)
def update_recent_recipients(payload: schemas.RecentRecipients, db: Session = Depends(get_db)):
    """Settings -> Email ingest: edit the list of recent recipients."""
    import json
    settings = _get_email_settings(db)
    addresses = _clean_addresses(payload.recipients)[:MAX_RECENT_RECIPIENTS]
    settings.recent_recipients = json.dumps(addresses)
    db.commit()
    return {"recipients": addresses}


@app.post("/api/recipes/{recipe_id}/email")
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


@app.get("/api/recipes/{recipe_id}/export.html")
def export_html(recipe_id: schemas.DbId, include_notes: bool = False, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    abs_image = safe_join(UPLOADS_DIR, recipe.image_path) if recipe.image_path else None
    html_str = render_recipe_html(recipe, abs_image, include_notes=include_notes)
    disposition = _attachment_disposition(recipe.title, "html")
    return Response(
        content=html_str,
        media_type="text/html",
        headers={"Content-Disposition": disposition},
    )


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


@app.get("/api/backup/info")
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


@app.get("/api/backup/database.zip")
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


@app.get("/api/backup/pdfs.zip")
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


@app.get("/api/backup/recipes-json.zip")
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


@app.get("/api/recipes/{recipe_id}/export.json")
def export_json(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """One recipe as a schema.org Recipe .json (Share -> Download JSON).
    Without its photo: a single .json file has nowhere to put it."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    body = json.dumps(interchange.recipe_to_jsonld(recipe), ensure_ascii=False, indent=1)
    return Response(content=body, media_type="application/ld+json",
                    headers={"Content-Disposition": _attachment_disposition(recipe.title, "json")})


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


@app.post("/api/import/recipes")
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


@app.get("/api/backups/status")
def backups_status(db: Session = Depends(get_db)):
    """The schedule, the last result and the backups in the folder."""
    return _backup_status(db)


@app.put("/api/backups/schedule")
def backups_schedule(payload: schemas.BackupScheduleIn, db: Session = Depends(get_db)):
    row = _backup_schedule(db)
    row.enabled, row.hour, row.keep = payload.enabled, payload.hour, payload.keep
    db.commit()
    log.info("Automatic backups %s (%02d:00, keep %d) in %s", "on" if payload.enabled else "off",
             payload.hour, payload.keep, scheduled_backup.folder())
    if payload.enabled:
        scheduled_backup.prune(payload.keep)
    return _backup_status(db)


@app.post("/api/backups/run")
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


@app.post("/api/backup/restore/check")
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


def _keep_https_rows() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT * FROM https_settings")).mappings().all()
    return [dict(r) for r in rows]


def _put_back_https_rows(rows: list[dict]):
    with engine.begin() as conn:
        cols = {r[1] for r in conn.execute(text("PRAGMA table_info(https_settings)")).all()}
        conn.execute(text("DELETE FROM https_settings"))
        for row in rows:
            row = {k: v for k, v in row.items() if k in cols}
            names = ", ".join(row)
            conn.execute(text(f"INSERT INTO https_settings ({names}) VALUES ({', '.join(':' + k for k in row)})"), row)


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


@app.post("/api/backup/restore")
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
                if _inflight <= 1:
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
        build_database_backup(os.path.join(DATA_DIR, safety_name))
        log.info("Restore: current data saved to %s", safety_name)
        https_rows = _keep_https_rows()

        new_uploads = UPLOADS_DIR.rstrip("/") + ".restore-new"
        shutil.rmtree(new_uploads, ignore_errors=True)
        kept, left_out = restore_mod.unpack_uploads(zip_path, new_uploads)

        engine.dispose()
        restore_mod.copy_into_live(db_path, DB_PATH)
        engine.dispose()
        init_db()                                     # an older backup's schema brought up to date
        _put_back_https_rows(https_rows)
        old_uploads = restore_mod.swap_dirs(new_uploads, UPLOADS_DIR)
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

# The image copies frontend/ to /app/static (Dockerfile: COPY frontend
# ./static), next to /app/app. Without it the API works but "/" is a 404,
# so say so in the log rather than fail quietly.
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")
if os.path.isdir(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
else:
    log.warning("No web app files at %s: the API runs, but the app's pages won't load", os.path.realpath(STATIC_DIR))
