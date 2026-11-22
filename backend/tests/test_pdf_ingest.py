import io
import os
import sys
import zlib
from unittest.mock import patch

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, NumberObject, StreamObject

from app.ingestion import pdf_ingest, pdf_read
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
    # Separate items 0168 joined (review of 0169): "-ly"/"-ment" only from a
    # list of adverbs, and a short tail only when it reads as one.
    for a, b in (("1 lb pork belly", "salt and pepper"), ("2 tbsp red currant jelly", "fresh mint"),
                 ("1 sheet parchment", "cooking spray"), ("500 g de piment", "huile d'olive"),
                 ("1 tsp chili", "olive oil")):
        assert _clean_ingredients([a, b]) == [a, b]
    assert _clean_ingredients(["3 Tablespoons plus 1 teaspoon cracked", "black pepper"]) == \
        ["3 Tablespoons plus 1 teaspoon cracked black pepper"]
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
    from app.ingestion import ocr, pdf_ingest
    complete = "Cake\nIngredients\n2 cups flour\n1 cup sugar\nMethod\n1. Mix the flour and sugar.\n"
    calls = []

    def fake(processed, config):
        calls.append(config)
        return (complete if config else readings[0]), 90.0
    monkeypatch.setattr(ocr, "_ocr_once", fake)
    readings = [complete]
    assert pdf_ingest.ocr_text_and_confidence(Image.new("L", (10, 10)))[0] == complete and calls == [None]
    calls.clear()
    readings = ["Cake\nIngredients\n2 cups flour\n1 cup sugar\n"]            # no steps
    assert pdf_ingest.ocr_text_and_confidence(Image.new("L", (10, 10)))[0] == complete
    assert calls == [None, "--psm 4"]


def test_a_huge_text_is_segmented_quickly():
    """Review of 0169: an email body of 62 KB whose lines each go on from
    the last took 17 s (1 MB about 75 minutes). The segmenter reads at
    most 400,000 characters, 4,000 lines, 2,000 a line."""
    import time
    body = "Pancakes\nIngredients\n1 cup (flour\n" + "and more (things\n" * 300_000 + "x" * 100_000
    start = time.monotonic()
    r = segment_raw_text(body)
    assert time.monotonic() - start < 5
    assert all(len(i) <= 2_000 + 100 for i in r["ingredients"])


def test_an_ingredient_in_capitals_isnt_a_part_name():
    """Review of 0169: "SALT", "BUTTER" and "OLIVE OIL" became "Salt:",
    "Butter:", "Olive oil:", shown as headings."""
    r = segment_raw_text("BISCUITS\n2 CUPS FLOUR\n1 TBSP BAKING POWDER\nSALT\nBUTTER\n1 CUP MILK\n"
                         "Mix everything and bake for twelve minutes.\n")
    assert "SALT" in r["ingredients"] and "BUTTER" in r["ingredients"]
    r = segment_raw_text("Tomato Salad\n4 tomatoes\n1 red onion\nOLIVE OIL\n2 tbsp vinegar\n"
                         "Slice the tomatoes and onion and dress them.\n")
    assert "OLIVE OIL" in r["ingredients"] and not any(i.endswith(":") for i in r["ingredients"])


def test_a_method_cut_into_strips_is_read_again_as_whole_lines(monkeypatch):
    """Review of 0176: on the Dosa page, Tesseract's layout cut the method
    running across both columns into strips that interleave (6 steps, 4
    starting mid-word). The page had ingredients and steps, so it wasn't
    read again. A step whose first two words are lowercase now asks for the
    whole-line reading, kept when it has fewer such steps. A stray letter
    for a step number ("w Beat") is followed by a capital and doesn't."""
    from app.ingestion import ocr, pdf_ingest
    head = "Rice Pancakes\nIngredients\n2 cups rice\n1 cup lentils\n1 tsp salt\nMethod\n"
    strips = head + ("SOAK the rice and the lentils in separate bowls overnight.\n\n"
                     "re to its bowl and blend the lentils the same way.\n\n"
                     "nce golden spots appear underneath, turn the pancake over.\n")
    whole = head + ("SOAK the rice and the lentils in separate bowls overnight, then drain them.\n\n"
                    "BLEND the rice and return it to its bowl, then blend the lentils the same way.\n\n"
                    "COOK a ladle of batter until golden spots appear underneath, then turn it over.\n")
    numbered = head + "w Beat the rice and the lentils together until smooth and light.\n\n" \
        "a Cook a ladle of batter in a hot pan until golden on both sides.\n"
    calls = []

    def fake(processed, config):
        calls.append(config)
        return (whole if config else readings[0]), 90.0
    monkeypatch.setattr(ocr, "_ocr_once", fake)
    readings = [strips]
    assert pdf_ingest.ocr_text_and_confidence(Image.new("L", (10, 10)))[0] == whole
    assert calls == [None, "--psm 4"]
    calls.clear()
    readings = [numbered]
    assert pdf_ingest.ocr_text_and_confidence(Image.new("L", (10, 10)))[0] == numbered and calls == [None]
    # The second reading is kept only if it's better on that count.
    calls.clear()
    readings = [strips]
    monkeypatch.setattr(ocr, "_ocr_once", lambda p, c: (calls.append(c) or (strips if c else readings[0]), 90.0))
    assert pdf_ingest.ocr_text_and_confidence(Image.new("L", (10, 10)))[0] == strips


def test_an_item_ending_in_a_participle_keeps_the_next_one_apart():
    """Review of 0176: 0172's "-ed" and hyphen endings joined items that
    0169 kept apart. After a comma, a participle ends the item; "red",
    "dried" and "seed" aren't participles; "half-and-half" isn't a word
    before a noun. The joins they were added for still happen."""
    from app.ingestion.pdf_ingest import _clean_ingredients
    for a, b in (("1/2 cup butter, melted", "salt to taste"), ("1/2 cup butter, softened", "pinch of nutmeg"),
                 ("1 cup rice, cooked", "scallions, sliced"), ("1 tsp chili", "red onion"),
                 ("1 tsp salt", "dried oregano"), ("1 tsp fennel seed", "salt"),
                 ("1 cup half-and-half", "vanilla extract")):
        assert _clean_ingredients([a, b]) == [a, b], (a, b)
    for a, b in (("1 tsp cracked", "black pepper"), ("1 large", "onion"), ("1/2 cup parsley", "chopped"),
                 ("2 tbsp minced", "garlic"), ("1 tbsp extra", "virgin olive oil"), ("2 cups all-purpose", "flour"),
                 ("1/2 cup melted", "butter")):
        assert _clean_ingredients([a, b]) == [f"{a} {b}"], (a, b)


def test_a_part_name_under_a_yield_line_in_capitals():
    """Review of 0176: on the cookie page, the yield line above "SIFT" is in
    capitals, so it counted as a capitals neighbour and "SIFT" was dropped.
    Directly above the first measured line, only the line below counts."""
    r = segment_raw_text("DIABOLO COOKIES\n@ MAKES:12 COOKIES PREP TIME: 28 MIN COOK TIME: 15 MIN\nSIFT\n"
                         "1.5 c all purpose flour\n1 c cocoa\nWISH\n1/2 c canola oil\n2 eggs\n"
                         "SCOOP 2 oz dough balls on to a greased cookie sheet and BAKE at 375 for 15 minutes.\n")
    assert r["ingredients"][:3] == ["Sift:", "1.5 c all purpose flour", "1 c cocoa"]
    assert "Wish:" in r["ingredients"]


def test_french_words_after_the_noun_and_a_hyphen_after_a_comma_end_an_item():
    """Review of 0177: "frais" and "fraîche" come after their noun, so they
    end an item ("persil frais" + "sel" joined since 0168), and a hyphenated
    word after a comma ends one too (", low-sodium" + "salt to taste")."""
    from app.ingestion.pdf_ingest import _clean_ingredients
    for a, b in (("1 bouquet de persil frais", "sel"), ("200 ml de crème fraîche", "ciboulette"),
                 ("1 cup broth, low-sodium", "salt to taste"), ("1 cup yogurt, full-fat", "honey")):
        assert _clean_ingredients([a, b]) == [a, b], (a, b)
    for a, b in (("1 cup flat-leaf", "parsley"), ("1 cup grated", "parmesan"), ("2 cups all-purpose", "flour")):
        assert _clean_ingredients([a, b]) == [f"{a} {b}"], (a, b)


def test_a_pdf_stops_being_read_past_its_ocr_time(monkeypatch, tmp_path):
    """GPT review of 0190: each Tesseract run has its own limit, up to two a
    page, so 60 scanned pages could hold an OCR slot for hours. One PDF's
    scanned pages share 10 minutes; past it the PDF is refused, saying
    where it stopped."""
    from app.ingestion import pdf_read
    path = tmp_path / "scan.pdf"
    pages = [Image.new("RGB", (400, 500), "white") for _ in range(3)]
    pages[0].save(path, save_all=True, append_images=pages[1:])
    read = []
    monkeypatch.setattr(pdf_read, "_ocr_image", lambda img: (read.append(1), ("", None))[1])
    monkeypatch.setattr(pdf_read, "MAX_PDF_OCR_SECONDS", -1)
    with pytest.raises(pdf_read.PdfTooLargeError, match=r"stopped before page 2"):
        pdf_read.extract_pdf_text(str(path))
    assert read == [1]
    monkeypatch.setattr(pdf_read, "MAX_PDF_OCR_SECONDS", 600)
    read.clear()
    assert pdf_read.extract_pdf_text(str(path)).ocr_used_on_pages == [1, 2, 3] and len(read) == 3


def test_reading_the_text_layer_doesnt_use_the_ocr_budget(monkeypatch, tmp_path):
    """DeepSeek review of 0192: the budget is OCR time. A slow first page
    (here 700 s of a 600 s budget spent on its text layer, before any OCR)
    doesn't stop the scanned pages after it."""
    import pdfplumber.page

    from app.ingestion import pdf_read
    path = tmp_path / "scan.pdf"
    pages = [Image.new("RGB", (400, 500), "white") for _ in range(2)]
    pages[0].save(path, save_all=True, append_images=pages[1:])
    clock = [0.0]
    monkeypatch.setattr(pdf_read.time, "monotonic", lambda: clock[0])
    real = pdfplumber.page.Page.extract_text

    def slow_first(self, *a, **k):
        if self.page_number == 1:
            clock[0] += 700
        return real(self, *a, **k)
    monkeypatch.setattr(pdfplumber.page.Page, "extract_text", slow_first)
    monkeypatch.setattr(pdf_read, "_ocr_image", lambda img: ("", None))
    monkeypatch.setattr(pdf_read, "MAX_PDF_OCR_SECONDS", 600)
    result = pdf_read.extract_pdf_text(str(path))
    assert clock[0] >= 700          # the slow page was simulated (pages counted from 1)
    assert result.ocr_used_on_pages == [1, 2]


def test_a_scanned_page_stops_at_the_pdfs_ocr_time(monkeypatch, tmp_path):
    """GPT review of 0204: the time was checked only between pages, so the
    page under way could run up to two Tesseract limits (4 minutes) past
    it. Each run is now cut to what's left of the PDF's OCR time."""
    import time as _time

    from app.ingestion import ocr, pdf_read
    path = tmp_path / "scan.pdf"
    Image.new("RGB", (400, 500), "white").save(path)
    timeouts = []

    def slow(*a, timeout=None, **k):
        timeouts.append(timeout)
        _time.sleep(timeout + 0.02)     # past it on a coarse clock too
        raise RuntimeError("Tesseract process timeout")
    monkeypatch.setattr(ocr.pytesseract, "image_to_data", slow)
    monkeypatch.setattr(ocr.pytesseract, "image_to_osd", slow)
    monkeypatch.setattr(pdf_read, "MAX_PDF_OCR_SECONDS", 0.4)
    started = _time.monotonic()
    with pytest.raises(pdf_read.PdfTooLargeError, match=r"stopped at page 1"):
        pdf_read.extract_pdf_text(str(path))
    assert _time.monotonic() - started < 3 and all(t <= 0.4 for t in timeouts), timeouts
    assert ocr._deadline.get() is None


def test_the_ocr_clock_starts_at_the_first_tesseract_run():
    """Review of 0206: the photos' clock started before they were saved and
    an OCR slot was free, so waiting on a busy server used it up and the
    error said reading took too long when nothing had been read."""
    import time as _time

    from app.ingestion import ocr
    token = ocr.start_deadline(0.3)
    try:
        _time.sleep(0.4)                          # saving, waiting for a slot
        assert 0.25 < ocr.run_timeout() <= 0.3    # the clock starts now
        _time.sleep(0.35)
        with pytest.raises(ocr.OcrTimeUp):
            ocr.run_timeout()
    finally:
        ocr.end_deadline(token)


def test_rendering_a_pdfs_first_scanned_page_counts_against_its_ocr_time(monkeypatch, tmp_path):
    """GPT review of 0210: the PDF's clock started at the first Tesseract
    run, after the first scanned page was rendered, so a page slow to render
    still got a fresh 10 minutes. It starts before rendering now, as
    SECURITY says: past the time, no Tesseract run begins."""
    import time as _time

    import pdfplumber.page

    from app.ingestion import ocr, pdf_read
    path = tmp_path / "scan.pdf"
    Image.new("RGB", (400, 500), "white").save(path)
    runs = []

    def tesseract(*a, timeout=None, **k):
        runs.append(timeout)
        raise RuntimeError("Tesseract process timeout")
    real = pdfplumber.page.Page.to_image

    def slow_render(self, *a, **k):
        _time.sleep(0.5)
        return real(self, *a, **k)
    monkeypatch.setattr(ocr.pytesseract, "image_to_data", tesseract)
    monkeypatch.setattr(ocr.pytesseract, "image_to_osd", tesseract)
    monkeypatch.setattr(pdfplumber.page.Page, "to_image", slow_render)
    monkeypatch.setattr(pdf_read, "MAX_PDF_OCR_SECONDS", 0.3)
    with pytest.raises(pdf_read.PdfTooLargeError, match=r"stopped at page 1"):
        pdf_read.extract_pdf_text(str(path))
    assert runs == []


def _text_stream_pdf(path, chars_per_page, pages=1):
    """A PDF whose pages each hold one compressed run of text: tiny on disk,
    any number of characters (GPT review of 0221)."""
    import zlib
    page_ids = [3 + 2 * i for i in range(pages)]
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % p for p in page_ids) + b"] /Count %d >>" % pages}
    font = 3 + 2 * pages
    for p in page_ids:
        body = zlib.compress(b"BT /F1 1 Tf 0 0 Td (" + b"a" * chars_per_page + b") Tj ET", 9)
        objs[p] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 %d 0 R >> >> "
                   b"/Contents %d 0 R >>" % (font, p + 1))
        objs[p + 1] = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(body) + body + b"\nendstream"
    objs[font] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    out, offsets = b"%PDF-1.4\n", {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % offsets[n] for n in sorted(objs))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(out)
    return path


def test_a_compressed_text_layer_is_refused_before_it_is_laid_out(tmp_path):
    """GPT review of 0221: a 1.6 KB PDF of 1,000,000 characters took 28 s and
    2.2 GB in pdfplumber. The characters are counted first, without keeping
    them, and the page refused at the limit."""
    import resource
    import time
    if sys.platform != "linux":
        pytest.skip("ru_maxrss is in KB on Linux only (bytes on macOS)")
    path = _text_stream_pdf(tmp_path / "bomb.pdf", 1_000_000)
    assert path.stat().st_size < 3000
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t = time.monotonic()
    with pytest.raises(pdf_read.PdfTooLargeError, match=r"Page 1 of this PDF holds more text than a recipe page"):
        pdf_read.extract_pdf_text(str(path))
    assert time.monotonic() - t < 3
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before < 200 * 1024     # KB: under 200 MB more


def test_pdf_text_limits_per_page_in_all_and_in_drawing(tmp_path, monkeypatch):
    path = _text_stream_pdf(tmp_path / "three.pdf", 2_000, pages=3)
    assert "aaaa" in pdf_read.extract_pdf_text(str(path)).raw_text        # under every limit
    monkeypatch.setattr(pdf_read, "MAX_PDF_TEXT_CHARS", 5_000)
    with pytest.raises(pdf_read.PdfTooLargeError, match=r"over 5,000 characters by page 3"):
        pdf_read.extract_pdf_text(str(path))
    monkeypatch.setattr(pdf_read, "MAX_PDF_PAGE_CONTENT_BYTES", 1_000)
    with pytest.raises(pdf_read.PdfTooLargeError, match=r"Page 1 of this PDF holds far more than a recipe"):
        pdf_read.extract_pdf_text(str(path))


def test_the_pdf_text_limit_message_in_french(client, tmp_path):
    path = _text_stream_pdf(tmp_path / "bomb.pdf", 40_000)
    r = client.post("/api/ingest/pdf", files={"file": ("bomb.pdf", path.read_bytes(), "application/pdf")},
                    headers={"X-App-Lang": "fr"})
    assert r.status_code in (400, 413, 422), r.text
    assert r.json()["detail"] == ("La page 1 de ce PDF contient plus de texte qu'une page de recette (plus de 30 000 "
                                  "caractères). Essayez un PDF de la recette seulement.")


def _cmap_pdf(path, cmap_bytes):
    """One page, one character, and a font whose /ToUnicode map is
    cmap_bytes long once inflated (GPT review of 0222)."""
    import zlib
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
            b"1 begincodespacerange <00> <FF> endcodespacerange\n")
    line = b"1 beginbfchar <61> <0061> endbfchar\n"
    cmap += line * (cmap_bytes // len(line)) + b"endcmap end end\n"
    body = zlib.compress(b"BT /F1 12 Tf 72 720 Td (a) Tj ET")
    cm = zlib.compress(cmap, 9)
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> "
               b"/Contents 4 0 R >>",
            4: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(body) + body + b"\nendstream",
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>",
            6: b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(cm) + cm + b"\nendstream"}
    out, offsets = b"%PDF-1.4\n", {}
    for n in sorted(objs):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % offsets[n] for n in sorted(objs))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.write_bytes(out)
    return path


def test_a_fonts_character_map_is_measured_before_pdfminer_parses_it(tmp_path):
    """GPT review of 0222: a 10 MB /ToUnicode map in an 11 KB PDF was decoded
    and parsed by pdfminer before any character was counted."""
    path = _cmap_pdf(tmp_path / "cmap.pdf", 10 * 1024 * 1024)
    assert path.stat().st_size < 50_000
    from unittest.mock import patch as mock_patch
    with mock_patch.object(pdf_read.PDFPageInterpreter, "process_page",
                           side_effect=AssertionError("pdfminer reached")):
        with pytest.raises(pdf_read.PdfTooLargeError,
                           match=r"Page 1 of this PDF holds far more than a recipe \(\d+ MB of font character maps\)"):
            pdf_read.extract_pdf_text(str(path))
    small = _cmap_pdf(tmp_path / "small.pdf", 2_000)
    assert pdf_read._page_streams(PdfReader(str(small)).pages[0], {})[1] > 2_000      # measured, under the limit


def test_a_pdf_pypdf_cant_measure_is_refused(tmp_path, monkeypatch):
    """GPT review of 0222: a pypdf error turned every size limit off."""
    path = _text_stream_pdf(tmp_path / "ok.pdf", 2_000)
    monkeypatch.setattr(pdf_read, "_page_streams", lambda page, sizes: (_ for _ in ()).throw(KeyError("/Bad")))
    with pytest.raises(pdf_read.PdfTooLargeError, match=r"Page 1 of this PDF is damaged and can't be checked"):
        pdf_read.extract_pdf_text(str(path))


def test_a_font_file_two_fonts_share_is_counted_once_a_page(tmp_path):
    """Review of 0222/0223: two fonts naming one /FontFile2 added its size
    twice, refusing a page that decodes it once."""
    import zlib
    font_file = zlib.compress(b"\0" * 50_000)
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    stream = StreamObject()
    stream._data = font_file
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    file_ref = w._add_object(stream)
    from pypdf.generic import DictionaryObject
    desc = w._add_object(DictionaryObject({NameObject("/Type"): NameObject("/FontDescriptor"),
                                           NameObject("/FontFile2"): file_ref}))
    fonts = DictionaryObject()
    for name in ("/F1", "/F2"):
        fonts[NameObject(name)] = w._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/TrueType"),
            NameObject("/BaseFont"): NameObject("/Shared"), NameObject("/FontDescriptor"): desc}))
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    path = tmp_path / "shared.pdf"
    with open(path, "wb") as f:
        w.write(f)
    content, _ = pdf_read._page_streams(PdfReader(str(path)).pages[0], {})
    assert 50_000 <= content < 100_000


@pytest.mark.parametrize("content,cmaps,expected", [
    (21, 1, "21 MB of page content and fonts"),
    (1, 5, "5 MB of font character maps"),
    (15, 5, "5 MB of font character maps"),       # content larger but under its limit
    (21, 5, "21 MB of page content and fonts"),
], ids=["content", "maps", "maps-over-content-larger", "both"])
def test_the_size_refusal_names_the_measure_past_its_limit(tmp_path, monkeypatch, content, cmaps, expected):
    """Review of 0224: the message's number and its name come from the
    same measure, the one over its limit."""
    from app import i18n
    path = _text_stream_pdf(tmp_path / "ok.pdf", 100)
    mb = 1024 * 1024
    monkeypatch.setattr(pdf_read, "_page_streams", lambda page, sizes: (content * mb, cmaps * mb))
    with pytest.raises(pdf_read.PdfTooLargeError) as e:
        pdf_read.extract_pdf_text(str(path))
    assert f"({expected})" in str(e.value)
    fr = i18n.translate(str(e.value), "fr")
    assert fr.startswith("La page 1 de ce PDF contient bien plus qu'une recette (")
    assert ("de contenu et de polices" if "content" in expected else "de tables de caractères des polices") in fr


def _fonts_pdf(path, font_files):
    """One blank page whose resources name one font per stream given, each
    with its own /FontFile2 (data, filter name)."""
    from pypdf.generic import DictionaryObject
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    fonts = DictionaryObject()
    for n, (data, filt) in enumerate(font_files):
        stream = StreamObject()
        stream._data = data
        stream[NameObject("/Filter")] = NameObject(filt)
        desc = w._add_object(DictionaryObject({NameObject("/Type"): NameObject("/FontDescriptor"),
                                               NameObject("/FontFile2"): w._add_object(stream)}))
        fonts[NameObject(f"/F{n}")] = w._add_object(DictionaryObject({
            NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/TrueType"),
            NameObject("/BaseFont"): NameObject(f"/Font{n}"), NameObject("/FontDescriptor"): desc}))
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    with open(path, "wb") as f:
        w.write(f)
    return path


def test_the_page_budget_stops_decoding_once_spent(tmp_path, monkeypatch):
    """GPT review of 0227: twelve run-length font files of 2 MB each were all
    decoded (24 MB) before the 20 MB page total was checked. Each stream is
    now decoded only as far as what's left, and measuring stops past it."""
    two_mb = bytes([129, 65]) * (2 * 1024 * 1024 // 128) + bytes([128])     # run-length: 2 MB of "A"
    path = _fonts_pdf(tmp_path / "rl.pdf", [(two_mb, "/RunLengthDecode")] * 12)
    calls = []
    real = pdf_read._decoded_size
    monkeypatch.setattr(pdf_read, "_decoded_size", lambda s, limit: calls.append(limit) or real(s, limit))
    content, _ = pdf_read._page_streams(PdfReader(str(path)).pages[0], {})
    assert content > pdf_read.MAX_PDF_PAGE_CONTENT_BYTES
    assert len(calls) == 11                                  # the 11th passes the limit; the 12th isn't read
    assert calls[-1] == pdf_read.MAX_PDF_PAGE_CONTENT_BYTES - 10 * 2 * 1024 * 1024     # what was left
    with pytest.raises(pdf_read.PdfTooLargeError, match=r"MB of page content and fonts"):
        pdf_read.extract_pdf_text(str(path))


def test_a_flate_stream_with_a_bad_checksum_is_measured_as_pdfminer_reads_it(tmp_path):
    """GPT review of 0227: a Flate error was measured as nothing, while
    pdfminer's recovery read the whole stream."""
    import zlib
    from pdfminer.pdftypes import decompress_corrupted
    broken = bytearray(zlib.compress(b"x" * 300_000))
    broken[-1] ^= 0xFF                                       # the checksum
    assert len(decompress_corrupted(bytes(broken))) == 300_000
    path = _fonts_pdf(tmp_path / "bad.pdf", [(bytes(broken), "/FlateDecode")])
    assert pdf_read._page_streams(PdfReader(str(path)).pages[0], {})[0] == 300_000
    truncated = zlib.compress(b"y" * 300_000)[:200]          # cut short: what came out before the end
    assert len(pdf_read._inflate(truncated, 10**9)) == len(zlib.decompressobj().decompress(truncated)) > 0


def test_the_bounded_decoders_match_pdfminers(tmp_path):
    import os
    from pdfminer.lzw import lzwdecode
    from pdfminer.runlength import rldecode
    from pypdf._codecs._codecs import LzwCodec
    for data in (b"", b"TOBEORNOTTOBEORTOBEORNOT" * 50, os.urandom(5000), bytes(range(256)) * 300):
        encoded = LzwCodec().encode(data)
        assert pdf_read._lzw(encoded, 10**9) == lzwdecode(encoded) == data
        assert len(pdf_read._lzw(encoded, 100)) == min(len(data), 101)
    runs = bytes([129, 65]) * 1000 + bytes([2]) + b"abc" + bytes([128])
    assert pdf_read._run_length(runs, 10**9) == rldecode(runs)
    assert len(pdf_read._run_length(runs, 500)) == 501
