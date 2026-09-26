"""Findings from the Opus 5 review (group A: things you'd hit in normal use).
Each was reproduced against the previous code before being fixed.

app.* is imported inside tests only; see the note in test_backup.py.
"""
import os
import uuid

import pytest


def test_rate_limit_ignores_photos_and_static_files(client, monkeypatch):
    """Scrolling ~100 photo recipes used the whole 120/min budget on
    thumbnails, then /api/recipes got 429 and the list read "No recipes
    match"."""
    import app.main as m
    monkeypatch.setattr(m, "RATE_LIMIT_MAX_REQUESTS", 5)
    m._request_log.clear()
    for _ in range(20):
        client.get("/uploads/img-" + uuid.uuid4().hex + ".png")
    assert client.get("/api/recipes").status_code == 200
    m._request_log.clear()


def test_rate_limit_still_applies_to_the_api(client, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "RATE_LIMIT_MAX_REQUESTS", 3)
    m._request_log.clear()
    codes = [client.get("/api/tags").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
    m._request_log.clear()


@pytest.mark.parametrize("title", ["Grandma’s Pie", "Stew — slow", "Pie \U0001F967", 'Say "cheese"'])
def test_export_works_for_titles_that_arent_latin1(client, title):
    """Curly apostrophes, dashes and emoji in titles made both exports 500."""
    rid = client.post("/api/recipes", json={"title": title, "source_type": "manual",
                      "ingredients": [], "steps": ["Bake"], "tags": []}).json()["id"]
    try:
        for ext in ("pdf", "html"):
            r = client.get(f"/api/recipes/{rid}/export.{ext}")
            assert r.status_code == 200, (ext, r.status_code)
            cd = r.headers["content-disposition"]
            cd.encode("latin-1")
            assert "filename*=UTF-8''" in cd
    finally:
        client.delete(f"/api/recipes/{rid}")


@pytest.mark.parametrize("value", ["00:99:99", "01:24:00", "00:00:60", "20000:00:00", "61:00:00"])
def test_out_of_range_cook_times_are_rejected(client, value):
    r = client.post("/api/recipes", json={"title": "T", "source_type": "manual", "ingredients": [],
                                         "steps": [], "tags": [], "actual_cook_time": value})
    assert r.status_code == 422


def test_long_cook_times_still_allowed_and_buckets_stay_small():
    """8-day bacon cure must still work; the filter list stays short."""
    from app.time_utils import ddhhmm_to_minutes, available_time_buckets
    m = ddhhmm_to_minutes("08:06:20")
    assert m == 8 * 1440 + 6 * 60 + 20
    buckets = available_time_buckets([m])
    assert len(buckets) < 100
    assert buckets[-1]["minutes"] >= m and buckets[-1]["label"] == "9d"


def test_huge_notification_cooldown_is_rejected(client):
    r = client.put("/api/email-settings", json={
        "enabled": False, "imap_host": "h", "imap_port": 993, "imap_use_ssl": True, "smtp_host": "h",
        "smtp_port": 465, "smtp_use_tls": False, "username": "u", "notify_email": "u@x",
        "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 10 ** 12})
    assert r.status_code == 422


def test_a_recipe_cannot_claim_another_recipes_photo(client):
    """Recipe B pointed at A's photo; deleting B deleted A's file."""
    from app.database import UPLOADS_DIR, SessionLocal
    from app import models

    img = f"img-{uuid.uuid4().hex}.png"
    path = os.path.join(UPLOADS_DIR, img)
    open(path, "wb").write(b"x")
    db = SessionLocal()
    a = models.Recipe(title="Photo owner", source_type="manual", image_path=img)
    db.add(a); db.commit(); aid = a.id; db.close()
    try:
        r = client.post("/api/recipes", json={"title": "Thief", "source_type": "manual", "ingredients": [],
                                             "steps": [], "tags": [], "image_path": img})
        assert r.status_code == 400
        assert os.path.exists(path)
        # A itself can still be saved with its own photo
        body = client.get(f"/api/recipes/{aid}").json()
        payload = {k: body[k] for k in ("title", "source_type")}
        payload.update(ingredients=[], steps=[], tags=[], image_path=img)
        assert client.put(f"/api/recipes/{aid}", json=payload).status_code == 200
        assert os.path.exists(path)
    finally:
        client.delete(f"/api/recipes/{aid}")


def test_tagger_does_not_call_lamb_or_turkey_vegetarian():
    from app.ingestion.tagger import suggest_tags
    for title, ings in [("Roast Leg of Lamb", ["lamb"]), ("Thanksgiving Turkey", ["turkey"]),
                        ("Moules marinières", ["mussels"]), ("Osso Buco", ["veal shanks"])]:
        tags = [n for n, c, _ in suggest_tags(title, ings, "")]
        assert "Vegetarian" not in tags, title
    assert "Vegetarian" in [n for n, c, _ in suggest_tags("Lentil Soup", ["lentils"], "")]


def test_email_settings_report_server_timezone(client):
    assert "server_timezone" in client.get("/api/email-settings").json()
