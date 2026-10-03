"""Fixes from the Qwen review (Oct 3).

app.* is imported inside tests only; see the note in test_backup.py.
"""
import io
import json
import os
import re
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from .test_friday_batch import _recipe, mail  # noqa: F401  (mail is a fixture)

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")


@pytest.fixture(autouse=True)
def _isolated_data_dir(data_dir):
    pass


def test_failed_email_sends_do_not_use_up_the_hour(client, mail):
    from app import main
    rid = _recipe(client, "Quota Failure Test")
    with patch("app.main.email_client.connect_smtp", side_effect=Exception("smtp down")):
        for _ in range(main.MAX_RECIPE_EMAILS_PER_HOUR + 2):
            assert client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]}).status_code == 502
    with patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email_with_attachment"):
        assert client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]}).status_code == 200


def test_null_clears_the_cook_time_and_absent_leaves_it(client):
    rid = _recipe(client, "Cook Time Null")
    assert client.patch(f"/api/recipes/{rid}/rating", json={"actual_cook_time": "00:01:30"}).status_code == 200
    client.patch(f"/api/recipes/{rid}/rating", json={"favorite": True})
    assert client.get(f"/api/recipes/{rid}").json()["actual_cook_time"] == "00:01:30"
    client.patch(f"/api/recipes/{rid}/rating", json={"actual_cook_time": None})
    assert client.get(f"/api/recipes/{rid}").json()["actual_cook_time"] is None


def test_image_bytes_over_the_cap_are_refused(tmp_path):
    from PIL import Image
    from app.file_validation import validate_and_save_image_bytes
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), "red").save(buf, format="PNG")
    data = buf.getvalue()
    assert validate_and_save_image_bytes(data, str(tmp_path), "img-a", max_bytes=len(data) - 1) is None
    assert list(tmp_path.iterdir()) == []
    assert validate_and_save_image_bytes(data, str(tmp_path), "img-b") is not None


def test_email_times_are_sent_with_their_time_zone(client, mail):
    from app import main
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        s = main._get_email_settings(db)
        s.last_scan_at = main._utc_now()
        db.commit()
    finally:
        db.close()
    got = client.get("/api/email-settings").json()["last_scan_at"]
    assert got.endswith("+00:00"), got


def test_a_photo_removed_during_a_backup_is_listed_not_fatal(client, data_dir):
    from PIL import Image
    from app import backup, models
    from app.database import UPLOADS_DIR, SessionLocal

    name = "img-00000000-0000-4000-8000-0000000000aa.png"
    Image.new("RGB", (10, 10)).save(os.path.join(UPLOADS_DIR, name))
    db = SessionLocal()
    r = models.Recipe(title="Backup Race", source_type="manual", image_path=name)
    db.add(r); db.commit(); rid = r.id; db.close()

    real_write = zipfile.ZipFile.write

    def write(self, filename, arcname=None, *a, **k):
        if str(filename).endswith(name):
            os.remove(filename)          # gone between the check and the copy
        return real_write(self, filename, arcname, *a, **k)

    try:
        with patch.object(zipfile.ZipFile, "write", write):
            resp = client.get("/api/backup/database.zip")
        assert resp.status_code == 200
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        manifest = json.loads(z.read("manifest.json"))
        assert name in manifest["missing_uploads"]
        assert f"uploads/{name}" not in z.namelist()
    finally:
        client.post("/api/recipes/batch-delete", json={"ids": [rid]})


def test_service_worker_never_caches_logs_or_https_status():
    sw = open(os.path.join(FRONTEND, "service-worker.js"), encoding="utf-8").read()
    block = re.search(r"const NEVER_CACHE = \[(.*?)\];", sw, re.S).group(1)
    assert r"/^\/api\/logs/" in block and r"/^\/api\/https/" in block


def test_app_reads_storage_only_through_guarded_calls():
    """localStorage throws when storage is blocked; one unguarded read at
    startup stops the whole app."""
    js = open(os.path.join(FRONTEND, "js", "app.js"), encoding="utf-8").read()
    for m in re.finditer(r"localStorage\.(?:get|set)Item", js):
        line_start = js.rfind("\n", 0, m.start()) + 1
        context = js[max(0, line_start - 200):js.find("\n", m.end())]
        assert "try" in context, js[line_start:js.find("\n", m.end())]
