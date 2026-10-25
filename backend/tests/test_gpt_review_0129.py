"""GPT review of 0129: a backup failing after the day's success wasn't
retried; a same-length edit kept a recipe's old language guess; a merge
photo copy failing part way left a partial file."""
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_a_failure_after_the_days_success_is_retried():
    from app.scheduled_backup import due
    tz = timezone(timedelta(hours=-4))
    at = lambda h, m=0: datetime(2026, 10, 18, h, m, tzinfo=tz)   # noqa: E731
    # Scheduled backup worked at 3:00; Back up now failed at 4:00.
    assert not due(True, 3, at(3), at(4, 30), failed_at=at(4))     # within the hour
    for h in (5, 6, 7):
        assert due(True, 3, at(3), at(h, 5), failed_at=at(4))      # was False all day
    # A success after the failure ends the retries.
    assert not due(True, 3, at(5), at(6, 5), failed_at=at(4))


def test_a_same_length_edit_is_guessed_again(client):
    from app import picker
    from app.database import SessionLocal
    en = {"ingredients": [{"raw_line": "1 cup of milk"}], "steps": ["Add the milk and stir."]}
    r = client.post("/api/recipes", headers=H, json={"title": f"Lait {uuid.uuid4().hex[:4]}",
                                                     "source_type": "manual", **en}).json()
    try:
        with SessionLocal() as db:
            assert picker._recipe_languages(db)[r["id"]] == "en"
        # French text of exactly the same length.
        old = " ".join(["1 cup of milk", "Add the milk and stir."])
        fr_ing, fr_step = "1 tasse de lait", "Ajouter le lait et mélanger."
        pad = len(old) - len(" ".join([fr_ing, fr_step]))
        fr_step = fr_step[:len(fr_step) + pad] if pad < 0 else fr_step + "." * pad
        assert len(" ".join([fr_ing, fr_step])) == len(old)
        res = client.put(f"/api/recipes/{r['id']}", headers=H, json={
            "title": r["title"], "source_type": "manual", "ingredients": [{"raw_line": fr_ing}], "steps": [fr_step]})
        assert res.status_code == 200, res.text
        with SessionLocal() as db:
            assert picker._recipe_languages(db)[r["id"]] == "fr"
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)


def test_a_failed_merge_photo_copy_leaves_nothing(client, monkeypatch):
    from app import main
    photo = client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).json()["stored_file"]
    a = client.post("/api/recipes", headers=H, json={"title": "Keep", "source_type": "manual"}).json()
    b = client.post("/api/recipes", headers=H, json={"title": "Other", "source_type": "manual",
                                                     "image_path": photo}).json()
    try:
        before = set(os.listdir(main.UPLOADS_DIR))

        def half_copy(src, dst):
            with open(dst, "wb") as f:
                f.write(b"partial")
            raise OSError(28, "No space left on device")
        monkeypatch.setattr(main.shutil, "copyfile", half_copy)
        r = client.post("/api/recipes/merge", headers=H, json={"keep_id": a["id"], "other_id": b["id"],
                                                              "take": {"photo": "other"}})
        assert r.status_code == 500 and "Nothing was changed" in r.json()["detail"]
        assert set(os.listdir(main.UPLOADS_DIR)) == before
        assert client.get(f"/api/recipes/{b['id']}").status_code == 200      # not merged
    finally:
        for rid in (a["id"], b["id"]):
            client.delete(f"/api/recipes/{rid}", headers=H)
            client.delete(f"/api/recently-deleted/{rid}", headers=H)
