"""DeepSeek review and security audit of 0143: a failed first sweep stopped
the app starting; a restore interrupted between its renames left uploads/
empty (0144 put the old photos back: wrong, the database was already the
backup's; finished with the staged photos in 0145, and from 0146 also
when stopped after the database copy, before the swap); JSON-LD sections nested very deep skipped the whole JSON-LD tier;
a compressed JSON response would have been rewritten under its old
header; RECIPE_APP_ALLOWED_HOSTS=* turned the host check off silently;
a cPanel username or token with a line break or a colon went into the
Authorization header; a NAT64 address (64:ff9b::7f00:1) passed as public.
The backup sent twice after the password prompt is in tests_browser."""
import os
import socket
from unittest.mock import patch

import pytest

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


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


def test_an_interrupted_restore_is_finished(tmp_path):
    """DeepSeek review of 0144: the database is already the backup's when
    the folders are swapped, so the staged photos go into place, not the
    old ones (0144 moved the old ones back)."""
    from app.restore import recover_interrupted_restore
    live = tmp_path / "uploads"
    old = tmp_path / "uploads-before-restore-20261101-101010"
    staged = tmp_path / "uploads.restore-new"
    old.mkdir()
    (old / "img-old.jpg").write_bytes(b"x")
    staged.mkdir()
    (staged / "img-restored.jpg").write_bytes(b"y")
    live.mkdir()                                           # recreated empty at import
    assert recover_interrupted_restore(str(tmp_path), str(live)) == str(staged)
    assert (live / "img-restored.jpg").exists() and not staged.exists()
    assert (old / "img-old.jpg").exists()                  # left, named in the log


def test_after_recovery_the_restored_recipes_photo_resolves(client):
    """The same, through the app: a recipe as the restored database has it,
    its photo only in the staged folder, uploads/ empty as after a crash."""
    import shutil
    from app.database import DATA_DIR, UPLOADS_DIR
    from app.restore import recover_interrupted_restore, STAGED_SUFFIX
    from tests.conftest import TINY_PNG
    stored = client.post("/api/upload-image", headers=H, files={"file": ("p.png", TINY_PNG, "image/png")}).json()
    r = client.post("/api/recipes", headers=H, json={"title": "Restored with photo", "source_type": "manual",
                                                     "image_path": stored["stored_file"],
                                                     "ingredients": [{"raw_line": "1 egg"}], "steps": ["Boil."]}).json()
    photo = r["image_path"]
    live = UPLOADS_DIR.rstrip("/")
    before, staged = live + "-before-restore-20261101-101010", live + STAGED_SUFFIX
    try:
        shutil.rmtree(staged, ignore_errors=True)
        shutil.copytree(live, staged)                       # the backup's photos, unpacked
        os.remove(os.path.join(live, photo))                # not among the photos from before
        os.rename(live, before)                             # swap_dirs' first rename...
        os.makedirs(live)                                   # ...then the crash; recreated empty at import
        assert client.get(f"/uploads/{photo}").status_code == 404
        assert recover_interrupted_restore(DATA_DIR, UPLOADS_DIR) == staged
        got = client.get(f"/api/recipes/{r['id']}").json()["image_path"]
        assert client.get(f"/uploads/{got}").status_code == 200
    finally:
        if os.path.isdir(before):
            shutil.rmtree(before)
        if os.path.isdir(staged):
            shutil.rmtree(staged)
        client.delete(f"/api/recipes/{r['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{r['id']}", headers=H)


@pytest.mark.parametrize("setup", ["two left, uploads empty", "nothing left over", "old folder only"])
def test_otherwise_nothing_is_moved(tmp_path, setup):
    from app.restore import recover_interrupted_restore
    live = tmp_path / "uploads"
    live.mkdir()
    if setup != "two left, uploads empty":
        (live / "img-2.jpg").write_bytes(b"y")
    if setup != "nothing left over":
        (tmp_path / "uploads-before-restore-20261101-101010").mkdir()
    if setup == "two left, uploads empty":
        (tmp_path / "uploads-before-restore-20261101-111111").mkdir()
        (tmp_path / "uploads.restore-new").mkdir()
    assert recover_interrupted_restore(str(tmp_path), str(live)) is None
    assert live.is_dir()
    if setup == "two left, uploads empty":
        assert (tmp_path / "uploads.restore-new").is_dir()      # left for the owner


def _layout(tmp_path):
    live, staged = tmp_path / "uploads", tmp_path / "uploads.restore-new"
    live.mkdir()
    (live / "img-old.jpg").write_bytes(b"old")
    staged.mkdir()
    (staged / "img-restored.jpg").write_bytes(b"new")
    return live, staged


def test_stopped_after_the_database_copy_the_photos_follow(tmp_path):
    """DeepSeek review of 0145: stopped after the database was replaced but
    before the swap began, uploads/ still had the old photos and the staged
    ones were never moved. The marker says the database was replaced."""
    from app import restore
    live, staged = _layout(tmp_path)
    restore.mark_database_replaced(str(tmp_path))
    assert restore.recover_interrupted_restore(str(tmp_path), str(live)) == str(staged)
    assert (live / "img-restored.jpg").exists() and not staged.exists()
    old = [p for p in tmp_path.iterdir() if p.name.startswith("uploads-before-restore-")]
    assert len(old) == 1 and (old[0] / "img-old.jpg").exists()
    assert not (tmp_path / restore.MARKER).exists()


def test_stopped_before_the_database_copy_the_staged_copy_goes(tmp_path):
    """No marker: the database is still the old one, so are the photos;
    the staged copy belongs to nothing."""
    from app import restore
    live, staged = _layout(tmp_path)
    assert restore.recover_interrupted_restore(str(tmp_path), str(live)) is None
    assert (live / "img-old.jpg").exists() and not staged.exists()


def test_the_restore_marks_the_database_and_clears_the_mark(client, monkeypatch):
    """Through a real restore: the marker exists from the database copy to
    the swap, and not after."""
    from app import restore
    from app.database import DATA_DIR
    seen = {}
    real_swap = restore.swap_dirs

    def spy(new, live):
        seen["marked during swap"] = os.path.exists(os.path.join(DATA_DIR, restore.MARKER))
        return real_swap(new, live)
    monkeypatch.setattr(restore, "swap_dirs", spy)
    data = client.get("/api/backup/database.zip").content
    tok = client.post("/api/backup/restore/check", headers=H,
                      files={"file": ("b.zip", data, "application/zip")}).json()["token"]
    assert client.post("/api/backup/restore", headers=H, json={"token": tok}).status_code == 200
    assert seen == {"marked during swap": True}
    assert not os.path.exists(os.path.join(DATA_DIR, restore.MARKER))


def test_very_deep_sections_dont_lose_the_page():
    from app.ingestion.url_ingest import _flatten_instructions, MAX_INSTRUCTION_DEPTH
    node = {"@type": "HowToStep", "text": "Too deep."}
    for _ in range(5000):
        node = {"@type": "HowToSection", "name": "Part", "itemListElement": [node]}
    assert _flatten_instructions([{"@type": "HowToStep", "text": "Mix."}, node])[0] == "Mix."
    # Ordinary nesting is read whole.
    two = [{"@type": "HowToSection", "name": "Dough", "itemListElement": [
        {"@type": "HowToSection", "name": "Starter", "itemListElement": [{"@type": "HowToStep", "text": "Feed."}]}]}]
    assert _flatten_instructions(two) == ["Dough:", "Starter:", "Feed."]
    assert MAX_INSTRUCTION_DEPTH >= 10


def test_a_compressed_json_response_is_left_alone():
    import asyncio
    from starlette.requests import Request
    from starlette.responses import Response
    from app import main
    body = b"\x1f\x8b compressed"
    resp = Response(content=body, media_type="application/json", headers={"Content-Encoding": "gzip"})

    async def call_next(request):
        return resp
    req = Request({"type": "http", "method": "GET", "path": "/api/x", "headers": [(b"x-app-lang", b"fr")],
                   "query_string": b""})
    out = asyncio.run(main.interface_language(req, call_next))
    assert out is resp


def test_a_wildcard_host_list_is_warned_about():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "main.py"), encoding="utf-8").read()
    assert 'if "*" in ALLOWED_HOSTS:' in src and "DNS rebinding" in src
    readme = open(os.path.join(os.path.dirname(__file__), "..", "..", "README.md"), encoding="utf-8").read()
    assert "RECIPE_APP_ALLOWED_HOSTS=*" in readme


@pytest.mark.parametrize("user,token,ok", [
    ("marc", "ABCDEF0123456789ABCDEF0123456789", True), ("marc.d", "tok-en_1", True),
    ("marc\n", "TOKEN", False), ("marc", "TOKEN\r\nX-Evil: 1", False), ("ma:rc", "TOKEN", False),
    ("marc", "TO KEN", False), ("", "TOKEN", False), ("marc", "AbC+/x9=", True),
])
def test_cpanel_values_are_checked_before_the_header(user, token, ok):
    from app import https_setup
    if ok:
        assert https_setup.CpanelDns("https://cp.example.test:2083", user, token).auth == f"cpanel {user}:{token}"
    else:
        with pytest.raises(https_setup.HttpsError):
            https_setup.CpanelDns("https://cp.example.test:2083", user, token)


def test_settings_refuse_a_bad_cpanel_token(client):
    from app import admin_lock
    with patch.object(admin_lock, "is_locked_path", return_value=False):
        r = client.put("/api/https/credentials", headers=H,
                       json={"username": "marc", "base_url": "https://cp.example.test:2083",
                             "domain": "pantry.example.test", "token": "TOKEN\nX"})
    assert r.status_code == 400 and "spaces, line breaks or colons" in r.json()["detail"]


@pytest.mark.parametrize("address,blocked", [("64:ff9b::7f00:1", True), ("64:ff9b::c0a8:101", True),
                                              ("64:ff9b::5db8:d822", False)])
def test_nat64_addresses_are_judged_by_the_ipv4_inside(address, blocked):
    from app.ingestion.url_ingest import validate_public_url, UrlValidationError
    answer = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))]
    with patch("app.ingestion.url_ingest.socket.getaddrinfo", return_value=answer):
        if blocked:
            with pytest.raises(UrlValidationError):
                validate_public_url("http://nat64.example.com/")
        else:
            assert validate_public_url("http://nat64.example.com/") == "64:ff9b::5db8:d822"


def test_no_page_sets_html_from_data():
    """Audit of 0143: a future innerHTML of recipe text would run (the CSP
    allows <img onerror>). innerHTML is only ever emptied, and el()'s
    trustedStaticHtml only gets the fixed SVG constants."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for f in [root / "frontend/js/app.js", *sorted((root / "frontend-picker").glob("*.js"))]:
        js = f.read_text(encoding="utf-8")
        assert "insertAdjacentHTML" not in js and "outerHTML" not in js and "document.write" not in js, f.name
        for m in re.finditer(r"\.innerHTML\s*=\s*([^;\n]+)", js):
            assert m.group(1).strip() in ('""', "v"), (f.name, m.group(0))
        for m in re.finditer(r"trustedStaticHtml:\s*([^,}\n]+)", js):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*_SVG", m.group(1).strip()), (f.name, m.group(0))
        # The one `= v` is el()'s trustedStaticHtml branch.
        assert len(re.findall(r"\.innerHTML\s*=\s*v\b", js)) <= 1
