"""Every filter can also leave recipes out (owner, 2026-11-18): tags, pace,
cook time and To review. Something without the property (no pace, no
logged time) is never left out by it."""
import uuid


def _recipe(client, title, **extra):
    body = {"title": title, "source_type": "manual", "ingredients": [], "steps": [], "tags": [], **extra}
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _titles(client, sfx, **params):
    r = client.get("/api/recipes", params={"q": sfx, **params})
    assert r.status_code == 200, r.text
    return {x["title"].split()[0] for x in r.json()}


def test_each_filter_leaves_out_what_it_would_show(client):
    sfx = uuid.uuid4().hex[:8]
    tag = lambda n, c: {"name": n, "category": c, "subgroup": None}  # noqa: E731
    ids = [
        _recipe(client, f"Chicken {sfx}", tags=[tag("Dinner", "meal_type"), tag("Chicken", "main_ingredient")],
                cook_time_rating="quick", actual_cook_time="00:00:15"),
        _recipe(client, f"Beef {sfx}", tags=[tag("Dinner", "meal_type")], cook_time_rating="long",
                actual_cook_time="00:03:00"),
        _recipe(client, f"Plain {sfx}"),                       # no tags, pace or time
    ]
    try:
        everything = {"Chicken", "Beef", "Plain"}
        assert _titles(client, sfx) == everything
        assert _titles(client, sfx, not_tags=["Chicken"]) == {"Beef", "Plain"}
        assert _titles(client, sfx, tags=["Dinner"], not_tags=["Chicken"]) == {"Beef"}
        assert _titles(client, sfx, not_pace=["quick"]) == {"Beef", "Plain"}
        assert _titles(client, sfx, max_minutes=20) == {"Chicken"}
        assert _titles(client, sfx, max_minutes=20, minutes_out="true") == {"Beef", "Plain"}
        from app import models
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            db.get(models.Recipe, ids[1]).needs_review = True
            db.commit()
        finally:
            db.close()
        assert _titles(client, sfx, to_review="true") == {"Beef"}
        assert _titles(client, sfx, to_review="false") == {"Chicken", "Plain"}
    finally:
        client.post("/api/recipes/batch-delete", headers={"X-Requested-With": "XMLHttpRequest"}, json={"ids": ids})


def test_left_out_tags_count_toward_the_limit(client):
    from app.routes.recipes import MAX_TAG_FILTERS
    r = client.get("/api/recipes", params={"tags": ["a"] * 10, "not_tags": ["b"] * (MAX_TAG_FILTERS - 9)})
    assert r.status_code == 422
