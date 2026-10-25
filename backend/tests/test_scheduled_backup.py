"""Settings -> Backup & export -> Automatic backups (app/scheduled_backup.py)."""
import os
import zipfile
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.fixture
def folder(tmp_path, monkeypatch):
    from app import scheduled_backup
    monkeypatch.setattr(scheduled_backup, "BACKUP_DIR", str(tmp_path / "backups"))
    return tmp_path / "backups"


def test_due():
    from app.scheduled_backup import due
    tz = timezone(timedelta(hours=-4))
    at3 = datetime(2026, 10, 18, 3, 5, tzinfo=tz)
    assert not due(False, 3, None, at3)
    assert due(True, 3, None, at3)                                   # never made: now
    assert due(True, 3, at3 - timedelta(days=1), at3)                # the set hour, not yet today
    assert not due(True, 3, at3 - timedelta(minutes=4), at3)         # made at 3:01: already today
    assert not due(True, 3, at3 - timedelta(hours=20), at3.replace(hour=14))   # not the hour
    assert due(True, 3, at3 - timedelta(hours=40), at3.replace(hour=14))       # missed: catch up
    naive_utc = (at3 - timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    assert due(True, 3, naive_utc, at3)                              # as SQLite gives it back


def test_schedule_backup_now_and_pruning(client, folder):
    st = client.get("/api/backups/status").json()
    assert st["enabled"] is False and st["backups"] == [] and st["folder"] == str(folder)
    r = client.put("/api/backups/schedule", headers=H, json={"enabled": True, "hour": 4, "keep": 2})
    assert r.status_code == 200 and (r.json()["enabled"], r.json()["hour"], r.json()["keep"]) == (True, 4, 2)
    made = []
    for _ in range(3):
        st = client.post("/api/backups/run", headers=H).json()
        made.append(st["backups"][0]["name"])
        assert st["last_ok"] is True and st["last_message"] == made[-1]
        import time
        time.sleep(1.05)          # names are by the second
    names = [b["name"] for b in client.get("/api/backups/status").json()["backups"]]
    assert names == [made[2], made[1]]                               # newest two kept
    with zipfile.ZipFile(folder / made[2]) as zf:
        assert "recipes.db" in zf.namelist() and "manifest.json" in zf.namelist()
    # Other files in the folder are never touched.
    (folder / "my-notes.txt").write_text("keep me")
    (folder / "open-the-pantry-backup-old.zip").write_text("not ours by name")
    client.put("/api/backups/schedule", headers=H, json={"enabled": True, "hour": 4, "keep": 1})
    assert sorted(os.listdir(folder)) == sorted([made[2], "my-notes.txt", "open-the-pantry-backup-old.zip"])
    client.put("/api/backups/schedule", headers=H, json={"enabled": False, "hour": 3, "keep": 7})
    assert client.put("/api/backups/schedule", headers=H, json={"enabled": True, "hour": 24, "keep": 7}).status_code == 422


def test_a_failed_backup_is_recorded_and_says_why(client, folder, monkeypatch):
    from app import scheduled_backup
    monkeypatch.setattr(scheduled_backup, "MIN_FREE_BYTES", 10 ** 18)
    r = client.post("/api/backups/run", headers=H)
    assert r.status_code == 500 and "Not enough free space" in r.json()["detail"]
    st = client.get("/api/backups/status").json()
    assert st["last_ok"] is False and "Not enough free space" in st["last_message"]
    fr = client.get("/api/backups/status", headers={"X-App-Lang": "fr"}).json()
    assert fr["last_message"].startswith("Pas assez d'espace libre")
    assert not [n for n in os.listdir(folder) if n.endswith(".partial")]


def test_a_half_written_backup_never_gets_a_real_name(client, folder, monkeypatch):
    from app import main
    def broken(dest):
        open(dest, "wb").write(b"half")
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(main, "build_database_backup", broken)
    assert client.post("/api/backups/run", headers=H).status_code == 500
    assert os.listdir(folder) == []


def test_changing_the_schedule_needs_the_password(client, folder):
    from app.main import app
    stranger = TestClient(app)
    assert stranger.get("/api/backups/status").status_code == 200
    for method, path, body in (("put", "/api/backups/schedule", {"enabled": False, "hour": 3, "keep": 7}),
                               ("post", "/api/backups/run", None)):
        r = getattr(stranger, method)(path, headers=H, **({"json": body} if body else {}))
        assert r.status_code == 401 and r.json()["locked"], path


def test_not_during_a_restore(client, folder):
    from app import main, scheduled_backup
    main._restoring.set()
    try:
        with pytest.raises(scheduled_backup.BackupError):
            main.run_scheduled_backup()
    finally:
        main._restoring.clear()
