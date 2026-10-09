"""The settings password: Email, HTTPS, Logs and Email PDF are locked;
everything else in the app stays open (admin_lock.py)."""
import io
import json
import re
import zipfile

import pytest
from fastapi.testclient import TestClient

from .conftest import TEST_SETTINGS_PASSWORD


@pytest.fixture
def stranger(client):
    """Someone else on the network: same app, no unlock cookie."""
    from app.main import app
    from app import admin_lock
    c = TestClient(app, headers={"X-Requested-With": "OpenThePantry"})
    yield c
    with admin_lock._lock:
        admin_lock._failures.clear()


@pytest.fixture
def secure_stranger(client):
    """Someone else, over https:// (needed once HTTPS is on)."""
    from app.main import app
    from app import admin_lock
    c = TestClient(app, base_url="https://testserver", headers={"X-Requested-With": "OpenThePantry"})
    yield c
    with admin_lock._lock:
        admin_lock._failures.clear()


def _routes():
    from app.main import app
    for r in app.routes:
        path = getattr(r, "path", "")
        for m in sorted(getattr(r, "methods", None) or []):
            if m in ("HEAD", "OPTIONS"):
                continue
            yield m, path


def _concrete(path):
    return re.sub(r"\{[^}]+\}", "1", path)


def test_every_credential_route_is_locked(stranger):
    from app import admin_lock
    locked = [(m, p) for m, p in _routes() if admin_lock.is_locked_path(_concrete(p))]
    # The ones the feature is about must be in the list.
    paths = {p for _, p in locked}
    for must in ["/api/email-settings", "/api/email-settings/test", "/api/email-settings/password",
                 "/api/email-settings/encryption-key", "/api/email-settings/recipients",
                 "/api/https", "/api/https/setup", "/api/https/renew",
                 "/api/logs", "/api/logs/download", "/api/recipes/{recipe_id}/email"]:
        assert must in paths, must
    for method, path in locked:
        r = stranger.request(method, _concrete(path))
        assert r.status_code == 401 and r.json().get("locked") is True, (method, path, r.status_code)


def test_the_rest_of_the_app_stays_open(stranger):
    from app import admin_lock
    for path in ["/api/recipes", "/api/tags", "/api/backup/info", "/api/email-status", "/api/https/summary",
                 "/api/admin/status"]:
        assert not admin_lock.is_locked_path(path), path
        assert stranger.get(path).status_code == 200, path
    # The inbox scan reads only the recipe inbox; left open on purpose.
    assert not admin_lock.is_locked_path("/api/email-settings/scan")


def test_open_summaries_reveal_no_settings(stranger, client):
    client.put("/api/email-settings", json={"imap_host": "imap.example.com", "smtp_host": "smtp.example.com",
                                            "username": "secret-user@example.com"})
    assert set(stranger.get("/api/email-status").json()) == {"enabled", "can_send"}
    assert set(stranger.get("/api/https/summary").json()) == {"state", "enabled", "url"}


def test_unlock_and_lock(stranger):
    st = stranger.get("/api/admin/status").json()
    assert st == {"password_set": True, "unlocked": False, "seconds_left": 0, "reset": False, "https_url": None}
    assert stranger.post("/api/admin/unlock", json={"password": "not it"}).status_code == 401
    r = stranger.post("/api/admin/unlock", json={"password": TEST_SETTINGS_PASSWORD})
    assert r.status_code == 200 and r.json()["unlocked"] is True
    assert 0 < r.json()["seconds_left"] <= 15 * 60
    cookie = r.headers["set-cookie"].lower()
    for attr in ["httponly", "samesite=strict", "path=/api", "max-age=900"]:
        assert attr in cookie, attr
    assert stranger.get("/api/email-settings").status_code == 200
    stranger.post("/api/admin/lock")
    assert stranger.get("/api/email-settings").status_code == 401


def test_a_second_password_cannot_be_created(stranger):
    r = stranger.post("/api/admin/password", json={"password": "someone-elses-password"})
    assert r.status_code == 409
    assert stranger.get("/api/email-settings").status_code == 401


def test_wrong_guesses_slow_down(stranger):
    from app import admin_lock
    codes = [stranger.post("/api/admin/unlock", json={"password": f"guess-{i}"}).status_code
             for i in range(admin_lock.FREE_TRIES + 1)]
    assert codes[:admin_lock.FREE_TRIES] == [401] * admin_lock.FREE_TRIES
    assert codes[-1] == 429
    # Even the right password waits until the pause is over.
    r = stranger.post("/api/admin/unlock", json={"password": TEST_SETTINGS_PASSWORD})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_password_file_is_a_salted_hash_and_not_in_backups(client):
    from app import admin_lock
    raw = open(admin_lock.PASSWORD_PATH, encoding="utf-8").read()
    assert TEST_SETTINGS_PASSWORD not in raw
    assert set(json.loads(raw)) == {"salt", "hash", "n", "r", "p"}
    z = zipfile.ZipFile(io.BytesIO(client.get("/api/backup/database.zip").content))
    assert not any("admin-password" in n for n in z.namelist())


# Module-level behaviour on a separate password file, so the shared
# client's session is left alone.
@pytest.fixture
def own_file(tmp_path, monkeypatch):
    from app import admin_lock
    monkeypatch.setattr(admin_lock, "PASSWORD_PATH", str(tmp_path / "admin-password.json"))
    monkeypatch.setattr(admin_lock, "MARKER_PATH", str(tmp_path / "admin-password.created"))
    saved = dict(admin_lock._sessions)
    yield admin_lock
    with admin_lock._lock:
        admin_lock._sessions.clear()
        admin_lock._sessions.update(saved)
        admin_lock._failures.clear()


def test_short_passwords_are_refused(own_file):
    with pytest.raises(own_file.PasswordError):
        own_file.create("short")
    assert not own_file.password_set()


def test_unlock_ends_after_15_minutes(own_file, monkeypatch):
    token = own_file.create("first-password")
    assert own_file.seconds_left(token) > 14 * 60
    real = own_file.time.time
    monkeypatch.setattr(own_file.time, "time", lambda: real() + 15 * 60 + 1)
    assert own_file.seconds_left(token) == 0


def test_deleting_the_file_locks_and_allows_a_new_password(own_file):
    import os
    token = own_file.create("first-password")
    os.remove(own_file.PASSWORD_PATH)
    assert own_file.seconds_left(token) == 0
    assert own_file.seconds_left(own_file.create("second-password")) > 0


def test_changing_the_password_locks_other_browsers(own_file):
    other = own_file.create("first-password")
    assert own_file.change("wrong", "new-password-1", "c") is None
    mine = own_file.change("first-password", "new-password-1", "c")
    assert own_file.seconds_left(other) == 0 and own_file.seconds_left(mine) > 0
    assert own_file.unlock("new-password-1", "c") and not own_file.unlock("first-password", "c")


# A reset (the password file deleted after a password existed) clears the
# saved email password and HTTPS when the new password is created. A first
# password -- including on an install upgraded from before the password --
# clears nothing.
@pytest.fixture
def credentials(own_file, client):
    """Saved email password and a working HTTPS setup, restored afterwards."""
    import os
    from datetime import datetime, timedelta, timezone
    from app import https_setup
    from app.database import SessionLocal
    from app import models
    from app.main import _get_email_settings

    db = SessionLocal()
    em = _get_email_settings(db)
    saved_email = (em.password_encrypted, em.enabled)
    em.password_encrypted, em.enabled = "encrypted-blob", True
    hs = https_setup._settings(db)
    saved_https = {k: getattr(hs, k) for k in ("enabled", "state", "domain", "lan_address", "a_record_value",
                                               "cert_expires_at")}
    hs.enabled, hs.state, hs.domain, hs.lan_address = True, "ready", "pantry.example.com", "192.168.1.20"
    hs.a_record_value, hs.cert_expires_at = "192.168.1.20", datetime.now(timezone.utc) + timedelta(days=60)
    db.commit()
    db.close()
    os.makedirs(https_setup.HTTPS_DIR, exist_ok=True)
    files = [https_setup.KEY_PATH, https_setup.CERT_PATH, https_setup.ACCOUNT_KEY_PATH, https_setup.REQUESTS_PATH]
    saved_files = {f: open(f, "rb").read() if os.path.exists(f) else None for f in files}
    for f in files:
        with open(f, "wb") as fh:
            fh.write(b"[]" if f == https_setup.REQUESTS_PATH else b"material")
    yield own_file
    db = SessionLocal()
    em = _get_email_settings(db)
    em.password_encrypted, em.enabled = saved_email
    hs = https_setup._settings(db)
    for k, v in saved_https.items():
        setattr(hs, k, v)
    db.commit()
    db.close()
    for f, data in saved_files.items():
        if data is None:
            if os.path.exists(f):
                os.remove(f)
        else:
            with open(f, "wb") as fh:
                fh.write(data)


def _state():
    import os
    from app import https_setup
    from app.database import SessionLocal
    from app.main import _get_email_settings
    db = SessionLocal()
    try:
        em = _get_email_settings(db)
        hs = https_setup._settings(db)
        return {
            "email_password": em.password_encrypted, "email_enabled": em.enabled,
            "https_enabled": hs.enabled, "cert_expires_at": hs.cert_expires_at,
            "domain": hs.domain, "a_record_value": hs.a_record_value,
            "files": {os.path.basename(f): os.path.exists(f) for f in
                      (https_setup.KEY_PATH, https_setup.CERT_PATH, https_setup.ACCOUNT_KEY_PATH, https_setup.REQUESTS_PATH)},
        }
    finally:
        db.close()


def test_first_password_on_an_upgraded_install_keeps_the_credentials(credentials, secure_stranger):
    assert not credentials.was_reset()
    r = secure_stranger.post("/api/admin/password", json={"password": "first-password"})
    assert r.status_code == 200 and r.json()["reset"] is False
    st = _state()
    assert st["email_password"] == "encrypted-blob" and st["email_enabled"] is True
    assert st["https_enabled"] is True and all(st["files"].values())


def test_new_password_after_a_reset_clears_email_and_https(credentials, secure_stranger):
    import os
    assert secure_stranger.post("/api/admin/password", json={"password": "first-password"}).status_code == 200
    os.remove(credentials.PASSWORD_PATH)            # the reset: done on the server, not in the app
    st = secure_stranger.get("/api/admin/status").json()
    assert st["reset"] is True and st["unlocked"] is False
    assert secure_stranger.get("/api/email-settings").json()["reset"] is True
    # A short password is refused before anything is cleared.
    assert secure_stranger.post("/api/admin/password", json={"password": "short"}).status_code == 400
    assert _state()["email_password"] == "encrypted-blob"

    r = secure_stranger.post("/api/admin/password", json={"password": "second-password"})
    assert r.status_code == 200
    st = _state()
    assert st["email_password"] is None and st["email_enabled"] is False
    assert st["https_enabled"] is False and st["cert_expires_at"] is None
    assert st["files"] == {"privkey.pem": False, "fullchain.pem": False, "account.key": False,
                           "requests.json": True}       # Let's Encrypt's weekly count is kept
    assert st["domain"] == "pantry.example.com" and st["a_record_value"] == "192.168.1.20"
    assert not credentials.was_reset() and secure_stranger.get("/api/admin/status").json()["unlocked"] is True


def test_changing_the_password_clears_nothing(credentials, secure_stranger):
    secure_stranger.post("/api/admin/password", json={"password": "first-password"})
    r = secure_stranger.post("/api/admin/password/change",
                      json={"current_password": "first-password", "new_password": "second-password"})
    assert r.status_code == 200
    st = _state()
    assert st["email_password"] == "encrypted-blob" and st["https_enabled"] is True and all(st["files"].values())


def test_cross_site_and_host_checks_come_before_the_lock(stranger):
    """A write without X-Requested-With, or a request for an unknown host,
    is refused by those checks first, so it learns nothing about the lock."""
    from app.main import app
    bare = TestClient(app)
    r = bare.post("/api/email-settings/test")
    assert r.status_code == 403 and "locked" not in r.json()
    r = stranger.get("/api/email-settings", headers={"Host": "evil.example.com"})
    assert r.status_code == 400 and "locked" not in r.json()


def test_a_password_is_not_kept_without_its_marker(own_file, monkeypatch, tmp_path):
    """Without the marker a later reset would clear nothing, so if it can't
    be written the password is undone and the error reported."""
    monkeypatch.setattr(own_file, "MARKER_PATH", str(tmp_path / "no-such-dir" / "admin-password.created"))
    with pytest.raises(OSError):
        own_file.create("first-password")
    assert not own_file.password_set()


def test_a_failed_save_after_a_reset_says_what_was_cleared(credentials, secure_stranger, monkeypatch):
    """Credentials are cleared before the new password is written, so a
    failed write must say they're gone and that trying again is enough."""
    import os
    secure_stranger.post("/api/admin/password", json={"password": "first-password"})
    os.remove(credentials.PASSWORD_PATH)

    def disk_full(password):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(credentials, "_write", disk_full)
    r = secure_stranger.post("/api/admin/password", json={"password": "second-password"})
    assert r.status_code == 500
    assert "already cleared; try again" in r.json()["detail"]
    assert credentials.was_reset()                 # still a reset: the retry clears (nothing left) and saves


def test_guesses_sent_at_once_still_stop_at_the_limit(own_file, monkeypatch):
    """Claude review round 4: 60 guesses at the same moment were all checked,
    because each one passed the limit before any failure was recorded."""
    import threading
    own_file.create("the-real-password")
    checked = []
    real_verify = own_file._verify
    monkeypatch.setattr(own_file, "_verify", lambda p: checked.append(p) or real_verify(p))
    results = []

    def guess(i):
        try:
            results.append(own_file.unlock(f"wrong-{i}", "10.0.0.9"))
        except own_file.TooManyTries:
            results.append("wait")
    threads = [threading.Thread(target=guess, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(checked) == own_file.FREE_TRIES
    assert results.count("wait") == 30 - own_file.FREE_TRIES


def test_changing_address_doesnt_buy_more_guesses(own_file):
    own_file.create("the-real-password")
    outcomes = []
    for i in range(own_file.FREE_TRIES_ALL + 3):
        try:
            outcomes.append(own_file.unlock("wrong", f"2001:db8::{i}"))   # a new address every time
        except own_file.TooManyTries:
            outcomes.append("wait")
    assert outcomes[:own_file.FREE_TRIES_ALL] == [None] * own_file.FREE_TRIES_ALL
    assert outcomes[own_file.FREE_TRIES_ALL:] == ["wait"] * 3


def test_once_https_is_on_the_password_needs_the_secure_address(credentials, stranger, secure_stranger, monkeypatch):
    """Claude review round 4: after HTTPS is set up, the password and the
    unlock cookie still crossed the network in plain text on port 8090."""
    from app import https_setup
    monkeypatch.setattr(https_setup, "_server", object())        # the listener is up
    assert secure_stranger.post("/api/admin/password", json={"password": "first-password"}).status_code == 200
    st = stranger.get("/api/admin/status").json()
    assert st["https_url"] == "https://pantry.example.com:8443"
    for method, path, body in [("POST", "/api/admin/unlock", {"password": "first-password"}),
                               ("POST", "/api/admin/password/change",
                                {"current_password": "first-password", "new_password": "other-password"}),
                               ("GET", "/api/email-settings", None), ("GET", "/api/logs", None)]:
        r = stranger.request(method, path, json=body)
        assert r.status_code == 403 and "secure address" in r.json()["detail"], (path, r.status_code)
    # An unlocked cookie from plain http:// doesn't help either.
    stranger.cookies.set("pantry_settings", secure_stranger.cookies.get("pantry_settings"), path="/api")
    assert stranger.get("/api/email-settings").status_code == 403
    # Over https:// all is as usual, and the cookie is marked Secure.
    assert secure_stranger.get("/api/admin/status").json()["https_url"] is None
    assert secure_stranger.get("/api/email-settings").status_code == 200
    r = secure_stranger.post("/api/admin/unlock", json={"password": "first-password"})
    assert "secure" in r.headers["set-cookie"].lower()
    # The rest of the app is unaffected.
    assert stranger.get("/api/recipes").status_code == 200


def test_the_server_itself_may_use_plain_http():
    from types import SimpleNamespace
    from app.main import _https_only_url
    from unittest.mock import patch
    on = {"enabled": True, "url": "https://pantry.example.com:8443", "listening": True}
    with patch("app.main.https_setup.status", return_value=on):
        for host, expected in [("127.0.0.1", None), ("::1", None), ("192.168.1.30", on["url"])]:
            req = SimpleNamespace(url=SimpleNamespace(scheme="http"), client=SimpleNamespace(host=host))
            assert _https_only_url(req) == expected, host


def test_https_that_isnt_listening_doesnt_lock_out_settings():
    """ChatGPT review of 0057: with HTTPS on but its listener down (port in
    use), the secure address doesn't answer, so plain http must still work."""
    from types import SimpleNamespace
    from unittest.mock import patch
    from app.main import _https_only_url
    req = SimpleNamespace(url=SimpleNamespace(scheme="http"), client=SimpleNamespace(host="192.168.1.30"))
    down = {"enabled": True, "url": "https://pantry.example.com:8443", "listening": False}
    with patch("app.main.https_setup.status", return_value=down):
        assert _https_only_url(req) is None


def test_https_from_a_proxy_also_closes_plain_http(stranger, secure_stranger, monkeypatch):
    """ChatGPT review of 0057: on the Traefik route the app's own HTTPS is off,
    so port 8090 still took the password in plain text."""
    monkeypatch.setenv("RECIPE_APP_HTTPS_URL", "https://pantry.example.com")
    r = stranger.post("/api/admin/unlock", json={"password": TEST_SETTINGS_PASSWORD})
    assert r.status_code == 403 and "https://pantry.example.com" in r.json()["detail"]
    assert stranger.get("/api/email-settings").status_code == 403
    assert stranger.get("/api/admin/status").json()["https_url"] == "https://pantry.example.com"
    # Through the proxy (https), as usual.
    assert secure_stranger.post("/api/admin/unlock", json={"password": TEST_SETTINGS_PASSWORD}).status_code == 200
    assert stranger.get("/api/recipes").status_code == 200


def test_a_reset_during_https_setup_clears_nothing(credentials, secure_stranger, monkeypatch):
    """ChatGPT review of 0057: the email password was cleared before the
    running HTTPS job made the request fail."""
    import os
    from app import https_setup
    secure_stranger.post("/api/admin/password", json={"password": "first-password"})
    os.remove(credentials.PASSWORD_PATH)
    monkeypatch.setitem(https_setup._progress, "running", True)
    r = secure_stranger.post("/api/admin/password", json={"password": "second-password"})
    assert r.status_code == 409
    assert _state()["email_password"] == "encrypted-blob" and credentials.was_reset()
