"""DeepSeek review of 0132 (2026-10-25): "Moules à la marinière" tagged
Vegetarian (read as a baking tin); one invalid suggested tag failed an
emailed recipe whole; run_setup released a lock it might not hold; a Recipe
under mainEntity was missed; the HTTPS port was unbounded (the lock: test_gpt_review_0134.py); _copy_upload
joined names without safe_join; josepy unpinned."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.mark.parametrize("title", ["Moules à la marinière", "Moules à la crème", "Moules à l'ail",
                                   "Moules au curry"])
def test_mussels_are_fish(title):
    from app.ingestion.tagger import suggest_tags
    names = {t[0] for t in suggest_tags(title, [])}
    assert "Vegetarian" not in names, (title, names)


def test_a_baking_tin_is_still_not_mussels():
    from app.ingestion.tagger import _BAKING_TIN, _fold
    for text in map(_fold, ("moule à gâteau", "moule à tarte", "moules en silicone", "moule de 23 cm")):
        assert _BAKING_TIN.search(text), text
    for text in map(_fold, ("moules à la marinière", "moules à l'ail", "moules a les herbes")):
        assert not _BAKING_TIN.search(text), text


def test_an_invalid_suggested_tag_is_left_off_not_the_recipe(client, monkeypatch):
    from app import main, models
    warned = []
    monkeypatch.setattr(main.log, "warning", lambda msg, *a, **k: warned.append(msg))
    from app.database import SessionLocal
    result = {"title": "Moules emailed", "ingredients": [{"raw_line": "1 kg mussels"}],
              "steps": ["Steam the mussels."], "raw_text": "1 kg mussels\nSteam the mussels.",
              "tags": [{"name": "Fish", "category": "Diet"}, {"name": "x" * 150, "category": "Diet"}]}
    with SessionLocal() as db:
        rid = main._save_email_recipe(db, result, "[RECIPE]")
    try:
        with SessionLocal() as db:
            assert [t.name for t in db.get(models.Recipe, rid).tags] == ["Fish"]
        assert any("left off" in m for m in warned)
    finally:
        client.delete(f"/api/recipes/{rid}")
        client.delete(f"/api/recently-deleted/{rid}")


def test_a_recipe_under_main_entity_is_found():
    from app.ingestion.url_ingest import _try_json_ld
    recipe = {"@type": "Recipe", "name": "Lemon Rice", "recipeIngredient": ["1 cup rice"],
              "recipeInstructions": "Cook the rice."}
    for page in ({"@type": "WebPage", "mainEntity": recipe},
                 {"@type": "WebPage", "mainEntity": [{"@type": "Person"}, recipe]},
                 {"@graph": [{"@type": "WebPage", "mainEntityOfPage": recipe}]},
                 {"@type": "WebPage", "mainEntity": None}):
        html = f'<script type="application/ld+json">{json.dumps(page)}</script>'
        found = _try_json_ld(html)
        if page.get("mainEntity", 1) is None:
            assert found is None
        else:
            assert found and found.title == "Lemon Rice", page


@pytest.mark.parametrize("port,ok", [(8443, True), (1, True), (65535, True), (0, False), (70000, False), (-1, False)])
def test_the_https_port_is_a_port(port, ok):
    from pydantic import ValidationError
    from app import schemas
    body = {"domain": "pantry.example.com", "lan_address": "192.168.1.2", "email": "me@example.com", "port": port}
    if ok:
        assert schemas.HttpsSetupIn(**body).port == port
    else:
        with pytest.raises(ValidationError):
            schemas.HttpsSetupIn(**body)


def test_copy_upload_refuses_a_name_outside_uploads(tmp_path):
    from fastapi import HTTPException
    from app import main
    src = tmp_path / "a.jpg"
    src.write_bytes(b"x")
    with pytest.raises(HTTPException):
        main._copy_upload(str(src), "../escape.jpg", "test")
    assert not (Path(main.UPLOADS_DIR).parent / "escape.jpg").exists()


def test_josepy_is_pinned():
    req = (ROOT / "backend/requirements.txt").read_text(encoding="utf-8")
    assert "\njosepy==" in req
