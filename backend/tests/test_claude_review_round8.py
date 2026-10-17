"""Claude review round 8 (0078-0085).

An integer outside SQLite's 64-bit range (99999999999999999999) passed
validation as a Python int and raised OverflowError in the query: a 500 and
about 40 KB of traceback per request in the container log, reachable without
the API key on the What to eat? port. Every integer that reaches the
database is now held to SQLite's range (schemas.DbInt / DbId): a 422, and
nothing in the log.
"""
import re

import pytest
from fastapi.testclient import TestClient

BIG = "99999999999999999999"
H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _routes_with_int_path(app):
    for route in app.routes:
        path = getattr(route, "path", "")
        if re.search(r"\{\w+_id\}", path):
            for method in sorted(getattr(route, "methods", None) or ()):
                if method != "HEAD":
                    yield method, re.sub(r"\{\w+_id\}", BIG, path)


def test_every_id_in_a_path_is_checked_on_the_main_port(client):
    found = list(_routes_with_int_path(client.app))
    assert len(found) >= 12
    for method, path in found:
        r = client.request(method, path, headers=H, json={})
        assert r.status_code == 422, (method, path, r.status_code)
    # In range, nothing changes: an unknown id is still a 404.
    assert client.get("/api/recipes/0").status_code == 404
    assert client.get("/api/recipes/-5").status_code == 404
    assert client.get(f"/api/recipes/{2**63 - 1}").status_code == 404


def test_query_and_body_integers_are_checked(client):
    assert client.get(f"/api/recipes?max_minutes={BIG}").status_code == 422
    assert client.get(f"/api/recipes?max_minutes={2**63 - 1}").status_code == 200
    r = client.post("/api/recipes/batch-delete", headers=H, json={"ids": [int(BIG)]})
    assert r.status_code == 422


def test_the_picker_port_answers_422_not_500(client):
    from app.main import picker_app
    pc = TestClient(picker_app)
    for path in (f"/api/picker/recipes/{BIG}",
                 f"/api/picker/random?meal_type={BIG}",
                 f"/api/picker/random?cooking_style={BIG}",
                 f"/api/picker/random?main_ingredient={BIG}",
                 f"/api/picker/random?exclude={BIG}"):
        for c in (client, pc):
            assert c.get(path).status_code == 422, path
    assert pc.get("/api/picker/recipes/0").status_code == 404
    assert pc.get("/api/picker/random?meal_type=999999999").status_code == 200   # unknown id: ignored, as before


def test_no_traceback_is_logged(client):
    from app import main
    seen = []
    orig = main.log.exception
    main.log.exception = lambda *a, **k: seen.append(a)
    try:
        client.get(f"/api/recipes/{BIG}")
        client.get(f"/api/picker/random?meal_type={BIG}")
    finally:
        main.log.exception = orig
    assert seen == []
