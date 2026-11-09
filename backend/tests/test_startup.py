"""Startup and background jobs: the app starting despite failures, the
periodic sweeps, extra listeners, and the version fingerprint."""
import asyncio
import logging
import os
import threading
import time
import uuid

import pytest

UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


def _snapshot():
    out = {}
    for name in UVICORN_LOGGERS:
        lg = logging.getLogger(name)
        out[name] = (lg.level, list(lg.handlers), lg.propagate, lg.disabled)
    return out


@pytest.fixture
def main_port_logging():
    """uvicorn's loggers as the main server sets them up (`uvicorn app.main:app`
    at the default info level), restored afterwards."""
    import uvicorn
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers),
                 logging.getLogger(n).propagate, logging.getLogger(n).disabled) for n in UVICORN_LOGGERS}
    uvicorn.Config("app.main:app", log_level="info")      # what the main server's Config does
    assert logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)
    yield
    for n, (level, handlers, propagate, disabled) in saved.items():
        lg = logging.getLogger(n)
        lg.setLevel(level)
        lg.handlers = handlers
        lg.propagate = propagate
        lg.disabled = disabled


@pytest.mark.parametrize("module", ["picker", "https_setup"])
def test_extra_listener_leaves_the_main_ports_logging_alone(main_port_logging, module):
    import importlib

    from fastapi import FastAPI
    mod = importlib.import_module(f"app.{module}")
    before = _snapshot()
    mod._make_server(FastAPI())
    assert _snapshot() == before
    assert logging.getLogger("uvicorn.access").isEnabledFor(logging.INFO)
    assert logging.getLogger("uvicorn.access").handlers      # access_log=False would empty them


def test_a_sweep_during_a_backup_does_not_freeze_the_event_loop(client):
    from app import main
    from app.backup import UPLOADS_LOCK
    from app.database import UPLOADS_DIR

    # An orphan old enough to delete: the sweep will wait on the lock for it.
    orphan = os.path.join(UPLOADS_DIR, f"img-{uuid.uuid4().hex}.jpg")
    with open(orphan, "wb") as f:
        f.write(b"x")
    old = time.time() - main.TMP_FILE_MAX_AGE_SECONDS - 60
    os.utime(orphan, (old, old))

    held, release = threading.Event(), threading.Event()

    def backup():                 # a backup holding the lock, in its own thread
        with UPLOADS_LOCK:
            held.set()
            release.wait(10)

    t = threading.Thread(target=backup)
    t.start()
    held.wait(5)

    async def go():
        sweep = asyncio.create_task(main._run_sweeps())
        ticks = 0
        for _ in range(10):       # 0.5 s of "other requests"
            await asyncio.sleep(0.05)
            ticks += 1
        still_waiting = not sweep.done()
        release.set()             # the backup finishes
        await asyncio.wait_for(sweep, 10)
        return ticks, still_waiting

    try:
        ticks, still_waiting = asyncio.run(go())
    finally:
        release.set()
        t.join(5)
    assert ticks == 10            # the loop kept running while the sweep waited
    assert still_waiting          # the sweep really was blocked on the backup
    assert not os.path.exists(orphan)   # and finished once the lock was free


def test_a_failed_periodic_sweep_is_logged_and_the_loop_goes_on(monkeypatch):
    from app import main
    calls, logged = [], []
    # The app's logger doesn't propagate (logging_setup), so pytest's log
    # capture can't see it; record the call itself.
    monkeypatch.setattr(main.log, "exception", lambda msg, *a, **k: logged.append(msg))

    def boom():
        calls.append(1)
        raise RuntimeError("disk gone")

    monkeypatch.setattr(main, "TMP_SWEEP_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(main, "_sweep_stale_tmp_files", boom)
    monkeypatch.setattr(main, "_sweep_orphaned_upload_files", lambda: None)

    async def go():
        task = asyncio.create_task(main._periodic_tmp_sweep())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                break
        task.cancel()

    asyncio.run(go())
    assert len(calls) >= 2        # it went round again after the failure
    assert "Periodic tmp/uploads sweep failed" in logged


def test_a_failed_first_sweep_doesnt_stop_the_app_starting(client, monkeypatch):
    import sqlalchemy.exc
    from fastapi.testclient import TestClient

    from app import main

    async def locked():
        raise sqlalchemy.exc.OperationalError("DELETE", {}, Exception("database is locked"))
    monkeypatch.setattr(main, "_run_sweeps", locked)
    logged = []
    monkeypatch.setattr(main.log, "exception", lambda msg, *a, **k: logged.append(msg))
    with TestClient(main.app) as c:                       # runs the start-up
        assert c.get("/healthz").status_code == 200
    assert any("Start-up sweep failed" in m for m in logged)


def test_version_endpoint_reports_a_fingerprint(client):
    r = client.get("/api/version")
    assert r.status_code == 200
    assert isinstance(r.json()["frontend"], str) and r.json()["frontend"]
