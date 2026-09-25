import os
import sys

import pytest

from app.ingestion.pdf_ingest import extract_pdf_text, segment_raw_text, PdfTooLargeError  # noqa: E402


def test_segment_handles_ingredients_before_steps():
    text = (
        "My Recipe\nIngredients\n2 cups flour\n1 tsp salt\n"
        "Instructions\n1. Mix\n2. Bake\n"
    )
    result = segment_raw_text(text)
    assert result["ingredients"] == ["2 cups flour", "1 tsp salt"]
    # Numbers are stripped: the app numbers steps itself, and keeping them
    # showed "1. 1. Mix" in the recipe view and export.
    assert result["steps"] == ["Mix", "Bake"]


def test_segment_handles_steps_before_ingredients():
    """A prior third-party review claimed this ordering produces empty
    lists. It doesn't -- the ing_start < step_start branch in
    segment_raw_text already handles both orderings. This test locks that
    in so a future refactor can't silently reintroduce the bug that was
    incorrectly reported."""
    text = (
        "My Recipe\nInstructions\n1. Do the first thing\n2. Do the second thing\n"
        "Ingredients\n2 cups flour\n1 tsp salt\n"
    )
    result = segment_raw_text(text)
    assert result["ingredients"] == ["2 cups flour", "1 tsp salt"]
    assert result["steps"] == ["Do the first thing", "Do the second thing"]


def test_segment_no_headings_falls_back_to_pattern_matching():
    text = "Quick Snack\n2 cups popcorn\n1. Pop the popcorn\n2. Add butter"
    result = segment_raw_text(text)
    assert any("popcorn" in i for i in result["ingredients"])
    assert any("Pop the popcorn" in s for s in result["steps"])


def test_pdf_with_text_layer_skips_ocr(tmp_path):
    from weasyprint import HTML

    html = "<h1>Test</h1><h2>Ingredients</h2><p>2 cups flour</p><h2>Instructions</h2><p>1. Mix</p>"
    pdf_path = tmp_path / "test.pdf"
    HTML(string=html).write_pdf(str(pdf_path))

    result = extract_pdf_text(str(pdf_path))
    assert result.ocr_used_on_pages == []
    assert "flour" in result.raw_text


def test_pdf_page_cap(tmp_path):
    from weasyprint import HTML

    html = "".join(f"<div style='page-break-after: always;'>Page {i}</div>" for i in range(65))
    pdf_path = tmp_path / "big.pdf"
    HTML(string=html).write_pdf(str(pdf_path))

    with pytest.raises(PdfTooLargeError):
        extract_pdf_text(str(pdf_path))


# The reported case: a webpage saved as PDF from iOS and emailed in. Text as
# the extractor returned it, trimmed of the article body.
PRINTED_RECIPE_CARD = """2026-09-25, 1:44 PM
Page 1 of 1
FREE SHIPPING ON ORDERS OVER $125
HOME / ALL RECIPES / PORK / HOMEMADE SMOKED BACON
Homemade Smoked
Bacon
BY SUSIE BULLOCH ON JUNE 17, 2021
Making your own homemade Smoked Bacon is a bit of a process.
Sweet Rub
$12.99 - $25.99
SAVE PIN PRINT
Homemade Smoked Bacon
BY: SUSIE BULLOCH (HEYGRILLHEY.COM)
4.57 from 82 votes
Ingredients
1X 2X 3X
1 5-pound slab pork belly skin removed
1 Tablespoon cracked black pepper
PEPPERED BACON CURE
1 \u00bc teaspoons Prague powder #1 (curing
salt)
3 Tablespoons plus 1 teaspoon cracked
black pepper
Instructions
1 Prepare the cure. Combine all
ingredients for the bacon cure in a bowl.
It will be a paste-like consistency.
2 Cure the pork belly. Place your slab of
pork belly in a large plastic bag.
Notes
Maple cure: swap the pepper for maple sugar.
"""


def test_printed_recipe_card_title_is_the_recipe_not_the_print_date():
    assert segment_raw_text(PRINTED_RECIPE_CARD)["title_guess"] == "Homemade Smoked Bacon"


def test_printed_recipe_card_ingredients():
    assert segment_raw_text(PRINTED_RECIPE_CARD)["ingredients"] == [
        "1 5-pound slab pork belly skin removed",
        "1 Tablespoon cracked black pepper",
        "For the peppered bacon cure:",
        "1 \u00bc teaspoons Prague powder #1 (curing salt)",
        "3 Tablespoons plus 1 teaspoon cracked black pepper",
    ]


def test_printed_recipe_card_steps_are_whole_steps():
    """Each printed line used to become its own step."""
    assert segment_raw_text(PRINTED_RECIPE_CARD)["steps"] == [
        "Prepare the cure. Combine all ingredients for the bacon cure in a bowl. "
        "It will be a paste-like consistency.",
        "Cure the pork belly. Place your slab of pork belly in a large plastic bag.",
    ]


def test_step_numbers_on_their_own_lines():
    text = "Soup\nIngredients\n1 onion\nInstructions\n1\nChop the onion\nfinely.\n2\nCook it.\n"
    assert segment_raw_text(text)["steps"] == ["Chop the onion finely.", "Cook it."]


def test_unnumbered_steps_split_at_sentence_ends():
    text = ("Soup\nIngredients\n1 onion\nInstructions\nChop the onion and\nput it in a pot.\n"
            "Simmer for ten\nminutes.\n")
    assert segment_raw_text(text)["steps"] == ["Chop the onion and put it in a pot.", "Simmer for ten minutes."]


def test_recipe_card_ingredients_win_over_article_mention():
    """A blog post with its own "Ingredients" paragraph before the card: the
    heading followed by actual quantities is the one used."""
    text = ("Stew\nIngredients\nGood beef matters most here.\nMethod\nRead on.\n"
            "Stew\nIngredients\n2 lb beef\n1 onion\nInstructions\n1. Brown the beef.\n2. Simmer.\n")
    result = segment_raw_text(text)
    assert result["ingredients"] == ["2 lb beef", "1 onion"]
    assert result["steps"] == ["Brown the beef.", "Simmer."]


# The reported email: Gmail plain text, "Ingredients" heading but no
# "Instructions" heading, bullets as "   - ", bold as *text*.
GMAIL_BODY = """Garlic Pork, Bok Choy & Carrot Stir-Fry Noodles
*Prep time:* 15 mins | *Cook time:* 12 mins

Ingredients

   - *Noodles:* 8 oz (225g) wheat noodles, lo mein, or ramen noodles

   - *Pork Marinade (Velveting):*

   - 1 tsp soy sauce

      - 3\\u20134 heads baby bok choy, stems sliced into bite-sized pieces,
      leaves separated

*1.Cook, rinse, and drain the noodles:*Stops cooking so noodles hold their
chew during the stir-fry.

*2.Sear the pork:*
   - *If using sliced pork:* Spread pieces flat, sear for
   1\\u20132 minutes per side until lightly browned and just cooked.
"""


def test_gmail_body_ingredients():
    """Only the wrapped fragment "1-2 minutes per side..." used to be found,
    because it was the one line that started with a digit."""
    assert segment_raw_text(GMAIL_BODY)["ingredients"] == [
        "8 oz (225g) wheat noodles, lo mein, or ramen noodles",
        "For the pork marinade (velveting):",
        "1 tsp soy sauce",
        "3\\u20134 heads baby bok choy, stems sliced into bite-sized pieces, leaves separated",
    ]


def test_gmail_body_numbered_steps_without_heading():
    steps = segment_raw_text(GMAIL_BODY)["steps"]
    assert len(steps) == 2
    assert steps[0].startswith("Cook, rinse, and drain the noodles: Stops cooking")
    assert steps[1].startswith("Sear the pork:") and steps[1].endswith("just cooked.")
