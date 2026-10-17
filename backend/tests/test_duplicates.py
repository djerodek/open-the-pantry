"""Already in your library? (app/duplicates.py)

- Same link (scheme, www., fragment, trailing slash and tracking parameters
  ignored) or same title (case, accents and punctuation ignored).
- GET /api/recipes/duplicates for the review screen and Add manually.
- Unattended adds: a batch link or an emailed link already saved is skipped;
  a title already used is saved with a note.
"""
from unittest.mock import MagicMock, patch

import pytest

from .test_email_ingest import _settings_payload

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_normalize():
    from app.duplicates import normalize_title, normalize_url
    same = ["https://www.example.com/crepes/", "http://example.com/crepes",
            "https://example.com/crepes?utm_source=news&utm_medium=email#comments",
            "https://EXAMPLE.com/crepes/?fbclid=abc"]
    assert len({normalize_url(u) for u in same}) == 1
    assert normalize_url("https://example.com/crepes?id=2") != normalize_url("https://example.com/crepes?id=3")
    assert normalize_url("https://example.com/a?b=2&a=1") == normalize_url("https://example.com/a?a=1&b=2")
    for bad in (None, "", "javascript:alert(1)", "ftp://example.com/x", "not a link"):
        assert normalize_url(bad) is None
    assert normalize_title("Crêpes du Dimanche!") == normalize_title("crepes  du dimanche") == "crepes du dimanche"
    assert normalize_title("!!!") is None


def _make(client, title, url=None):
    r = client.post("/api/recipes", headers=H, json={
        "title": title, "source_type": "url" if url else "manual", "source_url": url,
        "ingredients": [{"raw_line": "1 egg"}], "steps": ["Cook."], "tags": []})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_duplicates_endpoint(client):
    a = _make(client, "Dup Test Pancakes", "https://www.example.com/dup-pancakes/?utm_campaign=x")
    b = _make(client, "Dup Test Waffles")
    try:
        r = client.get("/api/recipes/duplicates", params={"url": "http://example.com/dup-pancakes"})
        assert r.json() == [{"id": a, "title": "Dup Test Pancakes", "reason": "link"}]
        r = client.get("/api/recipes/duplicates", params={"title": "dup test WAFFLES."})
        assert r.json() == [{"id": b, "title": "Dup Test Waffles", "reason": "title"}]
        # A link match comes first, and a recipe matching both is listed once.
        r = client.get("/api/recipes/duplicates", params={"title": "Dup Test Waffles",
                                                          "url": "https://example.com/dup-pancakes"})
        assert [d["id"] for d in r.json()] == [a, b]
        assert client.get("/api/recipes/duplicates", params={"title": "Dup Test Waffles", "exclude": b}).json() == []
        assert client.get("/api/recipes/duplicates").json() == []
        assert client.get("/api/recipes/duplicates", params={"title": "Nothing like it"}).json() == []
        # The route doesn't shadow /api/recipes/{id} or the other way round.
        assert client.get(f"/api/recipes/{a}").json()["title"] == "Dup Test Pancakes"
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [a, b]})


class _Fetched:
    def __init__(self, title):
        self.title, self.ingredients, self.steps = title, ["1 cup flour"], ["Mix."]
        self.servings = self.prep_time = self.cook_time = self.total_time = self.image_url = None
        self.raw_text, self.method = "text", "test"


def test_batch_links_skip_a_saved_link_and_note_a_saved_title(client, monkeypatch):
    from app import main
    a = _make(client, "Dup Batch Stew", "https://example.com/dup-batch-stew")
    fetched = []

    def fake_ingest(url):
        fetched.append(url)
        return _Fetched("Dup Batch Stew" if "other" in url else "Dup Batch Soup")

    monkeypatch.setattr(main, "ingest_url", fake_ingest)
    r = client.post("/api/ingest/url/batch", headers=H, json={"urls": [
        "https://www.example.com/dup-batch-stew/?utm_source=x",   # same link: skipped, never fetched
        "https://example.com/other-stew",                         # same title: saved, noted
        "https://example.com/dup-batch-soup",                     # new
    ]})
    body = r.json()
    try:
        assert body["skipped"] == [{"url": "https://www.example.com/dup-batch-stew/?utm_source=x",
                                    "recipe_id": a, "title": "Dup Batch Stew"}]
        assert fetched == ["https://example.com/other-stew", "https://example.com/dup-batch-soup"]
        notes = {s["title"]: s["duplicate_of"] for s in body["succeeded"]}
        assert notes["Dup Batch Stew"] == {"id": a, "title": "Dup Batch Stew", "reason": "title"}
        assert notes["Dup Batch Soup"] is None
        assert body["failed"] == []
    finally:
        client.post("/api/recipes/batch-delete", headers=H,
                    json={"ids": [a] + [s["recipe_id"] for s in body["succeeded"]]})


def _scan(client, result):
    sent = {}
    with patch("app.main.email_client.connect_imap", return_value=MagicMock()), \
         patch("app.main.email_client.search_unseen_by_subject", return_value=[b"1"]), \
         patch("app.main.email_client.fetch_full_message", return_value=MagicMock(get=lambda k, d="": "[RECIPE] x")), \
         patch("app.main.email_client.decode_subject", return_value="[RECIPE] x"), \
         patch("app.main._sender_allowed", return_value=True), \
         patch("app.main.process_tagged_email", return_value=result), \
         patch("app.main.email_client.mark_seen") as seen, \
         patch("app.main.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.main.email_client.send_email", side_effect=lambda *a: sent.setdefault("body", a[-1])):
        body = client.post("/api/email-settings/scan").json()
    seen.assert_called_once()
    return body, sent.get("body", "")


def test_email_skips_a_saved_link_and_notes_a_saved_title(client, monkeypatch):
    from cryptography.fernet import Fernet
    from app import crypto
    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    client.put("/api/email-settings", json=_settings_payload(enabled=True, password="pw"))
    a = _make(client, "Dup Mail Curry", "https://example.com/dup-mail-curry")
    base = {"success": True, "ingredients": [{"raw_line": "1 onion", "quantity": "1", "unit": None, "name": "onion"}],
            "steps": ["Cook."], "tags": [], "raw_text": "x", "image_path": None, "source_detail": "URL in body (example.com)"}
    made = []
    try:
        before = len(client.get("/api/recipes").json())
        body, mail = _scan(client, {**base, "title": "Dup Mail Curry", "source_url": "https://www.example.com/dup-mail-curry/"})
        assert body["succeeded"] == 0 and body["failed"] == 0
        assert any("already in your library; not saved again" in m for m in body["messages"])
        assert "already in your library" in mail
        assert len(client.get("/api/recipes").json()) == before

        body, _ = _scan(client, {**base, "title": "Dup Mail Curry", "source_url": "https://example.com/another-curry"})
        assert body["succeeded"] == 1
        assert any("a recipe with this title was already in your library" in m for m in body["messages"])
        made = [r["id"] for r in client.get("/api/recipes").json() if r["title"] == "Dup Mail Curry" and r["id"] != a]
        assert len(made) == 1
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [a] + made})
        client.delete("/api/email-settings/password")


def test_result_lines_translate_with_a_note_after_them():
    from app.i18n import translate
    line = 'OK: Pie (from "Fw: pie", via PDF attachment) -- no steps were found; check the recipe'
    assert translate(line, "fr") == "OK : Pie (de « Fw: pie », par la pièce jointe PDF) -- aucune étape trouvée; vérifiez la recette"
    line = 'OK: Pie (from "x", via URL in body (a.com)) -- already in your library; not saved again'
    assert translate(line, "fr") == "OK : Pie (de « x », par l'URL dans le corps (a.com)) -- déjà dans votre bibliothèque; pas enregistrée de nouveau"
