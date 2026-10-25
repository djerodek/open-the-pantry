"""Claude review round 14 (0125-0128): What to eat?'s Language filter
guessed every recipe's language on every options request and filtered
pick (2 s at 5,000 recipes, on the open port); an email naming a codec
that isn't a text encoding ("base64") still failed whole."""
import email
import uuid

import pytest

H = {"X-Requested-With": "OpenThePantry"}
FR = {"ingredients": [{"raw_line": "1 tasse de farine"}, {"raw_line": "2 cuillères de beurre"}],
      "steps": ["Faire fondre le beurre dans une poêle.", "Ajouter la farine et cuire 10 minutes environ."]}
EN = {"ingredients": [{"raw_line": "1 cup of flour"}, {"raw_line": "2 tablespoons butter"}],
      "steps": ["Melt the butter in a pan.", "Add the flour and stir until smooth."]}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _make(client, title, body):
    r = client.post("/api/recipes", headers=H, json={"title": title, "source_type": "manual", **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_languages_are_guessed_once_until_a_recipe_changes(client, monkeypatch):
    from app import picker
    calls = []
    real = picker.recipe_language
    monkeypatch.setattr(picker, "recipe_language", lambda *a: calls.append(a) or real(*a))
    r = _make(client, f"Soup {uuid.uuid4().hex[:6]}", EN)
    try:
        client.get("/api/picker/options")
        first = len(calls)
        assert first >= 1
        client.get("/api/picker/options")
        client.get("/api/picker/random", params={"language": "fr"})
        assert len(calls) == first                         # nothing guessed again
        assert picker._recipe_languages(_db())[r["id"]] == "en"
        # Edited into French: that recipe alone is guessed again.
        body = {k: r[k] for k in ("title", "source_type")}
        res = client.put(f"/api/recipes/{r['id']}", headers=H, json={**body, **FR})
        assert res.status_code == 200, res.text
        before = len(calls)
        assert picker._recipe_languages(_db())[r["id"]] == "fr"
        assert len(calls) == before + 1
        # Accented text: SQLite's length and Python's agree, so a second
        # call guesses nothing.
        picker._recipe_languages(_db())
        assert len(calls) == before + 1
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)


def test_a_recipe_added_outside_a_request_is_guessed_on_first_use(client):
    """The scheduled email scan saves recipes outside any request."""
    from app import models, picker
    picker._recipe_languages(_db())                        # map filled
    with _db() as db:
        rec = models.Recipe(title="Crêpes", source_type="email",
                            content_text=" ".join([i["raw_line"] for i in FR["ingredients"]] + FR["steps"]))
        db.add(rec)
        db.commit()
        rid = rec.id
    try:
        assert picker._recipe_languages(_db())[rid] == "fr"
    finally:
        client.delete(f"/api/recipes/{rid}", headers=H)


def test_a_hand_set_language_is_read_fresh(client):
    from app import picker
    r = _make(client, f"Pancakes {uuid.uuid4().hex[:6]}", EN)
    try:
        assert picker._recipe_languages(_db())[r["id"]] == "en"
        from app import models
        with _db() as db:                                   # set directly
            db.get(models.Recipe, r["id"]).language = "fr"
            db.commit()
        assert picker._recipe_languages(_db())[r["id"]] == "fr"
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)


def test_a_codec_that_isnt_a_text_encoding_is_read_as_utf8():
    from app.ingestion.email_processing import extract_email_parts
    for charset in ("base64", "hex", "rot13", "unknown-8bit"):
        raw = (f"From: a@example.com\r\nSubject: [RECIPE]\r\nContent-Type: text/plain; charset={charset}\r\n"
               "Content-Transfer-Encoding: 8bit\r\n\r\nCrème brûlée https://example.com/r\r\n").encode("utf-8")
        parts = extract_email_parts(email.message_from_bytes(raw))
        assert "Crème brûlée" in parts["body_text"], charset


def _db():
    from app.database import SessionLocal
    return SessionLocal()


def test_a_restore_forgets_the_guesses(monkeypatch):
    from app import picker
    picker._guessed[999_999] = ((1, 1), "fr")
    picker.forget_languages()
    assert picker._guessed == {}
