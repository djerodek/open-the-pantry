"""GPT review of 0119: upload sizes limited as the body arrives, merges
that would cut notes or tags refused, search's word list forgotten after
a restore."""
import asyncio
import io
import json
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


def _multipart(name, content, boundary="b0119"):
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + content + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _send_chunked(path, chunks, content_type, cookie=None):
    """Drives BodySizeLimit directly, the body in chunks with no
    Content-Length: returns (status, body, bytes the inner app received)."""
    from app.main import BodySizeLimit
    received = {"n": 0}

    async def inner(scope, receive, send):
        while True:
            m = await receive()
            if m["type"] == "http.disconnect":
                break
            received["n"] += len(m.get("body", b""))
            if not m.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"inner"})

    pending = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    out = []

    async def receive():
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(m):
        out.append(m)

    headers = [(b"content-type", content_type.encode()), (b"host", b"testserver")]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers, "query_string": b"",
             "client": ("127.0.0.1", 1), "server": ("testserver", 80), "scheme": "http", "root_path": ""}
    asyncio.run(BodySizeLimit(inner)(scope, receive, send))
    status = next(m["status"] for m in out if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in out if m["type"] == "http.response.body")
    return status, body, received["n"]


def test_an_upload_stops_at_its_routes_limit(client):
    """The reviewer's check: multipart went through whatever its size."""
    from app import main
    mb = 1024 * 1024
    body, ctype = _multipart("big.png", b"x" * (22 * mb))
    chunks = [body[i:i + mb] for i in range(0, len(body), mb)]
    status, answer, seen = _send_chunked("/api/upload-image", chunks, ctype)
    assert status == 413 and json.loads(answer)["detail"] == "File too large (max 20 MB)."
    assert seen <= main.MAX_UPLOAD_BYTES + main.MULTIPART_MARGIN_BYTES        # stopped as it arrived
    # Under the limit: through to the route.
    small, ctype = _multipart("p.png", b"x" * mb)
    assert _send_chunked("/api/upload-image", [small], ctype)[0] == 200
    # A multipart body to any other route: the 32 MB of other bodies.
    assert main._multipart_limit({"path": "/api/recipes"})[0] == main.MAX_JSON_BODY_BYTES + main.MULTIPART_MARGIN_BYTES


def test_a_declared_length_over_the_limit_is_refused_unread(client):
    body, ctype = _multipart("big.pdf", b"%PDF" + b"x" * (21 * 1024 * 1024))
    r = client.post("/api/ingest/pdf", content=body, headers={"Content-Type": ctype})
    assert r.status_code == 413
    fr = client.post("/api/ingest/pdf", content=body, headers={"Content-Type": ctype, "X-App-Lang": "fr"})
    assert fr.json()["detail"] == "Fichier trop volumineux (maximum 20 Mo)."
    # The limits per route, as the routes have them.
    from app import main
    m = main.MULTIPART_MARGIN_BYTES
    assert main._multipart_limit({"path": "/api/import/recipes"})[0] == main.MAX_IMPORT_BYTES + m
    assert main._multipart_limit({"path": "/api/ingest/pdf/batch"})[0] == main.MAX_BATCH_PDF_BYTES + m
    assert main._multipart_limit({"path": "/api/ingest/images"})[0] == main.MAX_COMBINED_IMAGES * main.MAX_UPLOAD_BYTES + m
    # Uploads under the limits still work.
    assert client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")}).status_code == 200


def test_restore_check_without_the_password_stops_at_the_import_limit(client, monkeypatch):
    """Without the password the box only imports (1 GB); past that the page
    gets the locked answer, asks for the password and resends."""
    from app import main
    monkeypatch.setattr(main, "MAX_IMPORT_BYTES", 2000)
    monkeypatch.setattr(main, "MULTIPART_MARGIN_BYTES", 0)
    body, ctype = _multipart("backup.zip", b"PK" + b"x" * 5000)
    chunks = [body[i:i + 1000] for i in range(0, len(body), 1000)]
    status, answer, seen = _send_chunked("/api/backup/restore/check", chunks, ctype)
    assert status == 401 and json.loads(answer)["locked"] is True and seen <= 2000
    # With the password: the full-backup limit.
    cookie = "; ".join(f"{k}={v}" for k, v in client.cookies.items())
    assert main._multipart_limit({"path": "/api/backup/restore/check", "type": "http",
                                  "headers": [(b"cookie", cookie.encode())]})[0] == main.restore_mod.MAX_TOTAL_BYTES
    status, _, seen = _send_chunked("/api/backup/restore/check", chunks, ctype, cookie=cookie)
    assert status == 200 and seen == len(body)


def _recipe(client, title, tags=()):
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual",
                                          "ingredients": [{"raw_line": "1 cup rice"}], "steps": ["Cook."],
                                          "tags": [{"name": t, "category": "custom"} for t in tags]})
    assert r.status_code == 200, r.text
    return r.json()


def _cleanup(client, *ids):
    for rid in ids:
        client.delete(f"/api/recipes/{rid}")
        client.delete(f"/api/recently-deleted/{rid}")


def test_a_merge_that_would_cut_notes_or_tags_is_refused(client):
    from app.main import NOTES_MAX
    sfx = uuid.uuid4().hex[:6]
    a, b = _recipe(client, f"Merge long A {sfx}"), _recipe(client, f"Merge long B {sfx}")
    try:
        client.patch(f"/api/recipes/{a['id']}/notes", json={"notes": "a" * NOTES_MAX})
        client.patch(f"/api/recipes/{b['id']}/notes", json={"notes": "Keep me."})
        both = {"keep_id": a["id"], "other_id": b["id"], "take": {"notes": "both"}}
        r = client.post("/api/recipes/merge", json=both)
        assert r.status_code == 400 and "50,000 characters" in r.json()["detail"]
        fr = client.post("/api/recipes/merge", json=both, headers={"X-App-Lang": "fr"}).json()["detail"]
        assert fr.startswith("Les notes des deux recettes dépassent ensemble 50 000 caractères.")
        # Nothing changed, and the other recipe is still in the library.
        assert client.get(f"/api/recipes/{b['id']}").json()["notes"] == "Keep me."
        assert len(client.get(f"/api/recipes/{a['id']}").json()["notes"]) == NOTES_MAX
        # One recipe's notes: fine.
        assert client.post("/api/recipes/merge", json={**both, "take": {"notes": "keep"}}).status_code == 200
    finally:
        _cleanup(client, a["id"], b["id"])

    c = _recipe(client, f"Merge tags C {sfx}", tags=[f"MT{sfx}-{i}" for i in range(30)])
    d = _recipe(client, f"Merge tags D {sfx}", tags=[f"MT{sfx}-{i}" for i in range(20, 55)])
    g = _recipe(client, f"Merge tags G {sfx}", tags=[f"MT{sfx}-{i}" for i in range(60, 80)])
    try:
        # 30 + 35, 10 shared: 55, over the 50 a recipe holds.
        r = client.post("/api/recipes/merge", json={"keep_id": c["id"], "other_id": d["id"]})
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == "Together these recipes have 55 tags; a recipe can have at most 50. Remove some tags first."
        assert client.get(f"/api/recipes/{d['id']}").status_code == 200
        assert len(client.get(f"/api/recipes/{c['id']}").json()["tags"]) == 30
        # Exactly 50: merged, every tag kept.
        r = client.post("/api/recipes/merge", json={"keep_id": c["id"], "other_id": g["id"]})
        assert r.status_code == 200 and len(r.json()["tags"]) == 50
    finally:
        _cleanup(client, c["id"], d["id"], g["id"])


def test_restore_forgets_searchs_word_list(client):
    """Restore isn't one of the changes that refreshed it: the restored
    library was searched with the old one's words for up to 30 s."""
    backup = client.get("/api/backup/database.zip").content
    r = _recipe(client, "Plimbertoast")
    # The list as it is now, with "plimbertoast" in it, cached.
    found = client.get("/api/recipes", params={"q": "plimbertoasr"})
    assert "plimbertoasr" in json.loads(found.headers.get("X-Search-Corrected", "{}"))
    tok = client.post("/api/backup/restore/check", headers=H,
                      files={"file": ("b.zip", backup, "application/zip")}).json()["token"]
    assert client.post("/api/backup/restore", headers=H, json={"token": tok}).status_code == 200
    # Restored: no Plimbertoast, so nothing to correct to.
    found = client.get("/api/recipes", params={"q": "plimbertoasr"})
    assert found.status_code == 200 and found.json() == []
    assert "X-Search-Corrected" not in found.headers
    _cleanup(client, r["id"])
