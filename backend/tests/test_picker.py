"""What to eat? / On mange quoi? (app/picker.py, specs/dinner-picker-spec.md
section 8): options, matching, cocktails, weighting, rounds, bad input, the
read-only picker port, the shared checks, the Settings switch and the links
between the two pages.

The session shares one database with every other test, so the recipes here
carry tags of their own (names with "Pk") and are deleted afterwards."""
import asyncio
import io
import os
import random
import socket
import sqlite3
import urllib.error
import urllib.request
import uuid
import zipfile
from collections import defaultdict

import pytest
from fastapi.testclient import TestClient

from .conftest import TINY_PNG


def _tag(name, category):
    return {"name": name, "category": category}


@pytest.fixture(scope="module")
def picker_client(client):
    from app.main import picker_app
    return TestClient(picker_app)


@pytest.fixture(scope="module")
def pk(client):
    """A small library:
      r1 PkMeal1 + PkA, quick, 5 stars
      r2 PkMeal2 + PkA, moderate
      r3 PkMeal1 + PkB, no pace, 1 star
      r4 Brunch + Cocktails + PkA, quick  (a drink: never picked)
      r5 PkA + Shaken (cocktail style)    (a drink: never picked)
      r6 PkA only, no meal type, no pace
      r7 Breakfast + Dinner + PkStyle (for the built-in order)"""
    sfx = uuid.uuid4().hex[:6]
    names = {k: f"{k}{sfx}" for k in ("PkMeal1", "PkMeal2", "PkA", "PkB", "PkStyle")}
    ids = {}

    def make(key, title, tags, **rating):
        r = client.post("/api/recipes", json={"title": f"{title} {sfx}", "source_type": "manual",
                                               "ingredients": [], "steps": ["Cook."], "tags": tags})
        assert r.status_code == 200, r.text
        ids[key] = r.json()["id"]
        if rating:
            r = client.patch(f"/api/recipes/{ids[key]}/rating", json=rating)
            assert r.status_code == 200, r.text

    m1, m2 = _tag(names["PkMeal1"], "meal_type"), _tag(names["PkMeal2"], "meal_type")
    a, b = _tag(names["PkA"], "main_ingredient"), _tag(names["PkB"], "main_ingredient")
    make("r1", "Pk one", [m1, a], cook_time_rating="quick", tastiness_rating=5)
    make("r2", "Pk two", [m2, a], cook_time_rating="moderate")
    make("r3", "Pk three", [m1, b], tastiness_rating=1)
    make("r4", "Pk punch", [_tag("Brunch", "meal_type"), _tag("Cocktails", "meal_type"), a], cook_time_rating="quick")
    make("r5", "Pk shaken", [a, {"name": "Shaken", "category": "cooking_style", "subgroup": "cocktail"}])
    make("r6", "Pk six", [a])
    make("r7", "Pk seven", [_tag("Breakfast", "meal_type"), _tag("Dinner", "meal_type"),
                            _tag(names["PkStyle"], "cooking_style")])
    tags = {t["name"]: t["id"] for t in client.get("/api/tags").json()}
    yield {"ids": ids, "names": names, "tag": lambda key: tags[names[key]], "tags": tags, "sfx": sfx}
    for rid in ids.values():
        client.delete(f"/api/recipes/{rid}")


def all_matches(c, params):
    """Every recipe a set of choices can return: picks until the round is
    exhausted, checking the counts on the way."""
    seen, count = [], None
    for _ in range(1000):
        r = c.get("/api/picker/random", params={**params, "exclude": seen})
        assert r.status_code == 200, r.text
        body = r.json()
        count = body["match_count"] if count is None else count
        assert body["match_count"] == count
        if body["exhausted"] or body["recipe"] is None:
            break
        assert body["recipe"]["id"] not in seen
        assert body["remaining"] == count - len(seen) - 1
        seen.append(body["recipe"]["id"])
    assert len(seen) == count
    return set(seen)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------

def test_options_list_only_tags_and_paces_on_pickable_recipes(client, pk):
    o = client.get("/api/picker/options").json()
    by = {cat: {t["name"]: t for t in o[cat]} for cat in ("meal_type", "cooking_style", "main_ingredient")}
    n = pk["names"]
    assert by["meal_type"][n["PkMeal1"]]["count"] == 2          # r1, r3 (r4 is a drink)
    assert by["meal_type"][n["PkMeal2"]]["count"] == 1
    assert by["main_ingredient"][n["PkA"]]["count"] == 3        # r1, r2, r6
    assert by["main_ingredient"][n["PkB"]]["count"] == 1
    assert by["main_ingredient"][n["PkA"]]["id"] == pk["tag"]("PkA")
    # Never offered: the cocktail tags, other groups, unused tags.
    assert "Cocktails" not in by["meal_type"]
    assert not any(t["subgroup"] == "cocktail" for t in o["cooking_style"])
    assert {"Shaken", "Stirred", "Built", "Blended"}.isdisjoint(by["cooking_style"])
    assert set(o) == {"meal_type", "cooking_style", "main_ingredient", "pace", "total", "main_url"}
    assert all(t["count"] > 0 for cat in by.values() for t in cat.values())
    assert [p["value"] for p in o["pace"]] == [v for v in ("quick", "moderate", "long")
                                               if v in {p["value"] for p in o["pace"]}]


def test_unused_tag_is_not_offered(client, pk):
    name = f"PkUnused{pk['sfx']}"
    r = client.post("/api/tags", json={"name": name, "category": "main_ingredient", "keywords": ""})
    assert r.status_code == 200, r.text
    try:
        o = client.get("/api/picker/options").json()
        assert name not in {t["name"] for t in o["main_ingredient"]}
    finally:
        client.delete(f"/api/tags/{r.json()['id']}")


def test_options_order_built_in_first_then_added_alphabetically(client, pk):
    meal = [t["name"] for t in client.get("/api/picker/options").json()["meal_type"]]
    n = pk["names"]
    assert meal.index("Breakfast") < meal.index("Dinner") < meal.index(n["PkMeal1"]) < meal.index(n["PkMeal2"])
    added = [m for m in meal if m not in {"Breakfast", "Brunch", "Lunch", "Dinner", "Snack"}]
    assert added == sorted(added, key=str.casefold)


def test_cocktails_count_towards_nothing(client, pk):
    before = client.get("/api/picker/options").json()
    long_count = lambda o: next((p["count"] for p in o["pace"] if p["value"] == "long"), 0)
    r = client.post("/api/recipes", json={"title": "Pk long drink", "source_type": "manual", "ingredients": [],
                                         "steps": ["Stir."], "tags": [_tag("Cocktails", "meal_type")]})
    rid = r.json()["id"]
    try:
        client.patch(f"/api/recipes/{rid}/rating", json={"cook_time_rating": "long"})
        after = client.get("/api/picker/options").json()
        assert long_count(after) == long_count(before)
        assert after["total"] == before["total"]
    finally:
        client.delete(f"/api/recipes/{rid}")


# ---------------------------------------------------------------------------
# Matching and cocktails
# ---------------------------------------------------------------------------

def test_or_within_a_category_and_across_categories(client, pk):
    ids, tag = pk["ids"], pk["tag"]
    both_meals = {"meal_type": [tag("PkMeal1"), tag("PkMeal2")], "main_ingredient": [tag("PkA")]}
    assert all_matches(client, both_meals) == {ids["r1"], ids["r2"]}
    # A category with nothing chosen matches recipes with no tag in it (r6).
    assert all_matches(client, {"main_ingredient": [tag("PkA")]}) == {ids["r1"], ids["r2"], ids["r6"]}
    assert all_matches(client, {"main_ingredient": [tag("PkA"), tag("PkB")], "meal_type": [tag("PkMeal1")]}) \
        == {ids["r1"], ids["r3"]}


def test_pace_is_or_within_and_excludes_unrated(client, pk):
    ids, tag = pk["ids"], pk["tag"]
    a = {"main_ingredient": [tag("PkA")]}
    assert all_matches(client, {**a, "pace": ["quick"]}) == {ids["r1"]}           # r4 is a drink
    assert all_matches(client, {**a, "pace": ["quick", "moderate"]}) == {ids["r1"], ids["r2"]}   # not r6


def test_cocktails_are_never_picked(client, pk):
    ids = pk["ids"]
    drinks = {ids["r4"], ids["r5"]}
    assert not drinks & all_matches(client, {})
    assert not drinks & all_matches(client, {"main_ingredient": [pk["tag"]("PkA")], "pace": ["quick"]})
    # Choosing the cocktail tags themselves finds nothing.
    assert client.get("/api/picker/random", params={"meal_type": [pk["tags"]["Cocktails"]]}).json()["match_count"] == 0


def test_result_shape(client, pk):
    ids, tag = pk["ids"], pk["tag"]
    body = client.get("/api/picker/random", params={"meal_type": [tag("PkMeal1"), tag("PkMeal2")],
                                                    "main_ingredient": [tag("PkA")],
                                                    "exclude": [ids["r2"]]}).json()
    rec = body["recipe"]
    assert rec["id"] == ids["r1"] and rec["cook_time_rating"] == "quick" and rec["image_path"] is None
    assert rec["tags"] == [{"name": pk["names"]["PkMeal1"], "category": "meal_type"},
                           {"name": pk["names"]["PkA"], "category": "main_ingredient"}]
    assert body["shuffle_titles"] == [f"Pk two {pk['sfx']}"]     # the other match, never the pick
    assert body["match_count"] == 2 and body["remaining"] == 0 and body["exhausted"] is False


def test_shuffle_titles_are_at_most_eight_others(client, pk):
    tag = _tag(f"PkMany{pk['sfx']}", "main_ingredient")
    made = []
    try:
        for i in range(10):
            r = client.post("/api/recipes", json={"title": f"Pk many {i}", "source_type": "manual",
                                                 "ingredients": [], "steps": ["Cook."], "tags": [tag]})
            made.append(r.json()["id"])
        tag_id = next(t["id"] for t in client.get("/api/tags").json() if t["name"] == tag["name"])
        for _ in range(5):
            body = client.get("/api/picker/random", params={"main_ingredient": [tag_id]}).json()
            assert body["match_count"] == 10 and len(body["shuffle_titles"]) == 8
            assert body["recipe"]["title"] not in body["shuffle_titles"]      # the titles are all different here
            assert set(body["shuffle_titles"]) <= {f"Pk many {i}" for i in range(10)}
    finally:
        for rid in made:
            client.delete(f"/api/recipes/{rid}")
    one = client.get("/api/picker/random", params={"main_ingredient": [pk["tag"]("PkB")]}).json()
    assert one["match_count"] == 1 and one["shuffle_titles"] == []


def test_unsafe_stored_photo_name_is_not_sent(client, pk):
    from app import models
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        db.get(models.Recipe, pk["ids"]["r3"]).image_path = "../recipes.db"
        db.commit()
        body = client.get("/api/picker/random", params={"main_ingredient": [pk["tag"]("PkB")]}).json()
        assert body["recipe"]["id"] == pk["ids"]["r3"] and body["recipe"]["image_path"] is None
    finally:
        db.get(models.Recipe, pk["ids"]["r3"]).image_path = None
        db.commit()
        db.close()


# ---------------------------------------------------------------------------
# Weighting
# ---------------------------------------------------------------------------

def test_tastier_recipes_come_up_slightly_more_often():
    from app import picker
    rng = random.Random(1234)
    candidates = [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, None)]
    counts = defaultdict(int)
    for _ in range(60000):
        counts[picker.weighted_choice(candidates, rng)] += 1
    assert all(counts[i] > 0 for i, _ in candidates)          # nothing is ruled out
    assert counts[5] > counts[4] > counts[3] > counts[2] > counts[1]
    assert 1.35 < counts[5] / counts[1] < 1.65                # 1.2 / 0.8 = 1.5
    assert 1.1 < counts[5] / counts[6] < 1.3                  # 1.2 / 1.0
    assert set(picker.TASTINESS_WEIGHTS) == {None, 1, 2, 3, 4, 5}
    assert min(picker.TASTINESS_WEIGHTS.values()) > 0


# ---------------------------------------------------------------------------
# Rounds and bad input
# ---------------------------------------------------------------------------

def test_exclusion_exhaustion_and_no_match(client, pk):
    ids, tag = pk["ids"], pk["tag"]
    two = {"meal_type": [tag("PkMeal1"), tag("PkMeal2")], "main_ingredient": [tag("PkA")]}
    for _ in range(10):
        assert client.get("/api/picker/random", params={**two, "exclude": [ids["r1"]]}).json()["recipe"]["id"] == ids["r2"]
    body = client.get("/api/picker/random", params={**two, "exclude": [ids["r1"], ids["r2"]]}).json()
    assert body == {"recipe": None, "shuffle_titles": [], "match_count": 2, "remaining": 0, "exhausted": True}
    body = client.get("/api/picker/random", params={"meal_type": [tag("PkMeal2")],
                                                    "main_ingredient": [tag("PkB")]}).json()
    assert body == {"recipe": None, "shuffle_titles": [], "match_count": 0, "remaining": 0, "exhausted": False}


def test_bad_input(client, pk):
    tag = pk["tag"]
    r = client.get("/api/picker/random", params={"meal_type": [tag("PkA")]})        # wrong category
    assert r.status_code == 422
    assert client.get("/api/picker/random", params={"meal_type": [tag("PkStyle")]}).status_code == 422
    assert client.get("/api/picker/random", params={"pace": ["slow"]}).status_code == 422
    assert client.get("/api/picker/random", params={"meal_type": ["x"]}).status_code == 422
    # An unknown id is ignored: the category is as if nothing were chosen.
    a = {"main_ingredient": [tag("PkA")]}
    assert all_matches(client, {**a, "meal_type": [999999999]}) == all_matches(client, a)


def test_wrong_category_message_in_french(client, pk):
    r = client.get("/api/picker/random", params={"meal_type": [pk["tag"]("PkA")]}, headers={"X-App-Lang": "fr"})
    assert r.status_code == 422 and "catégorie" in r.json()["detail"]


def test_exclude_is_capped(client, pk):
    ids, tag = pk["ids"], pk["tag"]
    two = {"meal_type": [tag("PkMeal1"), tag("PkMeal2")], "main_ingredient": [tag("PkA")]}
    from app import picker
    padding = list(range(10**9, 10**9 + picker.MAX_EXCLUDE))
    body = client.get("/api/picker/random", params={**two, "exclude": padding + [ids["r1"], ids["r2"]]}).json()
    assert body["exhausted"] is False and body["recipe"]["id"] in {ids["r1"], ids["r2"]}


# ---------------------------------------------------------------------------
# The picker port
# ---------------------------------------------------------------------------

def test_picker_port_serves_only_the_picker(picker_client):
    c = picker_client
    for method, path in [
        ("GET", "/api/recipes"), ("POST", "/api/recipes"), ("GET", "/api/recipes/1"),
        ("DELETE", "/api/recipes/1"), ("PUT", "/api/recipes/1"), ("PATCH", "/api/recipes/1/rating"),
        ("POST", "/api/recipes/batch-delete"), ("POST", "/api/ingest/url"), ("POST", "/api/tags"),
        ("GET", "/api/tags"), ("GET", "/api/backup/info"), ("GET", "/api/backup/database.zip"),
        ("GET", "/api/logs"), ("DELETE", "/api/logs"), ("GET", "/api/email-settings"),
        ("PUT", "/api/email-settings"), ("GET", "/api/https"), ("POST", "/api/https/setup"),
        ("POST", "/api/admin/password"), ("GET", "/api/admin/status"), ("GET", "/api/learning"),
        ("PUT", "/api/picker/settings"), ("POST", "/api/picker/random"), ("GET", "/api/version"),
        ("GET", "/index.html"), ("GET", "/js/app.js"), ("GET", "/css/styles.css"), ("GET", "/manifest.json"),
        ("GET", "/service-worker.js"), ("GET", "/tmp-preview/x.jpg"), ("GET", "/docs"), ("GET", "/openapi.json"),
    ]:
        r = c.request(method, path, headers={"X-Requested-With": "OpenThePantry"}, json={})
        assert r.status_code == 404, (method, path, r.status_code)
    assert c.get("/healthz").json() == {"status": "ok"}
    page = c.get("/")
    assert page.status_code == 200 and "picker.js" in page.text and "js/app.js" not in page.text
    assert c.get("/js/i18n.js").status_code == 200
    for f in ("picker.js", "picker.css", "manifest.json", "icons/icon-192.png", "icons/icon-512.png"):
        assert c.get(f"/picker-static/{f}").status_code == 200, f
    assert c.get("/picker-static/picker.js").headers["cache-control"] == "no-cache"
    for path in ("/api/picker/options", "/api/picker/random", "/api/picker/status"):
        assert c.get(path).status_code == 200, path


def test_host_check_and_rate_limit_apply_on_the_picker_port(picker_client, monkeypatch):
    from app import main
    c = picker_client
    assert c.get("/api/picker/options", headers={"Host": "evil.example.com"}).status_code == 400
    assert c.get("/healthz", headers={"Host": "evil.example.com"}).status_code == 200
    monkeypatch.setattr(main, "_request_log", defaultdict(__import__("collections").deque))
    monkeypatch.setattr(main, "RATE_LIMIT_MAX_REQUESTS", 3)
    codes = [c.get("/api/picker/options").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_api_key_guards_the_main_port_only(client, picker_client, monkeypatch):
    from app import main
    monkeypatch.setattr(main, "API_KEY", "a-long-test-key")
    assert picker_client.get("/api/picker/options").status_code == 200
    assert picker_client.get("/api/picker/random").status_code == 200
    assert client.get("/api/picker/options").status_code == 401
    assert client.get("/api/picker/options", headers={"X-API-Key": "a-long-test-key"}).status_code == 200


def test_photos_on_the_picker_port(picker_client):
    from app.database import UPLOADS_DIR
    c = picker_client
    name = f"img-{uuid.uuid4().hex}.png"
    pdf = f"pdf-{uuid.uuid4().hex}.pdf"
    with open(os.path.join(UPLOADS_DIR, name), "wb") as f:
        f.write(TINY_PNG)
    with open(os.path.join(UPLOADS_DIR, pdf), "wb") as f:
        f.write(b"%PDF-1.4\n")
    try:
        r = c.get(f"/uploads/{name}")
        assert r.status_code == 200 and r.content == TINY_PNG
        assert r.headers["content-type"] == "image/png" and r.headers["x-content-type-options"] == "nosniff"
        for bad in ("recipes.db", "..%2Frecipes.db", "%2E%2E%2Frecipes.db", pdf,
                    f"img-{uuid.uuid4().hex}.png", "img-x.png", "IMG-" + name[4:]):
            assert c.get(f"/uploads/{bad}").status_code == 404, bad
        assert c.get("/uploads/../recipes.db").status_code == 404
    finally:
        os.remove(os.path.join(UPLOADS_DIR, name))
        os.remove(os.path.join(UPLOADS_DIR, pdf))


# ---------------------------------------------------------------------------
# The switch and the listener
# ---------------------------------------------------------------------------

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


@pytest.fixture
def picker_port(client, monkeypatch):
    from app import picker
    port = _free_port()
    monkeypatch.setattr(picker, "LISTEN_PORT", port)
    monkeypatch.setattr(picker, "PICKER_PUBLIC_PORT", port)
    yield port
    client.put("/api/picker/settings", json={"enabled": False})


def test_off_by_default(client):
    from app import models, picker
    from app.database import engine, Base
    # A database from before this change has no table; create_all adds it, off.
    models.PickerSettings.__table__.drop(engine)
    Base.metadata.create_all(bind=engine)
    assert picker.saved_enabled() is False
    st = client.get("/api/picker/status").json()
    assert st["enabled"] is False and st["on"] is False and st["url"] is None
    assert picker._server is None and not picker.running()
    client.portal.call(picker.startup)         # a start-up with the switch off starts nothing
    assert picker._server is None


def test_switch_starts_and_stops_the_listener(client, picker_port):
    from app import picker
    st = client.put("/api/picker/settings", json={"enabled": True}).json()
    assert st["enabled"] is True and st["on"] is True and st["listen_error"] is None
    assert st["url"] == f"http://testserver:{picker_port}/"
    assert client.get("/api/picker/status").json()["enabled"] is True
    # A real request to the real listener.
    assert _get(f"http://127.0.0.1:{picker_port}/healthz")[0] == 200
    status, body = _get(f"http://127.0.0.1:{picker_port}/api/picker/options")
    assert status == 200 and b'"total"' in body
    assert _get(f"http://127.0.0.1:{picker_port}/api/recipes")[0] == 404
    st = client.put("/api/picker/settings", json={"enabled": False}).json()
    assert st == {**st, "enabled": False, "on": False, "url": None}
    assert picker._server is None
    with pytest.raises(OSError):
        _get(f"http://127.0.0.1:{picker_port}/healthz")


def test_setting_survives_a_restart_and_is_in_the_backup(client, picker_port):
    from app import picker
    client.put("/api/picker/settings", json={"enabled": True})
    client.portal.call(picker.stop_server)      # the process ends; the saved switch stays on
    assert picker.saved_enabled() is True and not picker.running()
    client.portal.call(picker.startup)
    assert picker.running() and client.get("/api/picker/status").json()["enabled"] is True
    res = client.get("/api/backup/database.zip")
    assert res.status_code == 200
    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
        data = z.read("recipes.db")
    path = os.path.join(os.environ["RECIPE_APP_DATA_DIR"], f"picker-check-{uuid.uuid4().hex}.db")
    try:
        with open(path, "wb") as f:
            f.write(data)
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT enabled FROM picker_settings WHERE id = 1").fetchone() == (1,)
    finally:
        os.remove(path)


def test_switch_needs_no_settings_password_but_needs_the_header(client):
    from app.main import app
    stranger = TestClient(app)          # no settings-password cookie
    assert stranger.get("/api/picker/status").status_code == 200
    assert stranger.put("/api/picker/settings", json={"enabled": False}).status_code == 403
    r = stranger.put("/api/picker/settings", json={"enabled": False}, headers={"X-Requested-With": "x"})
    assert r.status_code == 200 and r.json()["on"] is False
    assert client.put("/api/picker/settings", json={"enabled": "maybe"}).status_code == 422


def test_busy_port_leaves_the_switch_on_and_the_app_running(client, picker_port):
    from app import picker
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", picker_port))
    blocker.listen()
    try:
        st = client.put("/api/picker/settings", json={"enabled": True}).json()
        assert st["on"] is True and st["enabled"] is False and st["url"] is None
        assert st["listen_error"] == "port_in_use"
        assert client.get("/healthz").status_code == 200        # the main app carries on
        assert client.get("/api/picker/status").json()["enabled"] is False
    finally:
        blocker.close()


def test_busy_port_at_startup_does_not_stop_the_app(monkeypatch):
    """Start-up path: uvicorn exits the process when it can't bind; for the
    picker that must only end the picker."""
    from app import picker
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen()
    monkeypatch.setattr(picker, "LISTEN_PORT", blocker.getsockname()[1])
    monkeypatch.setattr(picker, "saved_enabled", lambda: True)

    async def go():
        await picker.startup()
        await asyncio.sleep(0.2)
        return picker._server, picker._listen_error

    try:
        assert asyncio.run(go()) == (None, "port_in_use")
    finally:
        blocker.close()
        picker._listen_error = None


# ---------------------------------------------------------------------------
# Links between the two pages
# ---------------------------------------------------------------------------

def test_picker_address(client, monkeypatch):
    from app import picker
    monkeypatch.setattr(picker, "saved_enabled", lambda: True)
    monkeypatch.setattr(picker, "running", lambda: True)
    monkeypatch.setattr(picker, "PICKER_PUBLIC_PORT", 9000)
    assert client.get("/api/picker/status").json()["url"] == "http://testserver:9000/"
    # Always plain http, even when the main app was opened over https.
    secure = TestClient(client.app, base_url="https://pantry.local:8443")
    assert secure.get("/api/picker/status").json()["url"] == "http://pantry.local:9000/"
    v6 = TestClient(client.app, base_url="http://[fd00::5]:8090")
    assert v6.get("/api/picker/status").json()["url"] == "http://[fd00::5]:9000/"
    monkeypatch.setattr(picker, "PICKER_PUBLIC_URL", "https://picker.example.com")
    assert client.get("/api/picker/status").json()["url"] == "https://picker.example.com/"
    monkeypatch.setattr(picker, "running", lambda: False)
    st = client.get("/api/picker/status").json()
    assert st["enabled"] is False and st["url"] is None


def test_main_app_address(picker_client, monkeypatch):
    from app import picker
    url = lambda: picker_client.get("/api/picker/options").json()["main_url"]
    assert url() == "http://testserver:8090"
    monkeypatch.setattr(picker, "MAIN_PUBLIC_PORT", 8095)
    assert url() == "http://testserver:8095"
    monkeypatch.setattr(picker.https_setup, "status", lambda: {"enabled": True, "url": "https://pantry.example.com:8443",
                                                               "listening": True})
    assert url() == "https://pantry.example.com:8443"
    monkeypatch.setenv("RECIPE_APP_HTTPS_URL", "https://pantry.example.com")
    assert url() == "https://pantry.example.com"
    monkeypatch.setattr(picker, "MAIN_PUBLIC_URL", "https://pantry.example.com:9443")
    assert url() == "https://pantry.example.com:9443"


def test_https_listener_down_means_plain_http_link(picker_client, monkeypatch):
    from app import picker
    monkeypatch.delenv("RECIPE_APP_HTTPS_URL", raising=False)
    monkeypatch.setattr(picker.https_setup, "status", lambda: {"enabled": True, "url": "https://pantry.example.com:8443",
                                                               "listening": False})
    assert picker_client.get("/api/picker/options").json()["main_url"] == "http://testserver:8090"


def test_only_web_addresses_are_used(monkeypatch):
    from app import picker
    for bad in ("javascript:alert(1)", "ftp://x", "pantry.example.com", "http://"):
        monkeypatch.setenv("RECIPE_APP_PUBLIC_URL", bad)
        assert picker._web_address("RECIPE_APP_PUBLIC_URL") == ""
    monkeypatch.setenv("RECIPE_APP_PUBLIC_URL", "https://pantry.example.com:8443/")
    assert picker._web_address("RECIPE_APP_PUBLIC_URL") == "https://pantry.example.com:8443"
    monkeypatch.setenv("RECIPE_APP_PICKER_PORT", "eighty")
    assert picker._port("RECIPE_APP_PICKER_PORT", 8091) == 8091
    monkeypatch.setenv("RECIPE_APP_PICKER_PORT", "70000")
    assert picker._port("RECIPE_APP_PICKER_PORT", 8091) == 8091
    monkeypatch.setenv("RECIPE_APP_PICKER_PORT", "9091")
    assert picker._port("RECIPE_APP_PICKER_PORT", 8091) == 9091


def test_settings_switch_is_not_behind_the_password():
    from app import admin_lock
    assert not admin_lock.is_locked_path("/api/picker/settings")
    assert not admin_lock.is_locked_path("/api/picker/status")
