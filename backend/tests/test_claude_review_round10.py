"""Claude review round 10 (0088-0098), and the two parts it left
unreviewed, looked at here.

1. Any restore check deleted every backup waiting to be restored: a second
   check (another tab, two at once) made the first's Replace fail or its
   check return a 500. Only the caller's own files, and pending ones over
   30 minutes old, are removed now.
2. Checking a full backup (unpacking and integrity-checking a database of
   up to 4 GB) needed no password. It does now; import files are still
   checked without one, and without it uploads stop at the import's 1 GB.
3. Imports: each entry was capped, but the same 25 MB photo named by 5,000
   recipes, or 5,000 small gzip bombs, came to over 100 GB of unpacking,
   with every photo held in memory before saving. Photos are now read as
   each recipe is saved, and one import's unpacking is capped in total.
4. Images: Pillow's default refused only above 179 MP; the app now refuses
   above 80 MP everywhere.
"""
import base64
import gzip
import io
import json
import os
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _check(c, data):
    return c.post("/api/backup/restore/check", headers=H, files={"file": ("b.zip", data, "application/zip")})


def test_a_second_check_leaves_the_first_waiting(client):
    from app import main
    backup = client.get("/api/backup/database.zip").content
    first = _check(client, backup).json()["token"]
    second = _check(client, backup).json()["token"]
    for tok in (first, second):
        assert all(os.path.isfile(p) for p in main._pending_restore_paths(tok))
    # A stale one (over 30 minutes) goes on the next check.
    old = time.time() - main.PENDING_RESTORE_SECONDS - 60
    for p in main._pending_restore_paths(first):
        os.utime(p, (old, old))
    _check(client, backup)
    assert not any(os.path.isfile(p) for p in main._pending_restore_paths(first))
    assert all(os.path.isfile(p) for p in main._pending_restore_paths(second))
    main._clear_pending_restores(None)


def test_a_failed_check_removes_only_its_own_files(client):
    from app import main
    backup = client.get("/api/backup/database.zip").content
    waiting = _check(client, backup).json()["token"]
    assert _check(client, _zip({"recipes.db": b"not a database"})).status_code == 422
    assert all(os.path.isfile(p) for p in main._pending_restore_paths(waiting))
    main._clear_pending_restores(None)


def test_without_the_password_uploads_stop_at_the_import_limit(client, monkeypatch):
    from app import main
    stranger = TestClient(main.app)
    monkeypatch.setattr(main, "MAX_IMPORT_BYTES", 1000)
    r = _check(stranger, b"PK" + b"\0" * 5000)
    assert r.status_code == 401 and r.json()["locked"]           # only a backup could be that big
    assert _check(client, b"PK" + b"\0" * 5000).status_code == 422  # with it: checked (and not a zip)
    assert not [n for n in os.listdir(main.BACKUP_TMP_DIR) if n.startswith("restore-")]


def _zip(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return out.getvalue()


def test_one_photo_named_by_many_recipes_is_read_within_the_budget(tmp_path, monkeypatch):
    from app import interchange
    big = TINY_PNG + b"\0" * 400_000
    recipes = [{"@type": "Recipe", "name": f"R{i}", "image": "images/p.png",
                "recipeIngredient": ["1 egg"], "recipeInstructions": ["Boil."]} for i in range(30)]
    path = tmp_path / "many.zip"
    path.write_bytes(_zip({"recipes/all.json": json.dumps(recipes), "images/p.png": big}))
    monkeypatch.setattr(interchange, "UNPACK_BUDGET", 5 * len(big))
    with interchange.opened(str(path), "many.zip") as (found, problems):
        assert len(found) == 30 and all(r.image_bytes is None for r in found)   # nothing read yet
        photos = [r.photo() for r in found]
    assert 1 <= sum(1 for p in photos if p) < 5
    assert any("of unpacking" in p["error"] for p in problems)


def test_paprika_gzip_bombs_stop_at_the_budget(tmp_path, monkeypatch):
    from app import interchange
    bomb = gzip.compress(json.dumps({"name": "X", "ingredients": "1 egg", "directions": "Boil.",
                                     "notes": "a" * 2_000_000}).encode())
    assert len(bomb) < 20_000
    path = tmp_path / "bombs.paprikarecipes"
    path.write_bytes(_zip({f"r{i}.paprikarecipe": bomb for i in range(50)}))
    monkeypatch.setattr(interchange, "UNPACK_BUDGET", 10 * 2_000_000)
    with interchange.opened(str(path), "bombs.paprikarecipes") as (found, problems):
        assert len(found) < 12 and any("of unpacking" in p["error"] for p in problems)


def test_paprika_photos_are_read_when_saved(client):
    data = _zip({"p.paprikarecipe": gzip.compress(json.dumps({
        "name": "R10 Paprika photo", "ingredients": "1 egg", "directions": "Boil.",
        "photo_data": base64.b64encode(TINY_PNG).decode()}).encode())})
    body = client.post("/api/import/recipes", headers=H,
                       files={"file": ("x.paprikarecipes", data, "application/zip")}).json()
    rid = body["succeeded"][0]["recipe_id"]
    try:
        assert client.get(f"/uploads/{client.get(f'/api/recipes/{rid}').json()['image_path']}").status_code == 200
    finally:
        client.delete(f"/api/recipes/{rid}", headers=H)


def test_huge_images_are_refused(tmp_path):
    from PIL import Image
    from app import file_validation
    assert Image.MAX_IMAGE_PIXELS * 2 == file_validation.MAX_DECODED_PIXELS   # refused above 50 MP from 0106
    big = tmp_path / "big.png"
    Image.new("1", (10_000, 9_000)).save(big)          # 90 MP, a few KB on disk
    assert os.path.getsize(big) < 100_000
    assert file_validation.validate_and_save_image_bytes(big.read_bytes(), str(tmp_path), "img-x") is None
