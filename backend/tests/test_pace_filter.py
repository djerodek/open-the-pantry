"""Pace filter (the quick / moderate / long rating) on the recipe list.

app.* is imported inside tests only; see the note in test_backup.py.
"""


def _make(client, title, pace):
    r = client.post("/api/recipes", json={"title": title, "source_type": "manual",
                                          "cook_time_rating": pace, "steps": ["x"]})
    assert r.status_code == 200
    return r.json()["id"]


def test_pace_filter_single_and_multiple(client):
    quick = _make(client, "Pace Quick", "quick")
    mod = _make(client, "Pace Moderate", "moderate")
    long_ = _make(client, "Pace Long", "long")
    unrated = _make(client, "Pace Unrated", None)
    ours = {quick, mod, long_, unrated}

    ids = lambda r: {x["id"] for x in r.json()} & ours
    assert ids(client.get("/api/recipes?pace=quick")) == {quick}
    assert ids(client.get("/api/recipes?pace=quick&pace=long")) == {quick, long_}
    assert ids(client.get("/api/recipes")) == ours


def test_pace_filter_combines_with_other_filters(client):
    fav = _make(client, "Pace Fav Quick", "quick")
    assert client.patch(f"/api/recipes/{fav}/rating", json={"favorite": True}).status_code == 200
    other = _make(client, "Pace NotFav Quick", "quick")
    got = {x["id"] for x in client.get("/api/recipes?pace=quick&favorite=true").json()}
    assert fav in got and other not in got
    got = {x["id"] for x in client.get("/api/recipes?pace=quick&q=NotFav").json()}
    assert got == {other}


def test_unknown_pace_value_is_rejected(client):
    assert client.get("/api/recipes?pace=glacial").status_code == 422
