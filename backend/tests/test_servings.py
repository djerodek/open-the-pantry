"""Servings: typed in by hand, shown on the recipe, never filtered on."""


def _make(client, payload, **extra):
    r = client.post("/api/recipes", json={**payload, **extra})
    assert r.status_code in (200, 201), r.text
    return r.json()


def test_servings_saved_on_create_and_shown(client, sample_recipe_payload):
    rec = _make(client, sample_recipe_payload, title="Servings on create", servings="4-6")
    assert client.get(f"/api/recipes/{rec['id']}").json()["servings"] == "4-6"


def test_servings_set_changed_and_cleared_alone(client, sample_recipe_payload):
    rec = _make(client, sample_recipe_payload, title="Servings patch")
    client.patch(f"/api/recipes/{rec['id']}/rating", json={"tastiness_rating": 4})
    r = client.patch(f"/api/recipes/{rec['id']}/rating", json={"servings": "  6  "})
    assert r.status_code == 200 and r.json()["servings"] == "6"
    assert r.json()["tastiness_rating"] == 4, "setting servings must leave the ratings alone"
    # Another quick control leaves servings alone.
    r = client.patch(f"/api/recipes/{rec['id']}/rating", json={"favorite": True})
    assert r.json()["servings"] == "6"
    for clear in ("", "   ", None):
        client.patch(f"/api/recipes/{rec['id']}/rating", json={"servings": "2"})
        r = client.patch(f"/api/recipes/{rec['id']}/rating", json={"servings": clear})
        assert r.status_code == 200 and r.json()["servings"] is None


def test_servings_length_is_capped(client, sample_recipe_payload):
    rec = _make(client, sample_recipe_payload, title="Servings cap")
    r = client.patch(f"/api/recipes/{rec['id']}/rating", json={"servings": "x" * 101})
    assert r.status_code == 422
    assert client.get(f"/api/recipes/{rec['id']}").json()["servings"] is None


def test_servings_is_not_a_filter_or_search_term(client, sample_recipe_payload):
    _make(client, sample_recipe_payload, title="Serves a crowd", servings="zqxservings")
    r = client.get("/api/recipes", params={"q": "zqxservings"})
    assert r.status_code == 200
    items = r.json()["items"] if isinstance(r.json(), dict) else r.json()
    assert items == []


def test_export_shows_servings_and_real_world_cook_time(client, sample_recipe_payload):
    from tests.test_friday_batch import FR
    rec = _make(client, sample_recipe_payload, title="Export facts")
    client.patch(f"/api/recipes/{rec['id']}/rating", json={"servings": "4-6", "actual_cook_time": "01:04:15"})
    en = client.get(f"/api/recipes/{rec['id']}/export.html").text
    fr = client.get(f"/api/recipes/{rec['id']}/export.html", headers=FR).text
    assert "Servings: 4-6" in en
    assert "Real-world cook time: 1 d 4 h 15 min" in en
    assert "Temps de cuisson réel : 1 j 4 h 15 min" in fr
    pdf = client.get(f"/api/recipes/{rec['id']}/export.pdf")
    assert pdf.status_code == 200 and pdf.content[:4] == b"%PDF"
    # Not logged: no line.
    client.patch(f"/api/recipes/{rec['id']}/rating", json={"actual_cook_time": ""})
    assert "Real-world cook time" not in client.get(f"/api/recipes/{rec['id']}/export.html").text


def test_export_cook_time_label_parts():
    from app.export import cook_time_label
    assert cook_time_label(None, "en") is None
    assert cook_time_label(45, "en") == "45 min"
    assert cook_time_label(120, "en") == "2 h"
    assert cook_time_label(24 * 60 + 5, "fr") == "1 j 5 min"
