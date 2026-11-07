"""Review of 0153-0157: index.csv's guard missed a no-break space before a
formula sign; after a restore start-up couldn't finish (no marker), the
photo sweep would delete the old library's photos left in uploads/."""
import asyncio
import os
import time
import uuid

import pytest


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


# Written as escapes: they look like plain spaces (DeepSeek review of 0159).
@pytest.mark.parametrize("title", ["\u00a0=1+1", "\u202f+cmd", "\u3000-1", " \u00a0@SUM(1)", "\u2007=1"])
def test_no_break_spaces_before_a_formula(title):
    from app import backup
    assert backup._csv_safe(title).startswith("'")


def test_no_photo_sweep_while_staged_photos_wait(client):
    from app import main, restore as restore_mod
    from app.database import UPLOADS_DIR
    staged = UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX
    path = os.path.join(UPLOADS_DIR, f"old-{uuid.uuid4().hex}.jpg")
    open(path, "wb").write(b"x")
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    os.makedirs(staged)
    try:
        asyncio.run(main._run_sweeps())
        assert os.path.exists(path)
    finally:
        os.rmdir(staged)
    asyncio.run(main._run_sweeps())
    assert not os.path.exists(path)
