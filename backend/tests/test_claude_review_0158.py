"""Claude review of 0158 (claude/reviews/review-0158.md): manual restore
left the database's WAL (and root-owned files); zip directories parsed
before the entry limit; search across text and tags; unbounded tags;
staged photos after a failed restore; URLs in the log; auto-saved recipes
over RecipeCreate's limits; notes and dates on import; quadratic import
checks; nested emails; Unicode hosts; a damaged password file; nits."""
import email
import io
import json
import os
import re
import subprocess
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ._zip_directory import make as make_directory_zip
from .test_restore import H, _check, _recipe

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


# ---------------------------------------------------------------- H1, M4: restoring by hand

def test_manual_restore_deletes_the_wal_first():
    from app.backup import RESTORE_INSTRUCTIONS, RESTORE_INSTRUCTIONS_FR
    rm = "rm -f ./data/recipes.db-wal ./data/recipes.db-shm"
    for text in (RESTORE_INSTRUCTIONS, RESTORE_INSTRUCTIONS_FR,
                 (ROOT / "README.md").read_text(encoding="utf-8"), (ROOT / "README.fr.md").read_text(encoding="utf-8"),
                 (ROOT / "frontend/index.html").read_text(encoding="utf-8")):
        assert rm in text
        # Deleted before the unzip that follows it.
        assert "unzip -o open-the-pantry-backup-" in text[text.index(rm):]


def test_the_entrypoint_fixes_root_owned_files_inside(tmp_path):
    script = (ROOT / "backend/docker-entrypoint.sh").read_text(encoding="utf-8")
    m = re.search(r'\$\(find /app/data (.+?) -print -quit 2>/dev/null\)', script)
    assert m, "the ownership check looks inside the folder"
    (tmp_path / "uploads").mkdir()
    (tmp_path / "recipes.db").write_bytes(b"x")
    os.chown(tmp_path / "recipes.db", 4321, 4321)       # as an unzip run with sudo leaves it, for another uid
    cmd = f'PUID={os.getuid()} PGID={os.getgid()}; find {tmp_path} {m.group(1)} -print -quit'
    assert subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout.strip().endswith("recipes.db")
    os.chown(tmp_path / "recipes.db", os.getuid(), os.getgid())
    assert subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout.strip() == ""


# ---------------------------------------------------------------- M1: zip directories

@pytest.mark.parametrize("entries,zip64,lie", [(30_000, False, None), (60_000, True, None), (400_000, False, 10)])
def test_an_oversized_zip_directory_is_refused_before_parsing(tmp_path, monkeypatch, entries, zip64, lie):
    """Refused from the end record alone: zipfile.ZipFile is never called."""
    from app import interchange, restore
    path = tmp_path / "big.zip"
    make_directory_zip(str(path), entries, zip64, lie)

    def no_parse(*a, **k):
        raise AssertionError("the directory was parsed")
    monkeypatch.setattr(zipfile, "ZipFile", no_parse)
    with pytest.raises(interchange.ImportFileError, match="files; the limit|too large"):
        with interchange.opened(str(path), "big.zip"):
            pass
    if entries > restore.MAX_ENTRIES or lie:
        with pytest.raises(restore.RestoreError):
            restore.kind_of(str(path))
        with pytest.raises(restore.RestoreError):
            restore.inspect(str(path), str(tmp_path / "x.db"))


def test_the_open_endpoints_refuse_it(client, tmp_path):
    path = tmp_path / "big.zip"
    make_directory_zip(str(path), 30_000)
    data = path.read_bytes()
    r = client.post("/api/import/recipes", headers=H, files={"file": ("big.zip", data, "application/zip")})
    assert r.status_code == 422 and "30000" in r.json()["detail"]


def test_ordinary_zips_still_open(tmp_path):
    from app.file_validation import check_zip_size, zip_directory_size
    path = tmp_path / "ok.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for i in range(10):
            zf.writestr(f"uploads/{uuid.uuid4().hex}.jpg", b"x" * 10)
    assert zip_directory_size(str(path))[0] == 10
    check_zip_size(str(path), 20, ValueError)
    with pytest.raises(ValueError):
        check_zip_size(str(path), 5, ValueError)


# ---------------------------------------------------------------- M2: search

def test_words_found_in_text_and_tags_together(client):
    tag = f"Dinnerx{uuid.uuid4().hex[:5]}"
    word = f"chickenx{uuid.uuid4().hex[:5]}"
    rid = client.post("/api/recipes", headers=H, json={
        "title": f"Roast {word}", "source_type": "manual", "ingredients": [{"raw_line": "1 bird"}],
        "steps": ["Roast."], "tags": [{"name": tag, "category": "meal_type"}]}).json()["id"]
    try:
        for q in (word, tag, f"{word} {tag}", f"roast {tag}"):
            got = client.get("/api/recipes", params={"q": q}).json()
            assert rid in [r["id"] for r in got], q
        both = next(r for r in client.get("/api/recipes", params={"q": f"{word} {tag}"}).json() if r["id"] == rid)
        assert both["matched_via"] == ["tag", "text"]
    finally:
        client.delete(f"/api/recipes/{rid}", headers=H)
        client.delete(f"/api/recently-deleted/{rid}", headers=H)


# ---------------------------------------------------------------- M3: tags

def test_at_most_50_tags_in_a_filter(client):
    assert client.get("/api/recipes", params=[("tags", f"t{i}") for i in range(50)]).status_code == 200
    r = client.get("/api/recipes", params=[("tags", f"t{i}") for i in range(1000)])
    assert r.status_code == 422 and "50" in r.json()["detail"]


def test_a_database_error_is_logged_briefly():
    src = (ROOT / "backend/app/main.py").read_text(encoding="utf-8")
    handler = src[src.index("async def _database_busy_handler"):src.index("@app.exception_handler(_HttpsOnly)")]
    assert "exc_info" not in handler and "%.300s" in handler


# ---------------------------------------------------------------- L1: staged photos

def test_a_restore_failing_before_the_copy_removes_its_staged_photos(client, monkeypatch):
    from app import main, restore as restore_mod
    from app.database import UPLOADS_DIR
    _recipe(client, f"L1 {uuid.uuid4().hex[:6]}", photo=True)
    token = _check(client, client.get("/api/backup/database.zip").content).json()["token"]

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(restore_mod, "copy_into_live", full)
    r = client.post("/api/backup/restore", headers=H, json={"token": token})
    assert r.status_code == 500 and "Restart" not in r.json()["detail"]
    assert not os.path.isdir(UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX)
    assert client.get("/api/recipes").status_code == 200


def test_a_failure_before_staging_leaves_an_earlier_staged_folder(client, monkeypatch):
    """Only the folder this restore staged is removed: one an earlier,
    interrupted restore left for a person stays when this one fails before
    staging (here, writing its safety copy)."""
    from app import restore as restore_mod
    from app.database import UPLOADS_DIR
    from app.routes import backup as routes_backup
    staged = UPLOADS_DIR.rstrip("/") + restore_mod.STAGED_SUFFIX
    os.makedirs(staged, exist_ok=True)
    token = _check(client, client.get("/api/backup/database.zip").content).json()["token"]

    def full(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(routes_backup, "build_database_backup", full)
    try:
        r = client.post("/api/backup/restore", headers=H, json={"token": token})
        assert r.status_code == 500
        assert os.path.isdir(staged)
    finally:
        os.rmdir(staged)


# ---------------------------------------------------------------- L2: URLs in the log

@pytest.mark.parametrize("text,secret", [
    ("No recipe found. Tried: link https://example.com/a?token=SECRET1: no recipe", "SECRET1"),
    ("HTTPSConnectionPool(host='example.com', port=443): Max retries exceeded with url: /b?token=SECRET2", "SECRET2"),
    ("Soup (from \"x\", via URL in body (https://u:pw@example.com/c?sig=SECRET3))", "SECRET3"),
])
def test_redact_urls(text, secret):
    from app.ingestion.url_ingest import redact_urls
    out = redact_urls(text)
    assert secret not in out and "pw@" not in out and "example.com" in out or "/b?…" in out


def test_no_recipe_log_has_no_query(monkeypatch):
    import logging
    from app.ingestion import url_ingest
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    url_ingest.log.addHandler(handler)
    try:
        with pytest.raises(url_ingest.NoRecipeFoundError):
            url_ingest.parse_html("<html><title>x</title><p>nothing</p></html>", "https://example.com/a?token=SECRET1")
    finally:
        url_ingest.log.removeHandler(handler)
    assert "SECRET1" not in buf.getvalue() and "example.com/a" in buf.getvalue()


# ---------------------------------------------------------------- L3: auto-saved recipes fit RecipeCreate

def test_a_batch_link_recipe_can_be_saved_again(client, monkeypatch):
    from app import schemas
    from app.ingestion.url_ingest import UrlIngestResult
    from app.routes import ingest
    long = UrlIngestResult(title="T" * 600, ingredients=["1 cup flour"] + ["x" * 2500], steps=["s" * 24_000, "Bake."],
                           prep_time="p" * 300, raw_text="1 cup flour\nBake.")
    monkeypatch.setattr(ingest, "ingest_url", lambda url: long)
    monkeypatch.setattr(ingest, "require_complete", lambda r: r)
    r = client.post("/api/ingest/url/batch", headers=H, json={"urls": ["https://example.com/long"]}).json()
    rid = r["succeeded"][0]["recipe_id"]
    try:
        got = client.get(f"/api/recipes/{rid}").json()
        schemas.RecipeCreate(title=got["title"], source_type="url", prep_time=got["prep_time"],
                             ingredients=[{"raw_line": i["raw_line"]} for i in got["ingredients"]],
                             steps=[s["text"] if isinstance(s, dict) else s for s in got["steps"]])
        assert len(got["title"]) == 500
    finally:
        client.delete(f"/api/recipes/{rid}", headers=H)
        client.delete(f"/api/recently-deleted/{rid}", headers=H)


def test_fit_recipe():
    from app import schemas
    fields, ings, steps = schemas.fit_recipe(
        {"title": "t" * 900, "source_url": "https://example.com/" + "a" * 3000, "servings": "4" * 999, "prep_time": None},
        [{"raw_line": "r" * 3000, "quantity": "1" * 200, "unit": None, "name": "n"}] * 600, ["s" * 30_000] * 700)
    assert len(fields["title"]) == 500 and fields["source_url"] is None and len(fields["servings"]) == schemas.SERVINGS_MAX
    assert len(ings) == 500 and len(ings[0]["raw_line"]) == 2000 and len(ings[0]["quantity"]) == 100
    assert len(steps) == 500 and len(steps[0]) == 20_000


# ---------------------------------------------------------------- L4, L5: import

def test_notes_and_dates_survive_a_json_round_trip(client):
    rid = client.post("/api/recipes", headers=H, json={
        "title": f"Round trip {uuid.uuid4().hex[:6]}", "source_type": "manual",
        "ingredients": [{"raw_line": "1 egg"}], "steps": ["Boil."]}).json()["id"]
    assert client.patch(f"/api/recipes/{rid}/notes", headers=H, json={"notes": "n" * 30_000}).status_code == 200
    from app.database import SessionLocal
    from app import models
    with SessionLocal() as db:
        db.get(models.Recipe, rid).created_at = datetime(2021, 5, 4, 12, 0, tzinfo=timezone.utc)
        db.commit()
    data = client.get("/api/backup/recipes-json.zip").content
    client.delete(f"/api/recipes/{rid}", headers=H)
    client.delete(f"/api/recently-deleted/{rid}", headers=H)
    r = client.post("/api/import/recipes", headers=H, files={"file": ("r.zip", data, "application/zip")}).json()
    new = next(s["recipe_id"] for s in r["succeeded"] if s["title"].startswith("Round trip"))
    others = [s["recipe_id"] for s in r["succeeded"] if s["recipe_id"] != new]
    try:
        got = client.get(f"/api/recipes/{new}").json()
        assert len(got["notes"]) == 30_000
        with SessionLocal() as db:
            assert db.get(models.Recipe, new).created_at.date().isoformat() == "2021-05-04"
    finally:
        for i in [new] + others:
            client.delete(f"/api/recipes/{i}", headers=H)
            client.delete(f"/api/recently-deleted/{i}", headers=H)


def test_dates_read_on_import():
    from app.interchange import _date
    assert _date("2024-03-01 12:00:00").year == 2024              # Paprika's form
    assert _date("2024-03-01T12:00:00+02:00").utcoffset() == timedelta(hours=2)
    for bad in (None, "", "yesterday", "1850-01-01", (datetime.now(timezone.utc) + timedelta(days=9)).isoformat(), 5):
        assert _date(bad) is None, bad


def test_the_import_index_agrees_with_find(client):
    from app import duplicates
    from app.database import SessionLocal
    title, url = f"Index soup {uuid.uuid4().hex[:6]}", f"https://example.com/{uuid.uuid4().hex[:6]}"
    rid = client.post("/api/recipes", headers=H, json={"title": title, "source_type": "url", "source_url": url,
                                                       "ingredients": [{"raw_line": "1 egg"}], "steps": ["Boil."]}).json()["id"]
    try:
        with SessionLocal() as db:
            index = duplicates.Index(db)
            assert index.same_link(url + "?utm_source=x")["id"] == duplicates.same_link(db, url + "?utm_source=x")["id"] == rid
            assert index.same_title(title.upper())["id"] == duplicates.find(db, title=title.upper(), limit=1)[0]["id"]
            assert index.same_link("https://example.com/none") is None
            index.add(999_999, "Brand new", "https://example.com/new")
            assert index.same_title("brand  NEW")["id"] == 999_999
    finally:
        client.delete(f"/api/recipes/{rid}", headers=H)
        client.delete(f"/api/recently-deleted/{rid}", headers=H)


def test_an_import_scans_the_library_once(client, monkeypatch):
    from app import duplicates
    calls = {"find": 0}
    real = duplicates.find

    def counted(*a, **k):
        calls["find"] += 1
        return real(*a, **k)
    monkeypatch.setattr(duplicates, "find", counted)
    recipes = [{"@context": "https://schema.org", "@type": "Recipe", "name": f"Bulk {i} {uuid.uuid4().hex[:4]}",
                "recipeIngredient": ["1 egg"], "recipeInstructions": "Boil."} for i in range(20)]
    r = client.post("/api/import/recipes", headers=H,
                    files={"file": ("r.json", json.dumps(recipes).encode(), "application/json")}).json()
    try:
        assert len(r["succeeded"]) == 20 and calls["find"] == 0
    finally:
        for s in r["succeeded"]:
            client.delete(f"/api/recipes/{s['recipe_id']}", headers=H)
            client.delete(f"/api/recently-deleted/{s['recipe_id']}", headers=H)


# ---------------------------------------------------------------- L6: nested emails

def _nested(levels):
    body = "Content-Type: text/plain\r\n\r\nhi\r\n"
    for i in range(levels):
        b = f"b{i}"
        body = f"Content-Type: multipart/mixed; boundary={b}\r\n\r\n--{b}\r\n{body}--{b}--\r\n"
    return ("From: a@example.com\r\nSubject: [RECIPE]\r\n" + body).encode()


class _Imap:
    def __init__(self, raw):
        self.raw = raw

    def fetch(self, msg_id, what):
        if "SIZE" in what:
            return "OK", [b"1 (RFC822.SIZE 100)"]
        return "OK", [(b"1", self.raw)]


@pytest.mark.parametrize("levels", [60, 1200])
def test_a_deeply_nested_email_is_unreadable_not_retried(levels):
    from app import email_client
    with pytest.raises(email_client.MessageUnreadableError):
        email_client.fetch_full_message(_Imap(_nested(levels)), b"1")
    assert email_client.fetch_full_message(_Imap(_nested(3)), b"1")["Subject"] == "[RECIPE]"


def test_the_scan_marks_it_read_and_refuses_a_stranger_before_downloading(client, monkeypatch):
    from app import email_client
    from app.database import SessionLocal
    from app.routes import email as email_routes
    with SessionLocal() as db:
        s = email_routes._get_email_settings(db)
        s.enabled, s.imap_host, s.username, s.password_encrypted = True, "imap.example.com", "u", "x"
        s.allowed_senders = "friend@example.com"
        db.commit()
    seen = []
    monkeypatch.setattr(email_routes.crypto, "decrypt_secret", lambda v: "pw")
    monkeypatch.setattr(email_client, "connect_imap", lambda *a, **k: type("I", (), {"logout": lambda self: None})())
    monkeypatch.setattr(email_client, "search_unseen_by_subject", lambda imap, kw: [b"1", b"2"])
    monkeypatch.setattr(email_client, "fetch_sender", lambda imap, i: "Stranger <bad@x.example>" if i == b"1" else "friend@example.com")

    def full(imap, i):
        assert i == b"2", "the stranger's email was downloaded"
        raise email_client.MessageUnreadableError("email can't be read: its parts are nested too deeply")
    monkeypatch.setattr(email_client, "fetch_full_message", full)
    monkeypatch.setattr(email_client, "mark_seen", lambda imap, i: seen.append(i))
    try:
        out = email_routes.run_email_scan()
        assert seen == [b"1", b"2"]                       # both marked read: neither retried
        assert any(m.startswith("IGNORED: an email from Stranger") for m in out["messages"])
        assert any("nested too deeply" in m for m in out["messages"])
    finally:
        from sqlalchemy import text
        with SessionLocal() as db:
            s = email_routes._get_email_settings(db)
            s.enabled, s.imap_host, s.username, s.password_encrypted, s.allowed_senders = False, None, None, None, ""
            db.execute(text("DELETE FROM email_notification_queue"))   # its lines aren't the next test's
            db.commit()


# ---------------------------------------------------------------- L7, L8, L9

def test_the_list_shows_only_the_newest_answer():
    js = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    body = js[js.index("  async function loadRecipes()"):js.index("// Recently deleted (0109)")]
    assert "const generation = ++recipesGeneration;" in body
    assert body.index("if (stale()) return;") < body.index("currentRecipes = recipes;")


@pytest.mark.parametrize("url,host", [("https://café.example/recette", "xn--caf-dma.example"),
                                      ("https://例え.jp/", "xn--r8jz45g.jp"),
                                      ("https://straße.example/", "xn--strae-oqa.example")])
def test_unicode_host_names(monkeypatch, url, host):
    import socket
    from app.ingestion import url_ingest
    looked_up = []
    monkeypatch.setattr(url_ingest.socket, "getaddrinfo",
                        lambda h, p: looked_up.append(h) or [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))])
    assert url_ingest.validate_public_url(url) == "93.184.216.34"
    assert looked_up == [host]


def test_a_damaged_password_file_isnt_no_password(client, monkeypatch):
    from app import admin_lock
    # The suite's own password file moved aside and put back as it was
    # (same file, same time), so the shared client stays unlocked after.
    kept = admin_lock.PASSWORD_PATH + ".kept-by-test"
    os.replace(admin_lock.PASSWORD_PATH, kept)
    with open(admin_lock.PASSWORD_PATH, "w") as f:
        f.write("")
    try:
        assert admin_lock.password_set() and admin_lock.damaged() and not admin_lock.was_reset()
        for path, body in (("/api/admin/password", {"password": "a-new-password-1"}),
                           ("/api/admin/unlock", {"password": "a-new-password-1"})):
            r = client.post(path, headers=H, json=body)
            assert r.status_code in (409, 403), (path, r.text)
            if r.status_code == 409:
                assert "damaged" in r.json()["detail"]
        assert admin_lock.seconds_left("anything") == 0
    finally:
        os.replace(kept, admin_lock.PASSWORD_PATH)
    assert client.get("/api/admin/status").json().get("unlocked") is not False
    src = (ROOT / "backend/app/admin_lock.py").read_text(encoding="utf-8")
    assert "os.fsync(f.fileno())" in src[src.index("def _write("):]


# ---------------------------------------------------------------- nits

def test_the_daily_scan_catches_up():
    from app.routes.email import _scan_due
    tz = datetime.now().astimezone().tzinfo
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    yesterday = (now - timedelta(days=1)).replace(tzinfo=tz)
    assert _scan_due(now.replace(hour=2), 2, yesterday)          # the hour itself
    assert _scan_due(now, 2, yesterday)                           # missed at 2: caught up later that day
    assert not _scan_due(now, 2, now.replace(hour=3, tzinfo=tz))  # already scanned today
    assert not _scan_due(now.replace(hour=1), 2, yesterday)       # not yet
    assert not _scan_due(now, 2, None)                            # never scanned: only at the hour


def test_a_relative_photo_is_resolved_against_the_page(monkeypatch):
    from app.ingestion import url_ingest
    page = ('<script type="application/ld+json">' + json.dumps({
        "@type": "Recipe", "name": "Soup", "image": "/img/soup.jpg", "recipeIngredient": ["1 egg"],
        "recipeInstructions": "Boil."}) + "</script>")
    resp = type("R", (), {"text": page, "status_code": 200, "url": "https://example.com/recipes/soup"})()
    monkeypatch.setattr(url_ingest, "validate_public_url", lambda url, deadline=None: "93.184.216.34")
    monkeypatch.setattr(url_ingest, "safe_get", lambda url, deadline=None: resp)
    assert url_ingest.ingest_url("https://example.com/recipes/soup").image_url == "https://example.com/img/soup.jpg"


def test_ci_starts_the_image():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = ci[ci.index("\n  docker-build:"):ci.index("\n  publish:")]
    assert "load: true" in job and "docker run -d --name otp-ci" in job and "/healthz" in job
    assert "p005-en-pdf-text-layer/input.pdf" in job and (ROOT / "backend/tests/corpus/p005-en-pdf-text-layer/input.pdf").is_file()
