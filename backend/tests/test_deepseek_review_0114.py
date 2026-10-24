"""DeepSeek review of 0114.

Not changed, with the reason here: (1) "a zip entry can unpack past its
stated size and fill the disk" -- zipfile stops at the stated size and
raises BadZipFile, checked below; (6) restore re-encoding outside the
re-encode slots -- reencode_image takes a slot itself, and a restore runs
alone; openRecipeDetail keeping the first opener's focus -- that is where
focus belongs when the recipe view closes."""
import io
import json
import struct
import zipfile

import pytest


def _lying_zip(name: str, real: int, stated: int) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, b"\0" * real)
    data = bytearray(buf.getvalue())
    struct.pack_into("<I", data, data.find(b"PK\x03\x04") + 22, stated)
    struct.pack_into("<I", data, data.find(b"PK\x01\x02") + 24, stated)
    return bytes(data)


def test_a_zip_entry_never_unpacks_past_its_stated_size(tmp_path):
    from app import restore
    path = tmp_path / "b.zip"
    path.write_bytes(_lying_zip("uploads/img-" + "a" * 32 + ".png", real=20 * 1024 * 1024, stated=1024))
    out = tmp_path / "out"
    kept, skipped = restore.unpack_uploads(str(path), str(out))
    assert (kept, skipped) == (0, 1)
    assert list(out.iterdir()) == []


def test_json_bodies_are_capped(client):
    from app import main
    big = b'{"ids": [' + b"1," * (main.MAX_JSON_BODY_BYTES // 2) + b'1]}'
    r = client.post("/api/recipes/batch-delete", content=big, headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    # Without a Content-Length (chunked), counted as it arrives.
    def chunks():
        yield b'{"ids": ['
        for _ in range(40):
            yield b"1," * (1024 * 1024)
        yield b"1]}"
    r = client.post("/api/recipes/batch-delete", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413
    # Uploads aren't affected (they stream to disk with their own limits).
    assert client.post("/api/recipes/batch-delete", json={"ids": []}).status_code == 200


@pytest.mark.parametrize("path,body", [
    ("/api/recipes/batch-delete", {"ids": list(range(10_001))}),
    ("/api/ingest/url/batch", {"urls": ["https://example.com/" + "a" * 2000]}),
    ("/api/tags/suggest", {"title": "x", "ingredients": ["a" * 2001]}),
    ("/api/tags/auto-apply", {"selections": [{"id": 1, "tags": ["t" * 101]}]}),
])
def test_lists_and_their_items_are_bounded(client, path, body):
    assert client.post(path, json=body).status_code == 422


def test_email_settings_fields_are_bounded(client):
    r = client.put("/api/email-settings", json={"password": "p" * 1025})
    assert r.status_code == 422
    r = client.put("/api/email-settings", json={"imap_host": "h" * 254})
    assert r.status_code == 422


def test_emailed_recipe_is_held_to_the_api_limits(client, data_dir):
    from app import main, models
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        rid = main._save_email_recipe(db, {
            "title": "T" * 900, "source_url": "https://example.com/" + "a" * 3000, "servings": "4" * 300,
            "raw_text": "x" * 1_100_000, "ingredients": [], "steps": [], "tags": []}, "subject")
        r = db.get(models.Recipe, rid)
        assert len(r.title) == 500 and r.source_url is None and len(r.servings) == 100 and len(r.raw_text) == 1_000_000
    finally:
        db.close()
    client.delete(f"/api/recipes/{rid}")
    client.delete(f"/api/recently-deleted/{rid}")


def test_a_damaged_key_file_is_reported_once(tmp_path, monkeypatch):
    from app import crypto
    key = tmp_path / "encryption.key"
    key.write_text("not a key")
    monkeypatch.setattr(crypto, "key_file_path", lambda: str(key))
    monkeypatch.delenv(crypto.ENCRYPTION_KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(crypto, "_warned_invalid_file", None)
    warned = []
    monkeypatch.setattr(crypto.log, "warning", lambda msg, *a, **k: warned.append(msg))
    for _ in range(5):
        assert crypto.key_source() == "file_invalid"
    assert len(warned) == 1
    key.write_text("still not a key, changed")
    crypto.key_source()
    assert len(warned) == 2


@pytest.mark.parametrize("url,ok", [
    ("https://cpanel.example.com:2083", True), ("https://cpanel.example.com", True),
    ("https://cpanel.example.com:99999", False), ("https://cpanel.example.com:0", False),
])
def test_cpanel_address_port_range(data_dir, monkeypatch, url, ok):
    """Port 99999 was accepted."""
    from app import https_setup
    saved = []
    monkeypatch.setattr(https_setup, "_value", lambda name: (None, False))
    monkeypatch.setattr(https_setup, "_stored_login", lambda: {})
    monkeypatch.setattr(https_setup, "_normal_domain", lambda d: None)     # stop right after the address check
    try:
        https_setup.save_login("user", url, "pantry.example.com", "token")
    except https_setup.HttpsError as e:
        saved.append(str(e))
    address_refused = any("cPanel address" in m for m in saved)
    assert address_refused is (not ok)


def test_status_doesnt_claim_a_model_it_couldnt_read(tmp_path, monkeypatch):
    from app.ml import service
    monkeypatch.setattr(service, "_path", lambda name: str(tmp_path / name))
    (tmp_path / "model.json").write_text(json.dumps({
        "format": service.FORMAT, "files": {"segmenter": "seg.npz"},
        "segmenter": {"status": "active"}, "tags": {"status": "off"}}))
    (tmp_path / "seg.npz").write_bytes(b"truncated")
    monkeypatch.setattr(service, "enabled", lambda: True)
    service.load()
    assert service._report == {}


def test_busy_state_covers_text_boxes_and_lists():
    from pathlib import Path
    js = (Path(__file__).parents[2] / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert '".modal button, .modal input, .modal textarea, .modal select"' in js
