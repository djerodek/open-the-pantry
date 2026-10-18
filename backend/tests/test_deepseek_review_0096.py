"""DeepSeek review of 0096 (the reset path is in test_settings_password.py).

3. A short step ("Mix everything.") in a recipe without headings was
   dropped, and the recipe failed as having no steps.
4. An emailed link lost the page's prep, cook and total times.
6. "Salt and pepper to taste:" was shown as a part name.
"""
import pytest


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_short_steps_without_headings():
    from app.ingestion.pdf_ingest import rule_segment_raw_text as seg
    r = seg("Pancakes\n1 cup flour\n2 eggs\nMix everything.\nFry.")
    assert r["ingredients"] == ["1 cup flour", "2 eggs"] and r["steps"] == ["Mix everything.", "Fry."]
    assert seg("Cake\n2 cups flour\n3 eggs\nBake 20 min.")["steps"] == ["Bake 20 min."]
    # A cooking verb at the start of an ingredient line isn't a step.
    r = seg("Steak\n2 lb top sirloin\nTop sirloin trimmings\n1 tsp salt\nGrill it.")
    assert "Top sirloin trimmings" not in r["steps"] and r["steps"] == ["Grill it."]


def test_emailed_link_keeps_its_times(client, monkeypatch, tmp_path):
    from email.message import EmailMessage
    from app.ingestion import email_processing
    from app.ingestion.url_ingest import UrlIngestResult
    result = UrlIngestResult(title="Timed Soup", ingredients=["1 l stock"], steps=["Simmer."], raw_text="x",
                             servings="4", prep_time="10 min", cook_time="PT30M", total_time="40 min")
    result.final_url = "https://example.com/timed-soup"
    monkeypatch.setattr(email_processing, "ingest_url", lambda url: result)
    msg = EmailMessage()
    msg["Subject"] = "[RECIPE] soup"
    msg.set_content("https://example.com/timed-soup")
    out = email_processing.process_tagged_email(msg, str(tmp_path))
    assert out["success"] and (out["prep_time"], out["cook_time"], out["total_time"]) == ("10 min", "PT30M", "40 min")
    from app.main import SessionLocal, _save_email_recipe
    db = SessionLocal()
    try:
        rid = _save_email_recipe(db, out, "soup")
    finally:
        db.close()
    r = client.get(f"/api/recipes/{rid}").json()
    try:
        assert (r["prep_time"], r["cook_time"], r["total_time"]) == ("10 min", "PT30M", "40 min")
    finally:
        client.delete(f"/api/recipes/{rid}", headers={"X-Requested-With": "OpenThePantry"})


def test_an_ingredient_ending_in_a_colon_isnt_a_part_name():
    from app.text_rules import is_part_name
    from app.interchange import _instructions
    assert is_part_name("Sauce:") and is_part_name("Pour la pâte :")
    assert not is_part_name("Salt and pepper to taste:") and not is_part_name("Persil, pour garnir :")
    assert not is_part_name("Just a line") and not is_part_name("one two three four five six seven:")
    assert _instructions(["Salt and pepper to taste:", "Stir."]) == [
        {"@type": "HowToStep", "text": "Salt and pepper to taste:"}, {"@type": "HowToStep", "text": "Stir."}]


def test_the_pages_use_the_same_rule():
    import re
    from pathlib import Path
    from app.text_rules import INGREDIENT_TAIL
    root = Path(__file__).resolve().parents[2]
    for page in (root / "frontend" / "js" / "app.js", root / "frontend-picker" / "picker.js"):
        js = page.read_text(encoding="utf-8")
        m = re.search(r"const notAPart = /(.+)/i;", js)
        assert m and m.group(1) == INGREDIENT_TAIL.pattern, page.name


def test_the_reset_message_has_french(monkeypatch):
    from app import https_setup
    from app.i18n import translate

    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(https_setup, "_stored_login", lambda: {"token_encrypted": "x"})
    monkeypatch.setattr(https_setup, "_write_login", denied)
    monkeypatch.setattr(https_setup.os, "remove", denied)
    with pytest.raises(https_setup.HttpsError) as e:
        https_setup.forget_token()
    fr = translate(str(e.value), "fr")
    assert fr.startswith("Le jeton cPanel") and "(Permission denied)" in fr
