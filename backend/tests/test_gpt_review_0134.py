"""GPT review of 0134 (2026-10-30): "moules de bouchot", "moules en sauce"
still read as a baking tin; run_setup checked that someone held the HTTPS
job lock, not that its caller did; the hour repeated when clocks go back
made a second scheduled backup. GPT review of 0135: progress left
shown as running when the setup failed before its own clean-up."""
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.mark.parametrize("title", ["Moules de bouchot", "Moules de corde", "Moules en sauce", "Moules de Bretagne",
                                   "Moules à la marinière", "Moules à la crème", "Moules à l'ail", "Moules au beurre",
                                   "Moules au curry", "Moules frites"])
def test_mussels_are_fish(title):
    from app.ingestion.tagger import suggest_tags
    names = {t[0] for t in suggest_tags(title, [])}
    assert "Fish" in names and "Vegetarian" not in names, (title, names)


@pytest.mark.parametrize("text", ["moule à gâteau", "moule à tarte", "moules à muffins", "moule en silicone",
                                  "moule de 23 cm", "moule à fond amovible", "moule à charnière", "moule à manqué",
                                  "moule beurré", "un moule rond", "moule à cake"])
def test_baking_tins(text):
    from app.ingestion.tagger import _BAKING_TIN, _fold
    assert _BAKING_TIN.search(_fold(text)), text


def test_a_tin_in_the_steps_is_not_fish():
    from app.ingestion.tagger import suggest_tags
    names = {t[0] for t in suggest_tags("Gâteau au chocolat", ["200 g de chocolat", "3 oeufs"],
                                        "Beurrer un moule à manqué de 22 cm. Verser dans le moule à gâteau.")}
    assert "Fish" not in names


def test_run_setup_takes_the_lock_itself(monkeypatch):
    from app import https_setup
    ran = []
    monkeypatch.setattr(https_setup, "_setup", lambda *a: ran.append(https_setup._job_lock.locked()))
    assert https_setup.run_setup(None, None, {}) is True
    assert ran == [True] and not https_setup._job_lock.locked()
    # Held elsewhere (reserved(), a running job): not run, and not released.
    with https_setup.reserved():
        assert https_setup.run_setup(None, None, {}) is False
        assert https_setup._job_lock.locked()
    assert ran == [True] and not https_setup._job_lock.locked()


def test_start_job_releases_after_its_thread(monkeypatch):
    from app import https_setup
    done = threading.Event()
    monkeypatch.setattr(https_setup, "_setup", lambda *a: done.set())
    assert https_setup.start_job(None, None, {})
    assert done.wait(5)
    for _ in range(100):
        if not https_setup._job_lock.locked():
            break
        threading.Event().wait(0.01)
    assert not https_setup._job_lock.locked()


def test_one_backup_when_clocks_go_back():
    from app.scheduled_backup import due
    tz = ZoneInfo("America/Toronto")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=tz, fold=0)         # EDT, 05:30 UTC
    second = datetime(2026, 11, 1, 1, 20, tzinfo=tz, fold=1)        # EST, 06:20 UTC
    assert second.astimezone(timezone.utc) > first.astimezone(timezone.utc)
    made = first.astimezone(timezone.utc).replace(tzinfo=None)      # as SQLite gives it back
    assert not due(True, 1, made, second)
    # The next day at 1:00, as usual.
    assert due(True, 1, made, datetime(2026, 11, 2, 1, 5, tzinfo=tz))
    # Spring forward: March 8 has no 2:00, so a 2:00 backup is made by the
    # 36-hour catch-up that afternoon (unchanged; noted in the change log).
    made = datetime(2026, 3, 7, 2, 5, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    assert not due(True, 2, made, datetime(2026, 3, 8, 15, 0, tzinfo=tz))
    assert due(True, 2, made, datetime(2026, 3, 8, 15, 10, tzinfo=tz))


@pytest.mark.parametrize("via", ["run_setup", "start_job"])
def test_progress_cleared_when_setup_fails_early(monkeypatch, via):
    """GPT review of 0135: _setup failing before its own clean-up (no
    database session) left the progress shown as running."""
    from app import https_setup
    done = threading.Event()

    def boom(*a):
        done.set()
        raise RuntimeError("no database")
    monkeypatch.setattr(https_setup, "SessionLocal", boom)
    if via == "run_setup":
        with pytest.raises(RuntimeError):
            https_setup.run_setup(None, None, {})
    else:
        assert https_setup.start_job(None, None, {})
        assert done.wait(5)
    for _ in range(100):
        if not https_setup._job_lock.locked():
            break
        threading.Event().wait(0.01)
    assert not https_setup._job_lock.locked() and https_setup._progress["running"] is False


def test_dnspython_has_the_name_decompression_fix():
    """CVE-2026-114605 (fixed in 2.9.0): a crafted DNS reply could take
    excessive CPU. https_setup resolves the domain's DNS records with it.
    The CI dependency audit failed on 2.8.0 (2026-10-30)."""
    import dns.version
    from pathlib import Path
    assert (dns.version.MAJOR, dns.version.MINOR) >= (2, 9)
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "\ndnspython==2.9.1\n" in req
