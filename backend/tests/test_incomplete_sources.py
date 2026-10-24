"""A PDF or screenshot that gives only part of a recipe (no ingredients or
no steps) is reported as a failed import, like a half recipe from a URL.
The case that prompted it: a PDF of a recipe blog post that stopped before
the recipe card -- ingredients described in prose, no steps.

app.* is imported inside tests only; see the note in test_backup.py.
"""
import io
from email.message import EmailMessage

import pytest


@pytest.fixture(autouse=True)
def _isolated_data_dir(data_dir):
    pass


ARTICLE = """
<h1>Cowboy Butter Recipe</h1>
<p>This butter is great on steak, chicken and grilled vegetables. Make it two ways.</p>
<h2>Ingredients</h2>
<p>Butter. Use your favorite brand of salted or unsalted butter. This recipe calls for 1 stick.</p>
<p>Garlic. I love lots of fresh minced garlic, though you can use garlic powder.</p>
<p>Lemon. Both lemon juice and lemon zest.</p>
<h2>Storage</h2>
<p>Cowboy butter will last up to 5 days in the refrigerator in a sealed container.</p>
"""

WHOLE = """
<h1>Garlic Butter</h1>
<h2>Ingredients</h2><p>1 stick butter</p><p>3 cloves garlic, minced</p>
<h2>Instructions</h2><p>1. Melt the butter over low heat.</p><p>2. Stir in the garlic and serve.</p>
"""


def _pdf(html):
    from weasyprint import HTML
    return HTML(string=html).write_pdf()


def _drop(client, draft):
    for key in ("stored_file", "image_path"):
        if draft.get(key):
            client.delete(f"/api/ingest/draft/{draft[key]}")


def test_pdf_without_steps_is_reported_but_keeps_the_draft(client):
    r = client.post("/api/ingest/pdf", files={"file": ("article.pdf", _pdf(ARTICLE), "application/pdf")})
    d = r.json()
    assert r.status_code == 200
    assert d["incomplete"] == {"missing": ["steps"],
                               "message": "This PDF didn't give a complete recipe (no steps found)."}
    assert d["ingredients"], "the partial draft is still there for Continue anyway"
    _drop(client, d)

    fr = client.post("/api/ingest/pdf", files={"file": ("article.pdf", _pdf(ARTICLE), "application/pdf")},
                     headers={"X-App-Lang": "fr"}).json()
    assert fr["incomplete"]["message"] == "Ce PDF ne donne pas une recette complète (aucune étape trouvée)."
    _drop(client, fr)


def test_whole_pdf_is_not_flagged(client):
    d = client.post("/api/ingest/pdf", files={"file": ("whole.pdf", _pdf(WHOLE), "application/pdf")}).json()
    assert d["incomplete"] is None and d["steps"]
    _drop(client, d)


def test_pdf_batch_fails_a_half_recipe_and_saves_nothing(client):
    before = len(client.get("/api/recipes").json())
    r = client.post("/api/ingest/pdf/batch", files=[
        ("files", ("article.pdf", _pdf(ARTICLE), "application/pdf")),
        ("files", ("whole.pdf", _pdf(WHOLE), "application/pdf")),
    ]).json()
    assert [s["filename"] for s in r["succeeded"]] == ["whole.pdf"]
    assert r["failed"][0]["filename"] == "article.pdf"
    assert r["failed"][0]["error"] == ("This PDF didn't give a complete recipe (no steps found). Add it on its own "
                                       "from screenshots of the recipe (Screenshot/Photo), or enter it by hand.")
    assert len(client.get("/api/recipes").json()) == before + 1


def _screenshot(lines):
    from PIL import Image, ImageDraw
    from .conftest import text_font
    img = Image.new("L", (700, 60 + 40 * len(lines)), color=255)
    draw = ImageDraw.Draw(img)
    for n, line in enumerate(lines):
        draw.text((20, 20 + 40 * n), line, fill=0, font=text_font(28, bold=True))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_screenshot_of_only_the_ingredients_is_reported(client):
    shot = _screenshot(["Garlic Butter", "Ingredients", "1 stick butter", "3 cloves garlic"])
    d = client.post("/api/ingest/image", files={"file": ("s1.png", shot, "image/png")}).json()
    assert d["incomplete"]["missing"] == ["steps"]
    assert d["incomplete"]["message"] == "This image didn't give a complete recipe (no steps found)."
    _drop(client, d)


def test_email_pdf_with_half_a_recipe_fails_with_advice(tmp_path):
    from app.ingestion import email_processing
    m = EmailMessage()
    m["Subject"] = "[RECIPE]"
    m.set_content("")
    m.add_attachment(_pdf(ARTICLE), maintype="application", subtype="pdf", filename="article.pdf")
    result = email_processing.process_tagged_email(m, str(tmp_path))
    assert result["success"] is False
    assert "article.pdf: only part of a recipe (no steps found)" in result["error"]
    assert result["error"].endswith("If the PDF or photo stops before the recipe card, send one that includes it. "
                                    "Otherwise add the recipe in the app, from screenshots or by hand.")
    assert list(tmp_path.iterdir()) == []


def test_email_reason_is_translated():
    from app import i18n
    msg = ("No recipe found. Tried: article.pdf: only part of a recipe (no steps found); body: empty. "
           "If the PDF or photo stops before the recipe card, send one that includes it. "
           "Otherwise add the recipe in the app, from screenshots or by hand.")
    fr = i18n.translate(msg, "fr")
    assert "seulement une partie d'une recette (aucune étape trouvée)" in fr
    assert "only part" not in fr and "stops before" not in fr


def test_a_page_without_markup_in_french_or_english():
    """0123: "Ingrédients" / "Préparation" / "Étapes" weren't headings to the
    last-resort link reader, so a French page with no recipe markup gave
    nothing. A time heading ("Temps de préparation : 15 min") isn't the
    steps; a heading with a number that isn't a time still counts."""
    from app.ingestion.url_ingest import parse_html
    page = ("<h1>Soupe</h1><h3>Temps de préparation : 15 min</h3><p>Cuisson : 40 min</p>"
            "<h2>Ingrédients (pour 4)</h2><ul><li>1 oignon</li><li>2 tasses de bouillon</li></ul>"
            "<h2>{steps}</h2><ol><li>Faire revenir l'oignon.</li><li>Ajouter le bouillon.</li></ol>")
    for heading in ("Préparation", "Étapes", "Mode de préparation", "Méthode", "Marche à suivre"):
        r = parse_html(page.format(steps=heading), "https://example.com/soupe")
        assert r.ingredients == ["1 oignon", "2 tasses de bouillon"], heading
        assert r.steps == ["Faire revenir l'oignon.", "Ajouter le bouillon."], heading
    en = parse_html("<h1>Soup</h1><h4>Prep time 15 minutes</h4><p>Serves 4</p><h2>Ingredients</h2><ul><li>1 onion</li></ul>"
                    "<h2>Method</h2><p>Soften the onion.</p>", "https://example.com/soup")
    assert en.ingredients == ["1 onion"] and en.steps == ["Soften the onion."]
