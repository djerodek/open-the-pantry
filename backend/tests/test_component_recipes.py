"""Recipes in parts with no Ingredients/Method headings, as typed in a word
processor: the main part's ingredients and method paragraph, then a sauce
with its own ingredients and method, and so on. They used to come out as
one long ingredient list with the method paragraphs inside it, and the
title was whichever ingredient line appeared twice."""
from app.ingestion.pdf_ingest import rule_segment_raw_text, segment_raw_text

FRENCH = "\n".join([
    "Galettes de saumon",
    "(Donne environ 8 galettes)",
    "400 g de saumon cuit",
    "250 ml de purée de pommes de terre",
    "15 ml de moutarde de Dijon",
    "Zeste d’un citron",
    "30 ml d’aneth haché",
    "Sel et poivre au goût",
    "Enrobage :",
    "Farine, œufs et chapelure en quantité suffisante.",
    "De l’huile pour la friture",
    "Émietter le saumon. Mélanger tous les ingrédients et former des galettes. (Si le mélange",
    "est trop mou, ajouter de la chapelure). Chauffer l’huile. Enrober les galettes et les frire à",
    "180 °C jusqu’à ce qu’elles soient bien dorées. Servir avec la sauce aux câpres et la mayonnaise Saint-",
    "Laurent.",
    "Sauce aux câpres",
    "125 ml de crème sure",
    "15 ml de câpres hachées",
    "1⁄2 échalote hachée",
    "Sel et poivre au goût",
    "Mélanger tous les ingrédients.",
    "Mayonnaise épicée",
    "100 ml de mayonnaise",
    "10 ml de sriracha",
    "Mélanger tous les ingrédients. Servir avec les galettes.",
])


def test_french_parts_are_grouped_with_their_names():
    s = rule_segment_raw_text(FRENCH)
    assert s["title_guess"] == "Galettes de saumon"
    assert s["servings"] == "environ 8 galettes"
    assert s["ingredients"] == [
        "400 g de saumon cuit", "250 ml de purée de pommes de terre", "15 ml de moutarde de Dijon",
        "Zeste d’un citron", "30 ml d’aneth haché", "Sel et poivre au goût",
        "Enrobage:", "Farine, œufs et chapelure en quantité suffisante.", "De l’huile pour la friture",
        "Sauce aux câpres:", "125 ml de crème sure", "15 ml de câpres hachées", "1⁄2 échalote hachée",
        "Sel et poivre au goût",
        "Mayonnaise épicée:", "100 ml de mayonnaise", "10 ml de sriracha",
    ]
    assert s["steps"] == [
        "Émietter le saumon. Mélanger tous les ingrédients et former des galettes. (Si le mélange est trop mou, "
        "ajouter de la chapelure). Chauffer l’huile. Enrober les galettes et les frire à 180 °C jusqu’à ce "
        "qu’elles soient bien dorées. Servir avec la sauce aux câpres et la mayonnaise Saint-Laurent.",
        "Sauce aux câpres:", "Mélanger tous les ingrédients.",
        "Mayonnaise épicée:", "Mélanger tous les ingrédients. Servir avec les galettes.",
    ]


def test_english_parts():
    raw = "\n".join([
        "Fish Tacos", "Serves 4",
        "1 lb white fish", "1 tsp chili powder", "8 corn tortillas",
        "Season the fish with the chili powder and grill it for about four minutes a side, then flake it",
        "into the warm tortillas.",
        "SLAW",
        "2 cups shredded cabbage", "2 tbsp lime juice", "Salt to taste",
        "Toss everything together and pile it on the tacos.",
    ])
    s = segment_raw_text(raw)
    assert s["title_guess"] == "Fish Tacos" and s["servings"] == "4"
    assert s["ingredients"] == ["1 lb white fish", "1 tsp chili powder", "8 corn tortillas",
                                "Slaw:", "2 cups shredded cabbage", "2 tbsp lime juice", "Salt to taste"]
    assert s["steps"] == ["Season the fish with the chili powder and grill it for about four minutes a side, "
                          "then flake it into the warm tortillas.",
                          "Slaw:", "Toss everything together and pile it on the tacos."]


def test_one_part_recipes_keep_the_old_reading():
    """Ingredients then method, no headings (an app screenshot): unchanged."""
    raw = "\n".join(["Toast", "2 slices bread", "1 tbsp butter",
                     "Toast the bread until golden.", "Spread with butter and serve."])
    s = rule_segment_raw_text(raw)
    assert s["ingredients"] == ["2 slices bread", "1 tbsp butter"]
    assert s["steps"] == ["Toast the bread until golden.", "Spread with butter and serve."]


def test_a_yield_line_is_servings_not_an_ingredient():
    raw = "\n".join(["Cookies", "Ingredients", "Makes 24 cookies", "2 cups flour", "1 cup sugar",
                     "Instructions", "1. Mix.", "2. Bake."])
    s = rule_segment_raw_text(raw)
    assert s["servings"] == "24 cookies" and s["ingredients"] == ["2 cups flour", "1 cup sugar"]


def test_pdf_import_returns_the_servings(client):
    import html
    from weasyprint import HTML
    pdf = HTML(string="<pre>" + html.escape(FRENCH) + "</pre>").write_pdf()
    r = client.post("/api/ingest/pdf", files={"file": ("galettes.pdf", pdf, "application/pdf")})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"] == "Galettes de saumon" and d["servings"] == "environ 8 galettes"
    assert [i["raw_line"] for i in d["ingredients"]][6] == "Enrobage:"
    if d.get("stored_file"):
        client.delete(f"/api/ingest/draft/{d['stored_file']}")


def test_part_names_are_headings_in_the_export(client, sample_recipe_payload):
    r = client.post("/api/recipes", json={
        **sample_recipe_payload, "title": "Parts export",
        "ingredients": [{"raw_line": l, "name": l} for l in ["1 cup rice", "Sauce:", "2 tbsp soy sauce"]],
        "steps": ["Cook the rice.", "Sauce:", "Mix the sauce.", "Pour it over."]})
    rid = r.json()["id"]
    html = client.get(f"/api/recipes/{rid}/export.html").text
    assert '<li class="part">Sauce:</li>' in html
    assert '<li value="2">Mix the sauce.</li>' in html and '<li value="3">Pour it over.</li>' in html
    client.delete(f"/api/recipes/{rid}")


def test_the_page_shows_part_names_as_headings():
    import pathlib
    js = (pathlib.Path(__file__).resolve().parents[2] / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert "function isPartName(text)" in js
    assert 'el("li", { value: String(++n), text: s.text })' in js
