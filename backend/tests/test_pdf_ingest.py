import io
import os
import sys
import zlib
from unittest.mock import patch

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject, StreamObject

from app.ingestion import pdf_ingest
from app.ingestion.pdf_ingest import (PdfTooLargeError, extract_largest_embedded_image, extract_pdf_text,
                                      segment_raw_text)


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


def _pdf_with_mask(mask_w: int, mask_h: int, mask_data_len: int, fmt: str = "PNG") -> bytes:
    import base64

    from weasyprint import HTML
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 100, 50)).save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode()
    mime = "image/jpeg" if fmt == "JPEG" else "image/png"
    pdf = HTML(string=f'<img src="data:{mime};base64,{b64}" width="400" height="300">').write_pdf()
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    mask = StreamObject()
    mask.set_data(zlib.compress(b"\xff" * mask_data_len))
    mask.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(mask_w), NameObject("/Height"): NumberObject(mask_h),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"),
        NameObject("/BitsPerComponent"): NumberObject(8),
        NameObject("/Filter"): NameObject("/FlateDecode"),
    })
    ref = writer._add_object(mask)
    for page in writer.pages:
        for xobj in pdf_ingest._image_xobjects(page, 0, set()):
            xobj[NameObject("/SMask")] = ref
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


# OCR of two screenshots of a grocery-store app recipe (Spicy shrimp
# fusilli), exactly as Tesseract read them. The steps were lost: "Method 2
# steps" and "Ingredients 13 total" weren't taken as headings, "Step 1" not
# as a step marker, and the phone's status bar landed in the ingredients.
SHRIMP_OCR = """Spicy shrimp fusilli

2024-10-14 7:39 PM

7:38 a 7 @®

< Q. Search for a product

Ingredients 13 total

8 oz Fusilli

2 tbsp olive oil, divided

1 pkg Uncooked Extra Large Black Tiger Shrimp, thawed
1/2 tsp hot pepper flakes

1 onion, finely diced

3 cloves garlic, minced

1 can diced tomatoes

1/4 tsp salt

1/4 tsp pepper

1/4 tsp dried oregano

1 cup loosely packed baby arugula
1 tbsp lemon juice

2 tosp finely chopped fresh parsley


Method 2 steps

Step 1

Boil water in large skillet set over high heat. Cook pasta
until al dente (cook slightly less than package directions).
Reserve 1/2 cup (125 mL) pasta cooking water. Drain
pasta; set aside. Heat 1 tbsp (15 mL) oil in same skillet set
over medium heat. Add shrimp and hot pepper flakes;
sauté 3 to 4 min. until shrimp are pink and firm. Transfer
to plate.

Step 2

Heat remaining oil in skillet. Add onion and garlic. Cook
about 2 min. to soften. Stir in tomatoes, 1/2 cup (125 mL)
water (not the pasta water), salt, pepper and oregano;
bring to a boil. Reduce heat to medium-low; simmer about
15 min. until sauce is thickened. Add pasta, reserved
pasta water and shrimp into skillet; stir to coat. Just
before serving, stir in arugula and lemon juice; sprinkle
with parsley."""


@pytest.mark.parametrize("title,kept", [
    ("Death by Chocolate Cake", "Death by Chocolate Cake"), ("Stand by Me Brownies", "Stand by Me Brownies"),
    ("Pulled Pork Sandwich by Samuel Fortier, Anna Lee", "Pulled Pork Sandwich"),
    ("Lemon Tart by Anna Smith", "Lemon Tart"),
])
def test_a_byline_goes_a_dish_name_stays(title, kept):
    from app.ingestion.pdf_ingest import _tidy_title
    assert _tidy_title(title) == kept


def test_a_section_label_in_front_of_a_quantity_is_kept():
    from app.ingestion.pdf_ingest import _clean_ingredients
    assert _clean_ingredients(["For the dough: 2 cups flour", "1 tsp salt", "Noodles: 8 oz wheat noodles"]) == \
        ["For the dough:", "2 cups flour", "1 tsp salt", "8 oz wheat noodles"]
    assert _clean_ingredients(["Pour la pâte: 250 g de farine"]) == ["Pour la pâte:", "250 g de farine"]


def test_short_steps_without_headings():
    from app.ingestion.pdf_ingest import rule_segment_raw_text as seg
    r = seg("Pancakes\n1 cup flour\n2 eggs\nMix everything.\nFry.")
    assert r["ingredients"] == ["1 cup flour", "2 eggs"] and r["steps"] == ["Mix everything.", "Fry."]
    assert seg("Cake\n2 cups flour\n3 eggs\nBake 20 min.")["steps"] == ["Bake 20 min."]
    # A cooking verb at the start of an ingredient line isn't a step.
    r = seg("Steak\n2 lb top sirloin\nTop sirloin trimmings\n1 tsp salt\nGrill it.")
    assert "Top sirloin trimmings" not in r["steps"] and r["steps"] == ["Grill it."]


def test_an_oversized_soft_mask_is_not_decoded(tmp_path):
    """A 400 x 300 photo with a 20,000 x 20,000 soft mask passed 0104's
    check: only the mask's filter was looked at."""
    path = tmp_path / "m.pdf"
    path.write_bytes(_pdf_with_mask(20000, 20000, 1000))
    from pypdf.generic import _image_xobject
    decoded = []
    with patch.object(_image_xobject, "_xobj_to_image", side_effect=lambda *a, **k: decoded.append(1)):
        assert extract_largest_embedded_image(str(path)) is None
    assert decoded == []


def test_a_jpeg_with_a_soft_mask_is_skipped_before_decoding(tmp_path):
    """pypdf returns it as JPEG 2000, which the app doesn't accept."""
    path = tmp_path / "j.pdf"
    path.write_bytes(_pdf_with_mask(400, 300, 400 * 300, fmt="JPEG"))
    from pypdf.generic import _image_xobject
    decoded = []
    with patch.object(_image_xobject, "_xobj_to_image", side_effect=lambda *a, **k: decoded.append(1)):
        assert extract_largest_embedded_image(str(path)) is None
    assert decoded == []


def test_a_soft_mask_without_a_size_is_refused():
    class X(dict):
        def get_object(self):
            return self
    image = X({"/Filter": NameObject("/FlateDecode"), "/Width": 400, "/Height": 300,
               "/SMask": X({"/Filter": NameObject("/FlateDecode")})})
    assert not pdf_ingest._decode_allowed(image, 40_000_000)
    image["/SMask"]["/Width"], image["/SMask"]["/Height"] = 400, 300
    assert pdf_ingest._decode_allowed(image, 40_000_000)


def test_a_photo_with_a_normal_soft_mask_is_still_extracted(tmp_path):
    path = tmp_path / "ok.pdf"
    path.write_bytes(_pdf_with_mask(400, 300, 400 * 300))
    data = extract_largest_embedded_image(str(path))
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (400, 300)


def test_pdf_photos_use_the_same_limit(tmp_path):
    import base64

    from weasyprint import HTML
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (1, 2, 3)).save(buf, format="JPEG")
    pdf = HTML(string=f'<img src="data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode()}">').write_pdf()

    def declared(w, h):
        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
        for page in writer.pages:
            for xobj in pdf_ingest._image_xobjects(page, 0, set()):
                xobj[NameObject("/Width")] = NumberObject(w)
                xobj[NameObject("/Height")] = NumberObject(h)
        path = tmp_path / f"{w}.pdf"
        out = io.BytesIO()
        writer.write(out)
        path.write_bytes(out.getvalue())
        return str(path)

    from unittest.mock import patch

    from pypdf.generic import _image_xobject
    for (w, h), expect_decode in (((10_000, 5_100), False), ((8_000, 6_000), True)):
        decoded = []
        with patch.object(_image_xobject, "_xobj_to_image", side_effect=lambda *a, **k: decoded.append(1)):
            pdf_ingest.extract_largest_embedded_image(declared(w, h))
        assert bool(decoded) is expect_decode, (w, h)


@pytest.mark.parametrize("footer", ["©", "Copyright", "  ©  ", "COPYRIGHT"])
def test_a_bare_copyright_line_ends_the_steps(footer):
    from app.ingestion.pdf_ingest import rule_segment_raw_text
    s = rule_segment_raw_text("\n".join(["Bread", "Ingredients", "2 cups flour", "Method",
                                         "Mix the flour.", "Bake 30 minutes.", footer, "Breads (7)"]))
    assert s["steps"] == ["Mix the flour.", "Bake 30 minutes."]


def test_preprocess_for_ocr_caps_extreme_upscale():
    """A 12 x 60000 px render (a 3 x 14400 pt page rendered within budget)
    used to upscale to 1500 px wide unconditionally -- 1500 x 7,500,000 =
    11.2 gigapixels."""
    from PIL import Image

    from app.ingestion import pdf_ingest

    narrow_tall = Image.new("L", (12, 60000), color=255)
    result = pdf_ingest._preprocess_for_ocr(narrow_tall)
    assert result.width * result.height <= pdf_ingest.MAX_OCR_PIXELS * 1.02


def test_preprocess_for_ocr_downscales_an_already_oversized_render():
    """Safety net for an image that reaches preprocessing over budget: the
    old code only ever upscaled narrow images, so an already-too-big one
    passed through untouched."""
    from PIL import Image

    from app.ingestion import pdf_ingest

    oversized = Image.new("L", (6200, 6200), color=255)
    result = pdf_ingest._preprocess_for_ocr(oversized)
    assert result.width * result.height <= pdf_ingest.MAX_OCR_PIXELS * 1.02


def test_huge_page_renders_within_budget():
    """The first attempt only shrank the image after rendering: a 200 x 200
    inch page still rendered at 72 dpi (the old floor), 14400 x 14400 =
    207 MP, 1.7 GB peak, before the cap could apply."""
    from app.ingestion.pdf_ingest import MAX_OCR_PIXELS, _ocr_resolution
    dpi = _ocr_resolution(14400, 14400)
    assert (200 * dpi) ** 2 <= MAX_OCR_PIXELS


def test_image_preprocess_caps_extreme_upscale(tmp_path):
    """A 20 x 40000 px upload asked for 4.5 gigapixels after the
    upscale-to-1500-wide step."""
    from PIL import Image

    from app.ingestion import image_ingest

    p = tmp_path / "tall.png"
    Image.new("RGB", (20, 40000), color=(255, 255, 255)).save(p)
    processed, _ = image_ingest._preprocess(str(p))
    assert processed.size[0] * processed.size[1] <= image_ingest.MAX_OCR_PIXELS * 1.02


def test_rotated_photo_keeps_jpeg_quality(tmp_path):
    from PIL import Image

    from app.file_validation import JPEG_QUALITY
    from app.ingestion.image_ingest import _rotate_stored_file

    p = tmp_path / "photo.jpg"
    Image.effect_noise((600, 400), 60).convert("RGB").save(p, format="JPEG", quality=JPEG_QUALITY)
    before = p.stat().st_size
    _rotate_stored_file(str(p), 90)
    # At the old default (75) the re-save of a noisy image shrank by well
    # over a third; at the same quality it stays close to the original size.
    assert p.stat().st_size > before * 0.8


def test_every_tesseract_run_has_a_time_limit():
    """(5) A crafted image held a heavy-job slot with no limit."""
    import ast
    from pathlib import Path

    from app.ingestion.pdf_ingest import OCR_TIMEOUT_SECONDS
    assert 0 < OCR_TIMEOUT_SECONDS <= 300
    calls = 0
    for f in (Path(__file__).resolve().parents[1] / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr.startswith("image_to_")):
                calls += 1
                assert any(k.arg == "timeout" for k in node.keywords), (f.name, node.lineno)
    assert calls == 2          # orientation, then one pass for the text and its confidence (0167)


def test_tall_scanned_pages_render_within_the_pixel_budget():
    """An 8.5 x 200 inch image-only page rendered at 300 dpi was 153 MP."""
    from app.ingestion.pdf_ingest import MAX_OCR_PIXELS, _ocr_resolution
    for w, h in [(612, 14400), (393, 14400), (14400, 14400)]:
        dpi = _ocr_resolution(w, h)
        assert (w / 72 * dpi) * (h / 72 * dpi) <= MAX_OCR_PIXELS * 1.02
    assert _ocr_resolution(612, 792) == 300          # an ordinary page is unchanged


def test_grocery_app_screenshot_steps_are_found():
    from app.ingestion.pdf_ingest import segment_raw_text
    s = segment_raw_text(SHRIMP_OCR)
    assert s["title_guess"] == "Spicy shrimp fusilli"
    assert len(s["ingredients"]) == 13
    assert s["ingredients"][0] == "8 oz Fusilli"
    assert "2 tbsp finely chopped fresh parsley" in s["ingredients"]        # OCR "tosp"
    assert not any("7:38" in i for i in s["ingredients"])                   # status bar
    assert len(s["steps"]) == 2
    assert s["steps"][0].startswith("Boil water") and s["steps"][0].endswith("Transfer to plate.")
    assert s["steps"][1].startswith("Heat remaining oil") and s["steps"][1].endswith("sprinkle with parsley.")


def test_step_label_is_not_the_steps_heading():
    from app.ingestion.pdf_ingest import HEADING_INGREDIENTS, HEADING_STEPS
    for h in ["Method", "Method 2 steps", "Directions:", "Instructions (4)", "Steps"]:
        assert HEADING_STEPS.match(h), h
    for h in ["Ingredients", "Ingredients 13 total", "Ingredients 1X 2X", "Ingredients (8)"]:
        assert HEADING_INGREDIENTS.match(h), h
    for not_h in ["Step 1", "Step 2:", "Method of cooking is key"]:
        assert not HEADING_STEPS.match(not_h), not_h


def test_screenshot_without_headings_gets_its_steps():
    """An app screenshot with a checklist of ingredients and circled step
    numbers, no "Ingredients"/"Method" headings. OCR turns the checkboxes
    into "LJ" and the circled numbers into "@", "9", "(c)"; the steps used
    to be lost and the title came out as "LJ"."""
    from app.ingestion.pdf_ingest import segment_raw_text
    raw = "\n".join([
        "Test Beef Stew", "2024-02-20 8:57 PM", "7:34 94", "",
        "2 tbsp olive oil", "2 lb beef chuck", "3 garlic cloves", "2 onions",
        "LJ", "L", "LJ", "1 sprig thyme", "AA & a example.com", "",
        "Cut the beef into chunks. Pat dry", "then season well.", "",
        "@ Brown the beef in batches over high", "heat. Remove to a plate.", "",
        "9 Cook garlic and onion for 3 minutes.", "",
        "© Return the beef and simmer for 2 hours.", "",
        "faa) Serve with mashed potatoes!!",
    ])
    s = segment_raw_text(raw)
    assert s["title_guess"] == "Test Beef Stew"
    assert s["ingredients"] == ["2 tbsp olive oil", "2 lb beef chuck", "3 garlic cloves", "2 onions", "1 sprig thyme"]
    assert s["steps"] == [
        "Cut the beef into chunks. Pat dry then season well.",
        "Brown the beef in batches over high heat. Remove to a plate.",
        "Cook garlic and onion for 3 minutes.",
        "Return the beef and simmer for 2 hours.",
        "Serve with mashed potatoes!!",
    ]


def test_steps_stop_at_ratings_and_reviews():
    from app.ingestion.pdf_ingest import segment_raw_text
    raw = "\n".join(["Test Pork Chops", "Ingredients", "4 pork chops", "2 apples", "Directions",
                     "Step 1", "Preheat the oven.", "Step 2", "Roast the chops.",
                     "Reviews (9)", "Great recipe, loved it.", "17 Ratings", "5 star 15"])
    assert segment_raw_text(raw)["steps"] == ["Preheat the oven.", "Roast the chops."]


# ---- Claude review of 0165: real cookbook pages (teal titles, two columns,
# steps without a heading)

def test_steps_after_an_ingredients_heading_without_numbers():
    """An Ingredients heading, no Method heading, unnumbered steps:
    everything after the heading had become ingredients."""
    r = segment_raw_text("LA RACLETTE CLASSIQUE\nINGREDIENTS\nSautéed potatoes\nraclette cheese of choice\n"
                         "mini pickles\npickled onions\n\nHEAT the ingredients and PUT the melted cheese on top.\n")
    assert r["ingredients"] == ["Sautéed potatoes", "raclette cheese of choice", "mini pickles", "pickled onions"]
    assert r["steps"] == ["HEAT the ingredients and PUT the melted cheese on top."]
    short = segment_raw_text("PINA COLADA\nINGREDIENTS\n220g of pineapple\n3-4 ice cubes\na pinch of nutmeg\nBLEND and SERVE.\n")
    assert short["steps"] == ["BLEND and SERVE."] and short["ingredients"][-1] == "a pinch of nutmeg"


def test_a_repeated_ingredient_isnt_the_title():
    """"salt and pepper" under two parts was taken as the name: the
    repeated-line rule only looks above the ingredients."""
    r = segment_raw_text("Les Brigands\nPASTRY BASE\npuff pastry\nparchment paper\n"
                         "ROLL the pastry and BAKE it for 20 minutes.\nCHEESE MIX\n200g of goat's cheese\n"
                         "salt and pepper\nMIX the cheese until smooth.\nONIONS\n2 large onions\nsalt and pepper\n"
                         "CHOP the onions and cook them slowly.\n")
    assert r["title_guess"] != "Salt and pepper" and r["title_guess"].lower() != "salt and pepper"


def test_lowercase_items_stay_separate_and_wrapped_lines_join():
    from app.ingestion.pdf_ingest import _clean_ingredients
    assert _clean_ingredients(["Sautéed potatoes", "raclette cheese of choice", "mini pickles"]) == \
        ["Sautéed potatoes", "raclette cheese of choice", "mini pickles"]
    assert _clean_ingredients(["2 tbsp olive oil", "fresh mint leaves"]) == ["2 tbsp olive oil", "fresh mint leaves"]
    # Ordinary wraps 0167 split (Claude review of 0167).
    for a, b in (("1 large onion, finely", "chopped"), ("2 tbsp butter, at room", "temperature"),
                 ("2 green onions, thinly", "sliced"), ("1/2 cup parsley", "chopped")):
        assert _clean_ingredients([a, b]) == [f"{a} {b}"]
    assert _clean_ingredients(["2/3 of a tsp of Dijon mustard sauce", "salt and pepper"])[-1] == "salt and pepper"
    # Wrapped: left off mid-phrase, or a long measured line's last words.
    assert _clean_ingredients(["2 mangos almost ripe, but still firm,", "peeled and cut into batonnets"]) == \
        ["2 mangos almost ripe, but still firm, peeled and cut into batonnets"]
    assert _clean_ingredients(["1 cup finely chopped fresh flat-leaf", "parsley"]) == \
        ["1 cup finely chopped fresh flat-leaf parsley"]


def test_a_part_name_in_capitals_after_a_comma_and_a_byline_without_items():
    """OCR read a paragraph's full stop as a comma, so "SALAD" was taken as
    the paragraph going on and the parts were lost; "LES BRIGANDS" under
    the title, with nothing under it, became a part."""
    r = segment_raw_text("THAI MANGO SALAD\nLE TUKTUK\nDRESSING\n170g of palm sugar\n1/4 c. of fish sauce\n"
                         "BRING the sauce and sugar to a boil, let it cool,\nSALAD\n2 mangos, cut into batonnets\n"
                         "3 French shallots, finely chopped\nMIX the mango and shallots with the sauce.\n")
    assert r["ingredients"][0] == "Dressing:" and "Salad:" in r["ingredients"]
    assert not any("tuktuk" in i.lower() for i in r["ingredients"])
    assert r["steps"][0].startswith("BRING")


def _words(*rows):
    """image_to_data's dictionary for lines of (left, word) pairs: one
    paragraph, 20 px high words, 10 px per character."""
    keys = ("level", "block_num", "par_num", "line_num", "left", "width", "height", "text", "conf")
    d = {k: [] for k in keys}
    for n, row in enumerate(rows, 1):
        for left, word in row:
            for k, v in zip(keys, (5, 1, 1, n, left, 10 * len(word), 20, word, 90)):
                d[k].append(v)
    return d


def test_two_columns_are_read_one_after_the_other():
    """Tesseract read straight across the gap: "1 cup uncooked rice indian
    spices:" (5 pages of 9)."""
    from app.ingestion.pdf_ingest import _lines_from_words
    data = _words([(100, "Sautéed"), (190, "potatoes"), (600, "mini"), (650, "pickles")],
                  [(100, "raclette"), (190, "cheese"), (600, "pickled"), (680, "onions")])
    assert _lines_from_words(data, 1100) == "Sautéed potatoes\nraclette cheese\nmini pickles\npickled onions\n"
    # One line across the gap, or a lone wide space: left as it is.
    prose = _words([(100, "HEAT"), (160, "the"), (600, "cheese")], [(100, "and"), (140, "serve"), (210, "it"), (240, "now")])
    assert _lines_from_words(prose, 1100) == "HEAT the cheese\nand serve it now\n"
    crossing = _words([(100, "a"), (600, "b")], [(100, "c"), (600, "d")], [(100, "a"), (130, "line"), (190, "that"),
                                                                          (250, "runs"), (310, "across"), (380, "it"),
                                                                          (410, "all"), (450, "the"), (490, "way"),
                                                                          (540, "and"), (580, "on")])
    # A line across the gutter ends the block above it, which is given
    # column by column.
    assert _lines_from_words(crossing, 1100).startswith("a\nc\nb\nd\na line that")
    # A heading between two blocks ("WISH" after "SIFT", owner's cookie
    # page): each block whole, the heading between them.
    blocks = _words([(100, "flour"), (600, "cinnamon")], [(100, "cocoa"), (600, "salt")], [(100, "WISH")],
                    [(100, "oil"), (600, "eggs")], [(100, "sugar"), (600, "vanilla")])
    assert _lines_from_words(blocks, 1100) == "flour\ncocoa\ncinnamon\nsalt\nWISH\noil\nsugar\neggs\nvanilla\n"


def test_one_tesseract_run_gives_the_text_and_the_confidence(monkeypatch, tmp_path):
    """Each photo was read twice; image_to_data already has the text."""
    import pytesseract
    from PIL import ImageDraw

    from app.ingestion import image_ingest

    from .conftest import text_font

    def refuse(*a, **k):
        raise AssertionError("a second Tesseract run")
    monkeypatch.setattr(pytesseract, "image_to_string", refuse)
    img = Image.new("RGB", (900, 200), "white")
    ImageDraw.Draw(img).text((40, 60), "Mix the flour and sugar.", fill="black",
                             font=text_font(40))
    img.save(tmp_path / "p.png")
    r = image_ingest.ingest_image(str(tmp_path / "p.png"))
    assert "flour" in r.raw_text and r.ocr_confidence and r.ocr_confidence > 50


def test_a_capitalised_ingredient_isnt_a_numbered_step():
    """Claude review of 0167: under an Ingredients heading with no Method
    heading, "3 Eggs" was taken as step 3 and every line after it became
    a step."""
    r = segment_raw_text("Small Omelette\nIngredients\n3 Eggs\n2 Green onions, sliced\n2 Cloves of garlic\n"
                         "1 tbsp butter\nWhisk the eggs with the onions and garlic.\nCook in the butter until set.\n")
    assert r["ingredients"] == ["3 Eggs", "2 Green onions, sliced", "2 Cloves of garlic", "1 tbsp butter"]
    assert r["steps"][0].startswith("Whisk")
    # A real numbered step still is one, with or without its full stop.
    r = segment_raw_text("Biscuits\nIngredients\n2 cups flour\n1 cup milk\n1 Preheat the oven to 220 degrees.\n"
                         "2 Mix the flour and milk and bake for twelve minutes\n")
    assert r["ingredients"] == ["2 cups flour", "1 cup milk"] and len(r["steps"]) == 2


def test_part_names_in_capitals_without_an_ingredients_heading():
    """Owner's cookie page: "SIFT" and "WISH" over measured lines, then the
    method, which starts with "SCOOP" (now a cooking verb) on a line that
    doesn't end a sentence."""
    r = segment_raw_text("DIABOLO COOKIES\nSIFT\n1.5 c all purpose flour\n1 c cocoa\nWISH\n1/2 c canola oil\n2 eggs\n"
                         "SCOOP 2 oz dough balls on to a greased cookie sheet, spaced out well -- they will\n"
                         "spread! BAKE at 375 for 15 minutes. Let cool and serve!\n")
    assert r["ingredients"] == ["Sift:", "1.5 c all purpose flour", "1 c cocoa", "Wish:", "1/2 c canola oil", "2 eggs"]
    assert r["steps"][0].startswith("SCOOP")


def test_ingredients_beside_the_method_are_read_apart(tmp_path):
    """Review of 0169: --psm 4 on every page read the two sides of such a
    page into each other (4-6 of 9 ingredients, 3-5 of 6 steps): the two
    sides have different line spacing, so there are no rows to split.
    Tesseract's own layout comes first."""
    from PIL import ImageDraw

    from app.ingestion import image_ingest

    from .conftest import text_font
    items = ["2 cups flour", "1 cup sugar", "3 eggs", "1 tsp vanilla", "1/2 cup butter", "1 tsp salt",
             "1 cup milk", "2 tsp baking powder", "1 lemon, zested"]
    method = ["Heat the oven to 180 degrees and butter a round", "cake tin well before you start mixing anything.",
              "Beat the eggs and sugar together until pale", "and thick, then add the vanilla and the zest.",
              "Fold in the flour, baking powder and salt,", "then the milk and the melted butter.",
              "Pour into the tin and level the top with", "the back of a spoon so it bakes evenly.",
              "Bake for thirty minutes, until a skewer", "comes out clean from the middle.",
              "Cool in the tin for ten minutes, then", "turn out onto a rack to cool completely."]
    img = Image.new("RGB", (1500, 1300), "white")
    d = ImageDraw.Draw(img)
    d.text((60, 40), "Plain Sponge Cake", font=text_font(52), fill="black")
    d.text((60, 150), "Ingredients", font=text_font(34), fill="black")
    for i, item in enumerate(items):
        d.text((60, 210 + 56 * i), item, font=text_font(30), fill="black")
    x = 60 + max(d.textlength(it, font=text_font(30)) for it in items) + 60
    d.text((x, 150), "Method", font=text_font(34), fill="black")
    for k in range(0, 12, 2):
        y = 210 + 37 * k + 14 * (k // 2)
        d.text((x, y), f"{k // 2 + 1}. " + method[k], font=text_font(26), fill="black")
        d.text((x, y + 37), method[k + 1], font=text_font(26), fill="black")
    img.save(tmp_path / "p.png")
    r = image_ingest.ingest_and_segment(str(tmp_path / "p.png"))
    assert sum(i in r["ingredients"] for i in items) >= 8, r["ingredients"]
    assert len(r["steps"]) == 6, r["steps"]


def test_a_page_is_read_again_as_whole_lines_only_when_incomplete(monkeypatch):
    from app.ingestion import pdf_ingest
    complete = "Cake\nIngredients\n2 cups flour\n1 cup sugar\nMethod\n1. Mix the flour and sugar.\n"
    calls = []

    def fake(processed, config):
        calls.append(config)
        return (complete if config else readings[0]), 90.0
    monkeypatch.setattr(pdf_ingest, "_ocr_once", fake)
    readings = [complete]
    assert pdf_ingest.ocr_text_and_confidence(Image.new("L", (10, 10)))[0] == complete and calls == [None]
    calls.clear()
    readings = ["Cake\nIngredients\n2 cups flour\n1 cup sugar\n"]            # no steps
    assert pdf_ingest.ocr_text_and_confidence(Image.new("L", (10, 10)))[0] == complete
    assert calls == [None, "--psm 4"]
