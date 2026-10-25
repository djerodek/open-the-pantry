"""Import rules from the owner's import corpus (2026-10-25): 27 of their
own PDF, photo and email recipes, read again and compared with what they
saved. Each test is one rule, on text written here, not theirs."""
import pytest


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def seg(*lines):
    from app.ingestion.pdf_ingest import rule_segment_raw_text
    return rule_segment_raw_text("\n".join(lines))


ING = ["Ingredients", "2 cups flour", "1 tsp salt"]


def test_procedure_is_a_steps_heading():
    s = seg("Plain Bread", *ING, "Procedure", "Mix the flour and salt.", "Bake for 30 minutes.")
    assert s["ingredients"] == ["2 cups flour", "1 tsp salt"]
    assert s["steps"] == ["Mix the flour and salt.", "Bake for 30 minutes."]
    assert seg("Pain", "Ingrédients", "2 tasses de farine", "Procédure", "Mélanger.")["steps"] == ["Mélanger."]


@pytest.mark.parametrize("tail", [
    ["Kitchen Tips> Use bread flour for a chewier loaf."], ["Kitchen Tips", "Use bread flour."],
    ["Variation", "Add herbs."], ["© A Test Kitchen – #Bread"], ["© COPYRIGHT 2026 A TEST KITCHEN"],
    ["RECIPE TYPES Breads (7)", "Desserts (34)"],
])
def test_the_steps_end_at_tips_variations_and_the_footer(tail):
    s = seg("Plain Bread", *ING, "Method", "Mix the flour and salt.", "Bake for 30 minutes.", *tail)
    assert s["steps"] == ["Mix the flour and salt.", "Bake for 30 minutes."]


def test_a_circled_step_number_read_as_copyright_is_still_a_step():
    s = seg("Plain Bread", *ING, "Method", "Mix the flour and salt.", "© Bake for 30 minutes.")
    assert s["steps"][-1].endswith("Bake for 30 minutes.")


@pytest.mark.parametrize("lines,title", [
    (["Sheet-Pan Lemon-", "Garlic Chicken"], "Sheet-Pan Lemon-Garlic Chicken"),
    (["Pork Chops with Apples", "and Bacon"], "Pork Chops with Apples and Bacon"),
    (["DESSERTS", "WARM SPICED", "PEARS", "BY A TEST KITCHEN"], "Warm Spiced Pears"),
    (["Pulled Pork Sandwich by Anna Smith, Bob Jones and Carol Lee"], "Pulled Pork Sandwich"),
    (["Cowboy Butter Recipe"], "Cowboy Butter"),
    (["e ° j", "Mushroom Quesadillas"], "Mushroom Quesadillas"),
    (["(thawed if frozen)", "Shrimp Noodles"], "Shrimp Noodles"),
    (["Recipes » Meat Recipes » Seafood Recipes", "Baked Cod"], "Baked Cod"),
])
def test_the_title(lines, title):
    assert seg(*lines, *ING, "Method", "Mix.")["title_guess"] == title


def test_the_line_above_a_byline_must_look_like_a_name():
    """A description ending just above "By …" isn't the name."""
    s = seg("Lemon Tart", "A bright tart that tastes of", "summer and sunshine.", "By Anna Smith | Published 2024",
            *ING, "Method", "Mix.")
    assert s["title_guess"] == "Lemon Tart"


def test_a_printed_pages_address_names_it():
    s = seg("Share This", "Email Address SEND!", "Share This", "Email Address SEND!",
            "EASY LEMON BAKED COD", "https://www.example.com/lemon-baked-cod/ 2026-10-03, 3:59 PM",
            *ING, "Method", "Mix.")
    assert s["title_guess"] == "Easy Lemon Baked Cod"


def test_step_labels_and_repeated_specks_are_not_titles():
    s = seg("Step 1", "Lemon Rice", "Gga Aaa", "Step 1", "Gga Aaa", *ING, "Method", "Mix.")
    assert s["title_guess"] == "Lemon Rice"


def test_screenshot_bullets_and_prices_leave_the_ingredients():
    s = seg("Quesadillas", "Ingredients", "« 1/4 tsp garlic powder $0.02", "» 8 oz. mozzarella, shredded $1.85",
            "+ 1/4 cup sour cream $0.29", "¢ 5 flour tortillas SO.94", "Instructions", "Cook.")
    assert s["ingredients"] == ["1/4 tsp garlic powder", "8 oz. mozzarella, shredded", "1/4 cup sour cream",
                                "5 flour tortillas"]


def test_steps_only_text_is_recognised():
    from app.text_rules import is_steps_only
    steps = ["Heat the oil.", "Add the onion and cook 5 minutes."]
    assert is_steps_only("Heat the oil.\nAdd the onion  and cook 5 minutes.\n", steps)
    assert not is_steps_only("Soup\nIngredients\n1 onion\nHeat the oil.", steps)
    assert not is_steps_only("", steps) and not is_steps_only("Heat the oil.", [])


def test_steps_only_email_text_stays_out_of_learning_and_the_corpus(client, tmp_path):
    from app import corpus
    from app.database import SessionLocal
    from app.ml import service
    steps = ["Heat the oil in a pan.", "Add the onion and cook until soft."]
    r = client.post("/api/recipes", json={"title": "Old emailed link", "source_type": "email",
                                          "raw_text": "\n".join(steps), "steps": steps,
                                          "ingredients": [{"raw_line": "1 onion"}]}).json()
    try:
        with SessionLocal() as db:
            seg_ex, _, _ = service._examples(db)
        assert next(e for e in seg_ex if e.recipe_id == r["id"]).raw_text is None
        backup = tmp_path / "b.zip"
        backup.write_bytes(client.get("/api/backup/database.zip").content)
        res = corpus.export_library(backup, tmp_path / "c", ids=[r["id"]])
        assert res["added"] == [] and "only its steps" in res["skipped"][0][1]
    finally:
        client.delete(f"/api/recipes/{r['id']}")
        client.delete(f"/api/recently-deleted/{r['id']}")
