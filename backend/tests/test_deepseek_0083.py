"""DeepSeek review of 0083.

- The tmp/uploads sweeps ran on the event loop. A delete in uploads/ waits
  on backup.UPLOADS_LOCK, which a full backup holds for its whole archive
  build, so a sweep that met a backup froze every request on both ports
  until the backup ended. Both sweeps now run in a worker thread; a failed
  periodic sweep is logged instead of ending the loop.
- The picker port shares the main port's per-address request limit (one
  rate_limiter); the README now says so.
"""
import asyncio
import os
import threading
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


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


def test_readme_says_the_picker_shares_the_request_limit():
    for name, phrase in (("README.md", "same per-address request limit"),
                         ("README.fr.md", "même limite de requêtes par adresse")):
        assert phrase in (ROOT / name).read_text(encoding="utf-8"), name
