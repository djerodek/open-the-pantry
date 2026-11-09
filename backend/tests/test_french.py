"""French recipes: tagging, sections, units. Matching ignores accents and
case, since OCR and phone keyboards are inconsistent about them.

app.* is imported inside tests only; see the note in test_backup.py.
"""
import pytest


@pytest.mark.parametrize("title,ingredients,steps,expect,forbid", [
    ("Poulet au vinaigre", ["1 poulet en morceaux", "250 ml de bouillon de poulet"],
     "Faire dorer dans une poêle. Mijoter 30 minutes.", {"Chicken", "Stovetop"}, {"Vegetarian"}),
    ("Pâté chinois", ["1 lb de bœuf haché", "maïs en crème"], "Cuire au four à 375 °F.", {"Beef", "Oven"}, {"Vegetarian"}),
    ("PATE CHINOIS", ["1 LB DE BOEUF HACHE"], "CUIRE AU FOUR", {"Beef", "Oven"}, set()),
    ("Soupe aux légumes", ["bouillon de légumes", "carottes"], "Mijoter à feu doux.", {"Vegetarian", "Stovetop"}, set()),
    ("Ragoût de porc", ["2 lb d'épaule de porc", "500 ml de bouillon de poulet"], "", {"Pork"}, {"Chicken"}),
    ("Pad thaï au tofu", ["tofu", "sauce de poisson"], "Faire sauter.", set(), {"Fish", "Vegetarian"}),
    ("Moules marinières", ["2 lb de moules"], "", {"Fish"}, set()),
    ("Gâteau aux carottes", ["farine", "carottes"], "Beurrer deux moules à gâteau.", {"Vegetarian"}, {"Fish"}),
    ("Gigot d'agneau", ["1 gigot d'agneau", "ail"], "", set(), {"Vegetarian"}),
    ("Déjeuner du dimanche", ["oeufs", "bacon"], "", {"Breakfast", "Pork"}, set()),
    ("Souper rapide", ["haut de cuisse de poulet"], "", {"Dinner", "Chicken"}, set()),
    ("Four cheese pasta", ["four cups milk", "pasta"], "Simmer.", {"Stovetop"}, {"Oven"}),  # English "four"
])
def test_french_tagging(title, ingredients, steps, expect, forbid):
    from app.ingestion.tagger import suggest_tags
    names = {n for n, _, _ in suggest_tags(title, ingredients, steps)}
    assert expect <= names and not (forbid & names), names


FRENCH_RECIPE = """Poulet au vinaigre
Portions : 4
Ingrédients
2 c. à soupe de beurre
1 poulet de 1,5 kg, coupé en 8 morceaux
125 ml (1/2 tasse) de vinaigre de vin rouge
250 ml (1 tasse) de bouillon de poulet
1 c. à thé de thym séché
Sel et poivre au goût
Préparation
Étape 1
Dans une grande poêle, dorer le poulet dans le beurre. Saler et poivrer.
Étape 2
Déglacer avec le vinaigre et réduire de moitié. Ajouter le bouillon et le thym.
Étape 3
Couvrir et laisser mijoter 30 minutes.
Valeur nutritive
Calories 420
Commentaires (12)
Délicieux!"""


def test_french_recipe_sections():
    from app.ingestion.pdf_ingest import segment_raw_text
    s = segment_raw_text(FRENCH_RECIPE)
    assert s["title_guess"] == "Poulet au vinaigre"
    assert s["ingredients"][0] == "2 c. à soupe de beurre"
    assert "Sel et poivre au goût" in s["ingredients"]
    assert len(s["steps"]) == 3
    assert s["steps"][0].startswith("Dans une grande poêle")
    assert s["steps"][2] == "Couvrir et laisser mijoter 30 minutes."     # stops at "Valeur nutritive"


def test_french_heading_variants():
    from app.ingestion.pdf_ingest import HEADING_INGREDIENTS, HEADING_STEPS
    for h in ["Préparation", "Preparation :", "Étapes", "Etapes", "Méthode", "Mode de préparation", "Marche à suivre"]:
        assert HEADING_STEPS.match(h), h
    for h in ["Ingrédients", "Ingredients :", "INGRÉDIENTS"]:
        assert HEADING_INGREDIENTS.match(h), h
    assert not HEADING_STEPS.match("Étape 1")


@pytest.mark.parametrize("line,qty,unit,name", [
    ("2 c. à soupe d'huile d'olive", "2", "tbsp", "huile d'olive"),
    ("1 c. à thé de sel", "1", "tsp", "sel"),
    ("1 c. à s. de miel", "1", "tbsp", "miel"),
    ("2 c. à c. de cumin", "2", "tsp", "cumin"),
    ("3 cuillères à soupe de beurre", "3", "tbsp", "beurre"),
    ("1,5 tasse de farine", "1,5", "cup", "farine"),
    ("1 pincée de muscade", "1", "pinch", "muscade"),
    ("3 gousses d'ail, hachées", "3", "clove", "ail, hachées"),
    ("1 boîte (540 ml) de pois chiches", "1", "can", "pois chiches (540 ml)"),
    ("500 g de bœuf haché", "500", "g", "bœuf haché"),
    ("1 (15 oz) can chickpeas", "1", "can", "chickpeas (15 oz)"),    # English unchanged
])
def test_french_units(line, qty, unit, name):
    from app.ingestion.ingredient_parser import parse_ingredient_line
    r = parse_ingredient_line(line)
    assert (r["quantity"], r["unit"], r["name"]) == (qty, unit, name)


@pytest.mark.parametrize("tail", ["Salt and pepper to taste", "Fresh herbs, for serving",
                                  "Flour for dusting the pan", "Sel et poivre au goût", "Persil frais pour garnir"])
def test_unmeasured_ingredient_is_not_step_one(tail):
    """DeepSeek review: with no headings, a quantity-less ingredient right
    after the measured ones read as method text and became step 1."""
    from app.ingestion.pdf_ingest import segment_raw_text
    raw = "\n".join(["Test Stew", "2 lb beef chuck", "2 onions", "3 carrots", tail,
                     "Brown the beef in a heavy pot over high heat.", "Add the onions and carrots and simmer 2 hours."])
    s = segment_raw_text(raw)
    assert s["steps"][0].startswith("Brown the beef"), s


def test_ocr_reads_french_when_available():
    import pytesseract

    from app.ingestion.pdf_ingest import ocr_lang
    assert ocr_lang() == ("eng+fra" if "fra" in pytesseract.get_languages(config="") else "eng")


def test_unmeasured_ingredient_is_kept_as_an_ingredient():
    from app.ingestion.pdf_ingest import segment_raw_text
    raw = "\n".join(["Test Stew", "2 lb beef chuck", "2 onions", "Salt and pepper to taste",
                     "Brown the beef in a heavy pot over high heat."])
    assert segment_raw_text(raw)["ingredients"] == ["2 lb beef chuck", "2 onions", "Salt and pepper to taste"]


FR = {"X-App-Lang": "fr"}


def _recipe(client, title, notes=None, **extra):
    body = {"title": title, "source_type": "manual",
            "ingredients": [{"raw_line": "2 cups flour"}], "steps": ["Mix."]}
    body.update(extra)
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    if notes is not None:
        assert client.patch(f"/api/recipes/{rid}/notes", json={"notes": notes}).status_code == 200
    return rid


def test_translation_keeps_every_set_cookie():
    import asyncio

    from starlette.requests import Request
    from starlette.responses import StreamingResponse

    from app.main import interface_language

    async def call_next(request):      # what Starlette hands a middleware: a streamed body
        body = b'{"detail": "Not found"}'

        async def chunks():
            yield body
        r = StreamingResponse(chunks(), media_type="application/json", headers={"content-length": str(len(body))})
        r.set_cookie("a", "1")
        r.set_cookie("b", "2")
        return r
    scope = {"type": "http", "method": "GET", "path": "/x", "headers": [(b"x-app-lang", b"fr")],
             "query_string": b""}
    out = asyncio.run(interface_language(Request(scope), call_next))
    cookies = [v for k, v in out.raw_headers if k == b"set-cookie"]
    assert len(cookies) == 2
    lengths = [v for k, v in out.raw_headers if k == b"content-length"]
    assert lengths == [str(len(out.body)).encode()]


def test_a_compressed_json_response_is_left_alone():
    import asyncio

    from starlette.requests import Request
    from starlette.responses import Response

    from app import main
    body = b"\x1f\x8b compressed"
    resp = Response(content=body, media_type="application/json", headers={"Content-Encoding": "gzip"})

    async def call_next(request):
        return resp
    req = Request({"type": "http", "method": "GET", "path": "/api/x", "headers": [(b"x-app-lang", b"fr")],
                   "query_string": b""})
    out = asyncio.run(main.interface_language(req, call_next))
    assert out is resp


def test_error_messages_follow_the_app_language(client):
    assert client.get("/api/recipes/999999").json()["detail"] == "Recipe not found"
    assert client.get("/api/recipes/999999", headers=FR).json()["detail"] == "Recette introuvable"
    # ?lang= for a plain navigation that can't send the header
    assert client.get("/api/recipes/999999?lang=fr").json()["detail"] == "Recette introuvable"


def test_unknown_language_falls_back_to_english(client):
    r = client.get("/api/recipes/999999", headers={"X-App-Lang": "de"})
    assert r.json()["detail"] == "Recipe not found"


def test_composed_messages_are_translated_part_by_part():
    from app import i18n
    msg = ("Sending (SMTP) failed: Could not connect/login to SMTP (mail.x.example:465, implicit TLS): "
           "timed out -- Diagnosis: mail.x.example:465 accepts the connection but never answers. "
           "Usually a firewall or proxy in between, or the wrong port.\n"
           "Reading: OK -- logged in to the inbox. Recipes can still be ingested; only the result "
           "notifications can't be sent until sending works.")
    fr = i18n.translate(msg, "fr")
    assert fr.startswith("L'envoi (SMTP) a échoué : Connexion ou authentification SMTP impossible "
                         "(mail.x.example:465, TLS implicite) : timed out -- Diagnostic : ")
    assert "Lecture : OK." in fr and "Sending" not in fr and "Reading" not in fr
    assert i18n.translate(msg, "en") == msg


def test_scan_result_lines_are_translated():
    from app import i18n
    assert i18n.translate('OK: Pie (from "[RECIPE] pie", via PDF attachment)', "fr") == \
        "OK : Pie (de « [RECIPE] pie », par la pièce jointe PDF)"
    assert i18n.translate('FAILED: "x": No recipe found. Tried: body: empty.', "fr") == \
        'ÉCHEC : "x": Aucune recette trouvée. Essais : corps : vide.'


def test_large_json_and_non_json_responses_pass_through(client):
    r = client.get("/api/recipes", headers=FR)
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_validation_errors_are_translated(client):
    rid = _recipe(client, "Temps Invalide")
    en = client.patch(f"/api/recipes/{rid}/rating", json={"actual_cook_time": "00:30:00"})
    fr = client.patch(f"/api/recipes/{rid}/rating", json={"actual_cook_time": "00:30:00"}, headers=FR)
    assert en.status_code == fr.status_code == 422
    assert "Hours must be under 24" in en.json()["detail"][0]["msg"]
    assert fr.json()["detail"][0]["msg"].startswith("Les heures doivent être inférieures à 24")


def test_the_count_message_reads_in_french():
    from app.i18n import translate
    assert translate("This archive has more than 20000 files; the limit is 20000.", "fr") == \
        "Cette archive contient plus de 20000 fichiers; la limite est 20000."
