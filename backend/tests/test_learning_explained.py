"""Where suggestions come from (backlog item 5): each suggested tag says
whether a keyword (which one), the learned model or "no meat word found"
put it there, and Settings -> Learning says why a model isn't in use."""
import random

import pytest

from tests.ml_tag_corpus import tagged_recipes

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_each_suggestion_says_where_it_came_from():
    from app.ingestion import tagger
    args = ("Baked salmon", ["salmon", "lemon"], "Bake 20 minutes.")
    got = {(t["name"], t["category"]): t for t in tagger.explain_tags(*args)}
    assert got[("Fish", "main_ingredient")]["source"] == "keyword"
    assert got[("Fish", "main_ingredient")]["keyword"] == "salmon"
    assert got[("Oven", "cooking_style")]["keyword"] in ("bake", "baked")
    veg = {(t["name"], t["category"]): t for t in tagger.explain_tags("Lemon rice", ["rice", "lemon"], "Simmer.")}
    assert veg[("Vegetarian", "main_ingredient")]["source"] == "no_meat"
    assert veg[("Vegetarian", "main_ingredient")]["keyword"] is None
    try:
        tagger.set_learned_tagger(lambda *a: [("Weeknight", "custom", None), ("Fish", "main_ingredient", None)])
        got = tagger.explain_tags(*args)
        by = {t["name"]: t for t in got}
        assert by["Weeknight"]["source"] == "learned" and by["Fish"]["source"] == "keyword"   # keyword wins
        # Same tags, same order, as suggest_tags.
        assert [(t["name"], t["category"], t["subgroup"]) for t in got] == tagger.suggest_tags(*args)
    finally:
        tagger.set_learned_tagger(None)


def test_a_settings_keyword_is_named_too():
    from app.ingestion import tagger
    try:
        tagger.set_user_keywords([("Sunday roast", "custom", None, ["yorkshire pudding"], False)])
        got = {t["name"]: t for t in tagger.explain_tags("Roast beef", ["beef", "Yorkshire pudding batter"], "")}
        assert got["Sunday roast"] == {"name": "Sunday roast", "category": "custom", "subgroup": None,
                                       "source": "keyword", "keyword": "yorkshire pudding"}
    finally:
        tagger.set_user_keywords([])


def test_the_edit_forms_suggestions_and_the_review_draft_carry_the_source(client):
    from .test_incomplete_sources import WHOLE, _drop, _pdf
    r = client.post("/api/tags/suggest", headers=H, json={"title": "Chicken curry", "ingredients": ["1 chicken"],
                                                          "steps": ["Simmer."], "current_tags": []}).json()
    chicken = next(t for t in r if t["name"] == "Chicken")
    assert chicken["source"] == "keyword" and chicken["keyword"] == "chicken"
    d = client.post("/api/ingest/pdf", headers=H, files={"file": ("w.pdf", _pdf(WHOLE), "application/pdf")}).json()
    try:
        assert d["suggested_tags"] and all(t["source"] in ("keyword", "learned", "no_meat") for t in d["suggested_tags"])
    finally:
        _drop(client, d)


def test_tags_without_a_model_report_how_close_they_came():
    from app.ingestion import tagger
    from app.ml import tags as T
    data = tagged_recipes(80)
    rng = random.Random(3)
    # A tag put on at random: nothing to learn, so no model, with its score.
    data = [(t, i, s, tags | ({("Random", "custom")} if rng.random() < 0.3 else set())) for t, i, s, tags in data]
    ex = [T.TagExample(k, t, ing, st, tags, {(n, c) for n, c, _ in tagger.keyword_suggestions(t, ing, st)})
          for k, (t, ing, st, tags) in enumerate(data)]
    model, report = T.train(ex, {("Pasta night", "custom"): None, ("Curry", "custom"): None, ("Random", "custom"): None})
    assert report["status"] == "active" and "Random" not in report["learned"]
    assert report["precision_needed"] == T.PRECISION and report["not_used_count"] == 1
    (row,) = report["not_used"]
    assert row["name"] == "Random" and (row["precision"] is None or row["precision"] < T.PRECISION)


def test_learning_status_has_the_margin_and_the_recipes_left_out(client):
    from app import models
    from app.database import SessionLocal
    from app.ml import segmenter, service
    _, report = segmenter.train([])
    assert report["min_gain"] == segmenter.MIN_GAIN
    db = SessionLocal()
    r = models.Recipe(title="LE left out", source_type="email", raw_text="x", needs_review=True)
    db.add(r)
    db.commit()
    try:
        status = service._train_locked()
        assert status["left_out_to_review"] >= 1
        assert client.get("/api/learning").json()["left_out_to_review"] >= 1
    finally:
        db.delete(r)
        db.commit()
        db.close()
        from app.ingestion import pdf_ingest, tagger
        pdf_ingest.set_learned_segmenter(None)
        tagger.set_learned_tagger(None)
        import os
        if os.path.isdir(service.ML_DIR):
            for name in os.listdir(service.ML_DIR):
                os.remove(os.path.join(service.ML_DIR, name))
        service._report.clear()
