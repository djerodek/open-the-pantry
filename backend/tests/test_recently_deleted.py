"""Recently deleted (0109): a deleted recipe is hidden everywhere, can be
put back for 30 days, then goes for good with its files."""
import os
import sqlite3
import time
import uuid
import zipfile
import io
from datetime import datetime, timedelta, timezone

from .conftest import TINY_PNG

H = {"X-Requested-With": "OpenThePantry"}


def _recipe(client, title, tags=(), photo=False):
    image = None
    if photo:
        up = client.post("/api/upload-image", files={"file": ("p.png", TINY_PNG, "image/png")})
        image = up.json()["stored_file"]
    body = {"title": title, "source_type": "manual", "ingredients": [{"raw_line": "1 cup rice"}],
            "steps": ["Cook."], "tags": [{"name": n, "category": c} for n, c in tags], "image_path": image}
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _bin(client):
    return client.get("/api/recently-deleted").json()


def _set_deleted_at(rid, when):
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        r = db.query(models.Recipe).execution_options(include_deleted=True).filter_by(id=rid).one()
        r.deleted_at = when
        db.commit()
    finally:
        db.close()


def test_a_deleted_recipe_is_hidden_and_can_be_put_back(client):
    sfx = uuid.uuid4().hex[:6]
    assert client.post("/api/tags", json={"name": f"BinTag{sfx}", "category": "meal_type"}).status_code == 200
    r = _recipe(client, f"Binned stew {sfx}", tags=[(f"BinTag{sfx}", "meal_type")], photo=True)
    assert client.delete(f"/api/recipes/{r['id']}").json() == {"ok": True, "days": 30}

    # Gone from everything that lists or finds recipes.
    assert client.get(f"/api/recipes/{r['id']}").status_code == 404
    assert r["id"] not in [x["id"] for x in client.get("/api/recipes").json()]
    assert client.get("/api/recipes", params={"q": f"Binned stew {sfx}"}).json() == []
    dup = client.get("/api/recipes/duplicates", params={"title": f"Binned stew {sfx}"}).json()
    assert not (dup.get("matches") if isinstance(dup, dict) else dup)
    opts = client.get("/api/picker/options").json()
    assert f"BinTag{sfx}" not in [t["name"] for t in opts["meal_type"]]
    assert client.get(f"/api/picker/recipes/{r['id']}").status_code == 404
    assert client.delete(f"/api/recipes/{r['id']}").status_code == 404           # already deleted
    # Its photo stays until it goes for good.
    assert client.get(f"/uploads/{r['image_path']}").status_code == 200

    entry = next(x for x in _bin(client)["recipes"] if x["id"] == r["id"])
    assert entry["title"] == f"Binned stew {sfx}" and entry["days_left"] == 30 and entry["image_path"] == r["image_path"]

    # A tag deleted while it waited: gone from the search column on the way back.
    tag_id = next(t["id"] for t in client.get("/api/tags").json() if t["name"] == f"BinTag{sfx}")
    client.delete(f"/api/tags/{tag_id}")
    back = client.post(f"/api/recently-deleted/{r['id']}/restore")
    assert back.status_code == 200 and back.json()["id"] == r["id"]
    got = client.get(f"/api/recipes/{r['id']}").json()
    assert got["title"] == f"Binned stew {sfx}" and got["tags"] == []
    assert [x["id"] for x in client.get("/api/recipes", params={"q": f"Binned stew {sfx}"}).json()] == [r["id"]]
    assert client.get("/api/recipes", params={"q": f"BinTag{sfx}"}).json() == []
    assert r["id"] not in [x["id"] for x in _bin(client)["recipes"]]
    assert client.post(f"/api/recently-deleted/{r['id']}/restore").status_code == 404   # not in the bin

    client.delete(f"/api/recipes/{r['id']}")
    client.delete(f"/api/recently-deleted/{r['id']}")


def test_delete_for_good_and_empty(client):
    a = _recipe(client, f"Bin A {uuid.uuid4().hex[:6]}", photo=True)
    b = _recipe(client, f"Bin B {uuid.uuid4().hex[:6]}", photo=True)
    live = _recipe(client, f"Bin live {uuid.uuid4().hex[:6]}")
    res = client.post("/api/recipes/batch-delete", json={"ids": [a["id"], b["id"]]}).json()
    assert res["deleted"] == [a["id"], b["id"]] and res["days"] == 30
    assert client.delete(f"/api/recently-deleted/{live['id']}").status_code == 404   # only from the bin
    assert client.delete(f"/api/recently-deleted/{a['id']}").json() == {"ok": True}
    assert client.get(f"/uploads/{a['image_path']}").status_code == 404
    assert client.get(f"/uploads/{b['image_path']}").status_code == 200
    assert client.delete("/api/recently-deleted").json()["deleted"] >= 1
    assert _bin(client)["recipes"] == []
    assert client.get(f"/uploads/{b['image_path']}").status_code == 404
    assert client.get(f"/api/recipes/{live['id']}").status_code == 200
    client.delete(f"/api/recipes/{live['id']}")
    client.delete(f"/api/recently-deleted/{live['id']}")


def test_the_sweep_deletes_after_30_days(client):
    from app import main
    from app.database import UPLOADS_DIR
    old = _recipe(client, f"Bin old {uuid.uuid4().hex[:6]}", photo=True)
    recent = _recipe(client, f"Bin recent {uuid.uuid4().hex[:6]}", photo=True)
    for r in (old, recent):
        client.delete(f"/api/recipes/{r['id']}")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    _set_deleted_at(old["id"], now - timedelta(days=30, minutes=1))
    _set_deleted_at(recent["id"], now - timedelta(days=29, hours=23))
    entry = next(x for x in _bin(client)["recipes"] if x["id"] == recent["id"])
    assert entry["days_left"] == 1
    # The uploads sweep keeps a waiting recipe's photo, however old the file.
    path = os.path.join(UPLOADS_DIR, recent["image_path"])
    os.utime(path, (time.time() - 10 * 86400,) * 2)
    main._sweep_orphaned_upload_files()
    assert os.path.isfile(path)

    assert main.purge_recently_deleted() == 1
    ids = [x["id"] for x in _bin(client)["recipes"]]
    assert old["id"] not in ids and recent["id"] in ids
    assert not os.path.isfile(os.path.join(UPLOADS_DIR, old["image_path"]))
    client.delete(f"/api/recently-deleted/{recent['id']}")


def test_backups_count_only_the_library(client):
    keep = _recipe(client, f"Bin backup keep {uuid.uuid4().hex[:6]}")
    gone = _recipe(client, f"Bin backup gone {uuid.uuid4().hex[:6]}")
    client.delete(f"/api/recipes/{gone['id']}")
    total_live = len(client.get("/api/recipes").json())
    data = client.get("/api/backup/database.zip").content
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        import json
        assert json.loads(zf.read("manifest.json"))["recipe_count"] == total_live
        db = zf.read("recipes.db")
    # The binned recipe is in the backup, still binned.
    tmp = f"/tmp/bin-backup-{uuid.uuid4().hex}.db"
    with open(tmp, "wb") as f:
        f.write(db)
    con = sqlite3.connect(tmp)
    try:
        assert con.execute("SELECT deleted_at IS NOT NULL FROM recipes WHERE id = ?", (gone["id"],)).fetchone() == (1,)
    finally:
        con.close()
        os.remove(tmp)
    check = client.post("/api/backup/restore/check", files={"file": ("b.zip", data, "application/zip")}, headers=H)
    assert check.status_code == 200 and check.json()["recipes"] == total_live
    for r in (keep, gone):
        client.delete(f"/api/recipes/{r['id']}")
        client.delete(f"/api/recently-deleted/{r['id']}")


def test_an_older_database_gets_the_column():
    from app.init_db import NEW_RECIPE_COLUMNS
    assert NEW_RECIPE_COLUMNS["deleted_at"] == "DATETIME"
