"""The settings password.

The app has no login. The exceptions are the parts that use credentials for
an outside service -- the email account and the cPanel DNS token:

  * Settings -> Email ingest (every /api/email-settings route except the
    inbox scan), Settings -> HTTPS (every /api/https route except the
    summary), Settings -> Logs (/api/logs...), and Share -> Email PDF
    (/api/recipes/{id}/email).

Those need the settings password. It is created in the app the first time
one of them is opened, stored as a salted scrypt hash in
data/admin-password.json (not in the database, so not in backups), and
reset by deleting that file. Entering it unlocks that browser for 15
minutes: an HttpOnly, SameSite=Strict cookie holding a random token that
only lives in this process's memory, so a restart locks everything.

A reset is not a fresh start for the credentials: creating a password
after one clears the saved email password and HTTPS (main.py does the
clearing), so whoever sets the new password has to enter them again.
data/admin-password.created records that a password has existed; without
it (a first install, or an upgrade from before the password) creating one
clears nothing.
"""
import contextlib
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

from .database import DATA_DIR
from .logging_setup import get_logger

log = get_logger("settings-password")

PASSWORD_PATH = os.path.join(DATA_DIR, "admin-password.json")
MARKER_PATH = os.path.join(DATA_DIR, "admin-password.created")
COOKIE_NAME = "pantry_settings"
UNLOCK_SECONDS = 15 * 60
MIN_LENGTH = 8
MAX_LENGTH = 256

# scrypt cost: about 0.1 s and 16 MB per check on a small NAS CPU. Slow on
# purpose, so guessing is slow too.
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}

# Wrong guesses: after FREE_TRIES in a row from one address, each further
# try must wait, doubling from BACKOFF_START up to BACKOFF_MAX.
FREE_TRIES = 5
BACKOFF_START = 30
BACKOFF_MAX = 15 * 60

_lock = threading.Lock()
_sessions: dict[str, tuple[float, str]] = {}   # token -> (expires_at, password fingerprint)
_failures: dict[str, tuple[int, float]] = {}   # client address -> (failures in a row, next try allowed at)

# Routes behind the password. Checked by one middleware in main.py, so a
# new route under these paths is locked without anyone remembering to.
_LOCKED = [
    re.compile(r"^/api/email-settings(/.*)?$"),
    re.compile(r"^/api/https(/.*)?$"),
    re.compile(r"^/api/logs(/.*)?$"),
    re.compile(r"^/api/recipes/[^/]+/email$"),
]
_OPEN = {
    "/api/email-settings/scan",   # reads only the recipe inbox; left open on purpose
    "/api/https/summary",         # on/off and the address, nothing else
}


def is_locked_path(path: str) -> bool:
    path = path.rstrip("/") or "/"
    if path in _OPEN:
        return False
    return any(p.match(path) for p in _LOCKED)


class PasswordError(ValueError):
    pass


class TooManyTries(Exception):
    def __init__(self, wait: int):
        super().__init__(wait)
        self.wait = wait


def _read() -> dict | None:
    try:
        with open(PASSWORD_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and {"salt", "hash", "n", "r", "p"} <= d.keys():
            return d
    except (OSError, ValueError):
        pass
    return None


def password_set() -> bool:
    return _read() is not None


def was_reset() -> bool:
    """A password existed and its file has been deleted."""
    return not password_set() and os.path.exists(MARKER_PATH)


def _fingerprint(d: dict) -> str:
    return d["hash"][:16]


def _hash(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          maxmem=64 * 1024 * 1024, dklen=32)


def check_new_password(password: str):
    if len(password) < MIN_LENGTH:
        raise PasswordError(f"The password must be at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordError(f"The password can be at most {MAX_LENGTH} characters.")


def _write(password: str):
    check_new_password(password)
    salt = secrets.token_bytes(16)
    d = {"salt": salt.hex(), "hash": _hash(password, salt, **_SCRYPT).hex(), **_SCRYPT}
    tmp = PASSWORD_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(tmp, PASSWORD_PATH)


def _verify(password: str) -> bool:
    d = _read()
    if d is None:
        return False
    try:
        got = _hash(password, bytes.fromhex(d["salt"]), int(d["n"]), int(d["r"]), int(d["p"]))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), d["hash"])


def _new_session() -> str:
    d = _read()
    token = secrets.token_urlsafe(32)
    with _lock:
        now = time.time()
        for t, (exp, _) in list(_sessions.items()):
            if exp <= now:
                del _sessions[t]
        # d is None only if the file was deleted (a reset) in the instant
        # since it was checked: the session then starts out invalid.
        _sessions[token] = (now + UNLOCK_SECONDS, _fingerprint(d) if d else "")
    return token


def create(password: str) -> str:
    """First use: sets the password and returns an unlocked session.
    Refused once a password exists (changing it needs the old one)."""
    with _lock:
        if password_set():
            raise PasswordError("A settings password already exists.")
        _write(password)
        # The marker is what makes a later reset clear the credentials, so
        # a password without one isn't kept: undo it and report the error.
        if not os.path.exists(MARKER_PATH):
            try:
                with open(MARKER_PATH, "w", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "\n")
            except OSError:
                log.exception("Couldn't write %s; the new settings password is undone", MARKER_PATH)
                with contextlib.suppress(OSError):
                    os.remove(PASSWORD_PATH)
                raise
    return _new_session()


def _check_backoff(client: str):
    with _lock:
        count, allowed_at = _failures.get(client, (0, 0.0))
        wait = int(allowed_at - time.time() + 0.999)
        if wait > 0:
            raise TooManyTries(wait)


def _record(client: str, ok: bool):
    with _lock:
        if ok:
            _failures.pop(client, None)
            return
        now = time.time()
        # Forget addresses whose last wrong guess is long past.
        for c, (_, allowed_at) in list(_failures.items()):
            if allowed_at < now - BACKOFF_MAX:
                del _failures[c]
        count, _ = _failures.get(client, (0, 0.0))
        count += 1
        wait = 0 if count < FREE_TRIES else min(BACKOFF_START * 2 ** (count - FREE_TRIES), BACKOFF_MAX)
        _failures[client] = (count, now + wait)


def unlock(password: str, client: str) -> str | None:
    """A session token for the right password, None for a wrong one."""
    _check_backoff(client)
    ok = _verify(password)
    _record(client, ok)
    return _new_session() if ok else None


def change(current: str, new: str, client: str) -> str | None:
    """Replaces the password; every other unlocked browser is locked."""
    _check_backoff(client)
    ok = _verify(current)
    _record(client, ok)
    if not ok:
        return None
    _write(new)
    with _lock:
        _sessions.clear()
    return _new_session()


def seconds_left(token: str | None) -> int:
    """How long this session stays unlocked; 0 if it isn't. A session ends
    early when the password file is deleted or replaced."""
    if not token:
        return 0
    with _lock:
        entry = _sessions.get(token)
    if not entry:
        return 0
    expires, fp = entry
    d = _read()
    left = int(expires - time.time())
    if d is None or _fingerprint(d) != fp or left <= 0:
        with _lock:
            _sessions.pop(token, None)
        return 0
    return left


def lock(token: str | None):
    if token:
        with _lock:
            _sessions.pop(token, None)
