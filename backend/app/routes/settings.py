"""Settings password, Logs, and HTTPS."""
import asyncio
from datetime import datetime
from fastapi import HTTPException, Query, Request
from fastapi.responses import Response, JSONResponse
from .. import schemas
from ..database import SessionLocal, DATA_DIR
from .. import crypto
from .. import https_setup
from .. import admin_lock
from .common import _HttpsOnly, _https_only_url
from .email import _get_email_settings
from fastapi import APIRouter
from ..logging_setup import get_logger

log = get_logger("app")
router = APIRouter()


# ---------------------------------------------------------------------------
# Settings -> Logs. The same file `docker logs` mirrors, data/logs/app.log
# plus its rotated copies.
# ---------------------------------------------------------------------------

@router.get("/api/logs")
def view_logs(lines: int = Query(300, ge=1, le=2000), problems: bool = False):
    from ..logging_setup import read_records
    return read_records(DATA_DIR, limit=lines, problems_only=problems)


@router.get("/api/logs/download")
def download_logs():
    from ..logging_setup import log_files
    files = log_files(DATA_DIR)
    body = b"".join(open(f, "rb").read() for f in files)
    name = f"open-the-pantry-log-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    return Response(content=body, media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.delete("/api/logs")
def clear_logs_endpoint():
    from ..logging_setup import clear_logs
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


@router.get("/api/admin/status")
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


@router.post("/api/admin/password")
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


@router.post("/api/admin/unlock")
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


@router.post("/api/admin/password/change")
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


@router.post("/api/admin/lock")
def admin_lock_now(request: Request):
    admin_lock.lock(request.cookies.get(admin_lock.COOKIE_NAME))
    res = JSONResponse(_admin_status(None))
    res.delete_cookie(admin_lock.COOKIE_NAME, path="/api")
    return res


@router.get("/api/https/summary")
def https_summary():
    """Open (no password): the line under Settings -> HTTPS."""
    st = https_setup.status()
    return {"state": st.get("state"), "enabled": bool(st.get("enabled")), "url": st.get("url")}


@router.get("/api/https")
def get_https_status():
    return https_setup.status()


@router.put("/api/https/credentials")
def save_https_login(payload: schemas.HttpsLoginIn):
    """The cPanel login and the name, entered in Settings -> HTTPS (behind
    the settings password like every /api/https route)."""
    try:
        https_setup.save_login(payload.username, payload.base_url, payload.domain, payload.token)
    except (https_setup.HttpsError, crypto.EncryptionNotConfiguredError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return https_setup.status()


@router.delete("/api/https/credentials")
def clear_https_login():
    if https_setup.status()["state"] == "working":
        raise HTTPException(status_code=409, detail="HTTPS setup is running. Try again when it has finished.")
    https_setup.clear_login()
    return https_setup.status()


@router.post("/api/https/setup")
async def setup_https(payload: schemas.HttpsSetupIn, http_request: Request):
    try:
        domain, lan, email, port = https_setup.validate(payload.domain, payload.lan_address,
                                                        payload.email, payload.port)
    except https_setup.HttpsError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cred = https_setup.credentials_status()
    if not cred["found"]:
        raise HTTPException(status_code=400, detail=str(https_setup.missing_settings_error(cred["missing"])))
    request = {"domain": domain, "lan_address": lan, "email": email, "port": port}
    if not https_setup.start_job(asyncio.get_running_loop(), http_request.app, request):
        raise HTTPException(status_code=409, detail="HTTPS setup is already running.")
    return https_setup.status()


@router.post("/api/https/renew")
async def renew_https(http_request: Request):
    st = https_setup.status()
    if not st["domain"]:
        raise HTTPException(status_code=400, detail="Set up HTTPS first.")
    if not https_setup.start_job(asyncio.get_running_loop(), http_request.app):
        raise HTTPException(status_code=409, detail="HTTPS setup is already running.")
    return https_setup.status()


@router.delete("/api/https")
async def turn_off_https():
    await https_setup.stop_server()
    https_setup.turn_off()
    return https_setup.status()
