"""The review queue (backlog item 4, app/review.py): recipes saved without
anyone checking them are flagged "to review", left out of learning until
they're checked, and the review view gets hints on what to look at."""
import json
import uuid

import numpy as np
import pytest

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _flagged(client, rid):
    return client.get(f"/api/recipes/{rid}").json()["needs_review"]


def _count(client):
    return client.get("/api/review-queue").json()["count"]


def test_unattended_imports_are_flagged_and_reviewed_ones_are_not(client, monkeypatch):
    from app import main
    from app.database import SessionLocal
    from .test_duplicates import _Fetched
    from .test_incomplete_sources import WHOLE, _pdf
    before = _count(client)
    monkeypatch.setattr(main, "ingest_url", lambda url: _Fetched("RQ link " + url[-6:]))
    sfx = uuid.uuid4().hex[:6]
    links = client.post("/api/ingest/url/batch", headers=H, json={"urls": [f"https://example.com/rq-{sfx}"]}).json()
    pdfs = client.post("/api/ingest/pdf/batch", headers=H,
                       files=[("files", ("whole.pdf", _pdf(WHOLE), "application/pdf"))]).json()
    db = SessionLocal()
    try:
        emailed = main._save_email_recipe(db, {"title": f"RQ email {sfx}", "ingredients": [{"raw_line": "1 egg"}],
                                               "steps": ["Boil."], "tags": [], "raw_text": "RQ\n1 egg\nBoil.",
                                               "ocr_confidence": None, "image_path": None, "source_url": None},
                                          "subject")
    finally:
        db.close()
    typed = client.post("/api/recipes", headers=H, json={"title": f"RQ typed {sfx}", "source_type": "manual",
                                                          "ingredients": [{"raw_line": "1 egg"}], "steps": ["Boil."],
                                                          "tags": []}).json()["id"]
    ids = [links["succeeded"][0]["recipe_id"], pdfs["succeeded"][0]["recipe_id"], emailed]
    try:
        assert all(_flagged(client, i) for i in ids) and not _flagged(client, typed)
        assert _count(client) == before + 3
        listed = {r["id"]: r["needs_review"] for r in client.get("/api/recipes", params={"to_review": "true"}).json()}
        assert set(ids) <= set(listed) and typed not in listed and all(listed.values())
        # "Looks right" and a save from Edit both clear it.
        assert client.post(f"/api/recipes/{ids[0]}/reviewed", headers=H).json()["needs_review"] is False
        r = client.get(f"/api/recipes/{ids[1]}").json()
        put = {k: r[k] for k in ("title", "source_type", "servings", "raw_text", "ocr_confidence")}
        put.update(ingredients=[{"raw_line": i["raw_line"]} for i in r["ingredients"]],
                   steps=[s["text"] for s in r["steps"]], tags=[])
        assert client.put(f"/api/recipes/{ids[1]}", headers=H, json=put).json()["needs_review"] is False
        assert _count(client) == before + 1
        assert client.post("/api/recipes/999999999/reviewed", headers=H).status_code == 404
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": ids + [typed]})


def test_email_scan_lists_what_it_saved(client, monkeypatch):
    """The scan's results in Settings name the recipes it added, so each can
    be deleted there."""
    from cryptography.fernet import Fernet
    from app import crypto
    from .test_duplicates import _scan, _settings_payload
    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(enabled=True, password="pw"))
    sfx = uuid.uuid4().hex[:6]
    body, _ = _scan(client, {"success": True, "title": f"RQ scan {sfx}", "source_url": None,
                             "ingredients": [{"raw_line": "1 onion", "quantity": "1", "unit": None, "name": "onion"}],
                             "steps": ["Cook."], "tags": [], "raw_text": "x", "image_path": None,
                             "source_detail": "email body"})
    try:
        assert [s["title"] for s in body["saved"]] == [f"RQ scan {sfx}"]
        assert _flagged(client, body["saved"][0]["recipe_id"])
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [s["recipe_id"] for s in body["saved"]]})
        client.delete("/api/email-settings/password")


def test_hints(client):
    from app import review
    from app.database import SessionLocal
    from app import models
    db = SessionLocal()
    try:
        r = models.Recipe(title="Hint Soup", source_type="pdf", raw_text="Hint Soup\n...", ocr_confidence=61.4,
                          needs_review=True)
        db.add(r)
        db.flush()
        lines = [("2 carrots", "2"), ("Sauce:", None), ("salt, to taste", None), ("onion", None), ("one leek", None)]
        for k, (line, qty) in enumerate(lines):
            db.add(models.Ingredient(recipe_id=r.id, position=k, raw_line=line, name=line, quantity=qty))
        for k, text in enumerate(["Chop.", "Simmer the vegetables for twenty minutes.", "x" * 800]):
            db.add(models.Step(recipe_id=r.id, position=k, text=text))
        db.commit()
        db.refresh(r)
        h = review.hints(r)
        assert h["title_guessed"] and h["servings_missing"] and h["ocr_confidence"] == 61
        assert [(i["index"], i["reason"]) for i in h["ingredients"]] == [(3, "no_amount")]
        assert [(s["index"], s["reason"]) for s in h["steps"]] == [(0, "short"), (2, "long")]
        assert h["line_scores"] is False
        # Lines the learned segmenter wasn't sure of.
        h = review.hints(r, uncertain={"2 carrots", "Simmer the vegetables for twenty minutes."})
        assert (0, "uncertain") in [(i["index"], i["reason"]) for i in h["ingredients"]]
        assert (1, "uncertain") in [(s["index"], s["reason"]) for s in h["steps"]] and h["line_scores"]
        # From the endpoint (no learned model in the test suite).
        got = client.get(f"/api/recipes/{r.id}/review-hints").json()
        assert got["needs_review"] and got["line_scores"] is False and got["ocr_confidence"] == 61
        # A web page's recipe data: the title wasn't guessed.
        r.source_type, r.source_url = "url", "https://example.com/hint"
        db.commit()
        assert client.get(f"/api/recipes/{r.id}/review-hints").json()["title_guessed"] is False
        assert client.get("/api/recipes/999999999/review-hints").status_code == 404
    finally:
        db.delete(r)
        db.commit()
        db.close()


def test_uncertain_lines_come_from_the_learned_segmenter(monkeypatch):
    from app.ml import segmenter, service
    assert service.uncertain_lines("a\nb") is None           # no model in use: no scores

    class Fake:
        def label(self, lines, rules):
            probs = np.full((len(lines), 4), 0.1)
            path = []
            for i, l in enumerate(lines):
                y = segmenter.ING if "egg" in l else segmenter.STEP
                probs[i, y] = 0.9 if "sure" in l else 0.4
                path.append(y)
            return path, probs
    monkeypatch.setattr(service, "_seg_model", Fake())
    got = service.uncertain_lines("2 eggs, sure\n1 egg white\nWhisk them, sure.\nFold gently.")
    assert got == {"1 egg white", "Fold gently."}


def test_learning_leaves_out_recipes_still_to_review(client):
    from app import models
    from app.database import SessionLocal
    from app.ml import service
    db = SessionLocal()
    try:
        a = models.Recipe(title="RQ learn checked", source_type="pdf", raw_text="x")
        b = models.Recipe(title="RQ learn unchecked", source_type="pdf", raw_text="x", needs_review=True)
        db.add_all([a, b])
        db.commit()
        seg, tags, _ = service._examples(db)
        ids = {e.recipe_id for e in seg} | {e.recipe_id for e in tags}
        assert a.id in ids and b.id not in ids
        b.needs_review = False
        db.commit()
        seg, tags, _ = service._examples(db)
        assert b.id in {e.recipe_id for e in seg} and b.id in {e.recipe_id for e in tags}
    finally:
        db.delete(a)
        db.delete(b)
        db.commit()
        db.close()


def test_json_export_keeps_the_flag(client):
    import io
    import zipfile
    from app import models
    from app.database import SessionLocal
    sfx = uuid.uuid4().hex[:6]
    db = SessionLocal()
    r = models.Recipe(title=f"RQ json {sfx}", source_type="email", raw_text="x", needs_review=True,
                      source_url=f"https://example.com/rq-json-{sfx}")
    db.add(r)
    db.commit()
    rid = r.id
    db.add(models.Ingredient(recipe_id=rid, position=0, raw_line="1 egg"))
    db.add(models.Step(recipe_id=rid, position=0, text="Boil."))
    db.commit()
    db.close()
    doc = client.get(f"/api/recipes/{rid}/export.json").json()
    assert doc["x-open-the-pantry"]["to_review"] is True
    client.delete(f"/api/recipes/{rid}", headers=H)
    res = client.post("/api/import/recipes", headers=H,
                      files={"file": ("r.json", json.dumps(doc).encode(), "application/json")}).json()
    new = res["succeeded"][0]["recipe_id"]
    try:
        assert _flagged(client, new)
    finally:
        client.delete(f"/api/recipes/{new}", headers=H)
