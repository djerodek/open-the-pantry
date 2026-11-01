"""The FastAPI app: start-up (single-instance lock, schema, sweeps and the
daily loops), the middleware (body size, restore guard, retraining, error
logging, host and cross-site checks, settings password, API key, rate
limit, security headers, translation), the version and health checks, the
What to eat? listener, and the static files. The API's routes are in
app/routes/ (0140; this file held all of them, about 4,200 lines)."""
import asyncio
import fcntl
import hmac
import json
import re
import os
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
import anyio
from fastapi import FastAPI, Request
from fastapi.responses import Response, JSONResponse
from fastapi.staticfiles import StaticFiles
import sqlalchemy.exc
from . import models, schemas
from .database import engine, SessionLocal, DATA_DIR, UPLOADS_DIR, TMP_DIR
from .logging_setup import setup_logging, get_logger

# Before the imports below: their modules log as they load.
setup_logging(DATA_DIR)

from .init_db import init_db
from .backup import remove_upload_file
from . import i18n
from . import https_setup
from . import admin_lock
from . import picker
from . import search
from . import restore as restore_mod
from .ml import service as learning
from .file_validation import MAX_UPLOAD_BYTES
from .routes import common, backup as backup_routes, email as email_routes, ingest as ingest_routes, recipes as recipes_routes, settings as settings_routes
from .routes.backup import _scheduled_backup_loop
from .routes.common import BACKUP_TMP_DIR, _HttpsOnly, _https_only_response, _inflight_lock, _restoring, _settings_lock_refusal, refresh_user_keywords
from .routes.email import _daily_email_scan_loop
from .routes.recipes import purge_recently_deleted

log = get_logger("app")

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


refresh_user_keywords()

TMP_FILE_MAX_AGE_SECONDS = 24 * 60 * 60
TMP_SWEEP_INTERVAL_SECONDS = 6 * 60 * 60  # re-sweep periodically, not just at startup


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

    # A restore stopped between its two folder renames left uploads/ missing
    # (recreated empty at import) beside uploads-before-restore-*.
    restore_mod.recover_interrupted_swap(UPLOADS_DIR)
    # A failed first sweep (the database still locked by the previous
    # container) mustn't stop the app starting; the loop sweeps again
    # (DeepSeek review and audit of 0143).
    try:
        await _run_sweeps()
    except Exception:
        log.exception("Start-up sweep failed; trying again in %d s", TMP_SWEEP_INTERVAL_SECONDS)
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
        return backup_routes.MAX_IMPORT_BYTES + MULTIPART_MARGIN_BYTES, refusal
    limit = {
        "/api/ingest/pdf": MAX_UPLOAD_BYTES,
        "/api/ingest/image": MAX_UPLOAD_BYTES,
        "/api/upload-image": MAX_UPLOAD_BYTES,
        "/api/ingest/images": ingest_routes.MAX_COMBINED_IMAGES * MAX_UPLOAD_BYTES,
        "/api/ingest/pdf/batch": ingest_routes.MAX_BATCH_PDF_BYTES,
        "/api/import/recipes": backup_routes.MAX_IMPORT_BYTES,
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


@app.middleware("http")
async def restore_guard(request: Request, call_next):
    # The count lives in routes.common, where restore_backup reads it.
    if _restoring.is_set() and request.url.path != "/healthz":
        return JSONResponse(status_code=503, content={"detail": "A backup is being restored. Try again in a minute."},
                            headers={"Retry-After": "30"})
    with _inflight_lock:
        common._inflight += 1
    try:
        return await call_next(request)
    finally:
        with _inflight_lock:
            common._inflight -= 1


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
if "*" in ALLOWED_HOSTS:
    # Accepted (a proxy whose name changes), but it turns the host check
    # off (DeepSeek audit of 0143: undocumented, and easy to copy).
    log.warning("RECIPE_APP_ALLOWED_HOSTS contains *: any host name is accepted, which turns off the check that "
                "stops other websites reaching the app through a visitor's browser (DNS rebinding). List the "
                "names instead.")
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


# SQLite's two "try again" results: SQLITE_BUSY ("database is locked",
# another connection's write) and SQLITE_LOCKED ("database table is
# locked", "database schema is locked": a conflict on the same
# connection). Read from the error's code, with the message as the
# fallback for an error without one (GPT review of 0140).
_BUSY_CODES = ("SQLITE_BUSY", "SQLITE_LOCKED")      # and their extended codes (_SNAPSHOT, _SHAREDCACHE, ...)
_BUSY_MESSAGES = ("database is locked", "database table is locked", "database schema is locked")


def _database_busy(exc: sqlalchemy.exc.OperationalError) -> bool:
    orig = exc.orig if exc.orig is not None else exc
    name = getattr(orig, "sqlite_errorname", "") or ""
    return name.startswith(_BUSY_CODES) or any(m in str(orig) for m in _BUSY_MESSAGES)


@app.exception_handler(sqlalchemy.exc.OperationalError)
async def _database_busy_handler(request: Request, exc: sqlalchemy.exc.OperationalError):
    """"database is locked" after BUSY_TIMEOUT_SECONDS: busy, not broken,
    so a 503 that says to try again instead of a bare 500. Any other
    OperationalError is still a 500."""
    if not _database_busy(exc):
        log.error("Database error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    log.warning("Database busy on %s %s: %s", request.method, request.url.path, exc.orig)
    # In French through the response translation, like any detail.
    return JSONResponse(status_code=503, content={"detail": "The app is busy with another change. Try again in a moment."},
                        headers={"Retry-After": "5"})


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
    # Served as the type it's labelled, never sniffed into another (photos
    # and drafts under /uploads and /tmp-preview; security audit of 0140).
    response.headers["X-Content-Type-Options"] = "nosniff"
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
    # A compressed body (a compression middleware or proxy added later)
    # can't be read as JSON here; left as it is, header and all (DeepSeek
    # review of 0143).
    if response.headers.get("content-encoding"):
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


# ---------------------------------------------------------------------------
# What to eat? (picker.py): its API on this port too, for the top-bar link;
# the on/off switch on this port only; and the read-only app the picker's
# own port serves. That app gets this port's host check, rate limit, error
# logging, cache headers and language middleware, added in the same order
# as here, but not the API key or the settings password: it has no write
# or admin route for them to protect.
# ---------------------------------------------------------------------------
# The API's routes, one module per area (app/routes/; split out of this
# file in 0140). Included before the picker's and the static mounts below,
# in the order they were declared here.
for _router in (ingest_routes.router, recipes_routes.router, email_routes.router, settings_routes.router,
                backup_routes.router):
    app.include_router(_router)
app.include_router(picker.router)
app.include_router(picker.settings_router)
picker_app = picker.create_app([log_unhandled_errors, restore_guard, cross_site_guard, rate_limiter,
                                app_files_revalidate, interface_language])
picker.set_api_key_check(lambda: bool(API_KEY))


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
