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
    assert st == {"password_set": True, "unlocked": False, "seconds_left": 0}
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
