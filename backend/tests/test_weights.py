"""0150: the recipe view's Weights button (volume amounts shown as grams,
from frontend/js/weights.js). Runs amounts.js's functions in Node, as the
scaling tests do (skipped without Node)."""
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def amounts_js():
    """weights.js and amounts.js, for Node, with amounts.js's functions as
    globals (0155: shared by the main app and What to eat?)."""
    return ("const self = globalThis;\n" + (FRONTEND / "js" / "weights.js").read_text(encoding="utf-8")
           + (FRONTEND / "js" / "amounts.js").read_text(encoding="utf-8")
           + "\nconst { isPartName, scaleLine, scaleLineInfo, scaleServings, weightLineInfo, volumeLineInfo } = self.OTP_AMOUNTS;\n")


def _run(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = (amounts_js() +
           f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([t, f]) => {{"
           "const s = scaleLineInfo(t, f || 1); if (s.kept) return ['scaled-kept', s.text];"
           "const w = weightLineInfo(s.text); return [w.kept ? 'kept' : w.converted ? 'converted' : 'same', w.text]; })));")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_volumes_become_grams():
    cases = [
        ("1 cup all-purpose flour", "120 g all-purpose flour"),
        ("1 cup packed brown sugar", "215 g packed brown sugar"),
        ("2 tbsp butter", "28 g butter"),
        ("1 stick unsalted butter, softened", "115 g unsalted butter, softened"),
        ("1/2 cup peanut butter", "135 g peanut butter"),
        ("1/2 cup buttermilk", "125 g buttermilk"),       # 245 g a cup (0156)
        ("1 1/2 cups bread flour", "180 g bread flour"),
        ("1-2 cups flour", "120-240 g flour"),
        ("1/8 tsp cayenne pepper", "0.2 g cayenne pepper"),
        ("1 tsp ground cinnamon", "2.5 g ground cinnamon"),
        ("1 cup water or broth", "235 g water or broth"),
        ("45 ml (3 tablespoons) olive oil", "38 g (3 tablespoons) olive oil"),
        ("2 tsp chopped fresh rosemary", "1 g chopped fresh rosemary"),
        ("½ tsp dried rosemary", "0.6 g dried rosemary"),
        ("2 tbsp oil from the jar of sun-dried tomatoes", "25 g oil from the jar of sun-dried tomatoes"),
        ("1/2 cup sun-dried tomatoes in oil", "55 g sun-dried tomatoes in oil"),
    ]
    got = _run([[t, 1] for t, _ in cases])
    for (text, want), (kind, out) in zip(cases, got):
        assert (kind, out) == ("converted", want), text


def test_french_measures():
    """A tasse is the 250 ml Canadian cup; c. à soupe / c. à table 15 ml;
    "de" stays."""
    cases = [
        ("1 tasse de farine", "125 g de farine"),
        ("2 c. à soupe de beurre", "29 g de beurre"),
        ("1 c. à table de miel", "21 g de miel"),
        ("1/2 c. à thé de sel", "3 g de sel"),
        ("250 ml de lait", "260 g de lait"),             # 245 g a cup (0156)
        ("1,5 tasse de sucre", "315 g de sucre"),
    ]
    got = _run([[t, 1] for t, _ in cases])
    for (text, want), (kind, out) in zip(cases, got):
        assert (kind, out) == ("converted", want), text


def test_after_scaling():
    got = _run([["1 cup flour", 2], ["3/4 cup sugar", 0.5]])
    assert got == [["converted", "240 g flour"], ["converted", "74 g sugar"]]


@pytest.mark.parametrize("text", [
    "1 tsp kosher salt", "1 cup ice cream", "1 tsp cream of tartar", "2 cups sugar snap peas",
    "3/4 tsp each salt and pepper", "1 cup lettuce, shredded", "2 tbsp taco seasoning",
])
def test_volumes_it_cant_weigh_are_left_as_written(text):
    (kind, out), = _run([[text, 1]])
    assert (kind, out) == ("kept", text)


def test_only_the_name_before_and_is_weighed():
    """The name part ends at "and"/"or": wine is weighed, not the salt."""
    assert _run([["1/2 cup white wine and sea salt", 1]]) == [["converted", "120 g white wine and sea salt"]]


@pytest.mark.parametrize("text", ["2 eggs", "200 g sugar", "8 oz cream cheese", "Salt to taste", "Sauce:", ""])
def test_weights_counts_and_plain_lines_are_unchanged(text):
    (kind, out), = _run([[text, 1]])
    assert (kind, out) == ("same", text)


def test_the_table():
    """Every entry has names and a positive weight (or None to leave a line
    as written); no name is in two entries, accents aside (matching folds
    them, so an accented copy of a name does nothing)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    weights = (FRONTEND / "js" / "weights.js").read_text(encoding="utf-8")
    out = subprocess.run([node, "-e", "const self = globalThis;\n" + weights +
                          "\nconsole.log(JSON.stringify(self.OTP_INGREDIENT_WEIGHTS));"],
                         capture_output=True, text=True, timeout=20)
    table = json.loads(out.stdout)
    seen = {}
    for i, e in enumerate(table):
        assert e["en"] or e["fr"]
        assert e["g"] is None or 0 < e["g"] < 400, e
        names = [unicodedata.normalize("NFD", n).encode("ascii", "ignore").decode() for n in e["en"] + e["fr"]]
        assert len(set(names)) == len(set(e["en"] + e["fr"])), f"a name twice, accents aside, in {e}"
        for name in e["en"] + e["fr"]:
            assert name == name.lower() and name.strip() == name, name
        for name in names:
            assert seen.setdefault(name, i) == i, f"{name} in two entries"
    assert len(table) > 150


def test_the_page_loads_the_table_and_the_service_worker_keeps_it():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert html.index('src="js/weights.js"') < html.index('src="js/amounts.js"') < html.index('src="js/app.js"')
    assert '"/js/weights.js"' in (FRONTEND / "service-worker.js").read_text(encoding="utf-8")


FRENCH_SPOONS = ["c. à soupe", "c. à s.", "c.à.s.", "c. à. s.", "c.à s.", "c. a s.", "c. s.", "c.s.", "c. à table",
                 "cuillère à soupe", "cuillères à soupe", "cuil. à soupe",
                 "c. à thé", "c. à café", "c. à t.", "c.à.t.", "c. à c.", "c.à.c.", "c. t.", "c.t.", "c. c.", "c.c.",
                 "cuillère à thé", "cuil. à thé", "cuillerée à café"]


def test_french_spoons_agree_with_the_ingredient_parser():
    """DeepSeek review of 0151: "c. s." was read as a cup ("c."), 16 times
    too much. Every spoon form the parser reads, Weights reads as the same
    spoon: 1 of each of salt is 6 g (tsp) or 18 g (tbsp), never a cup."""
    from app.ingestion.ingredient_parser import parse_ingredient_line
    got = _run([[f"1 {u} sel", 1] for u in FRENCH_SPOONS])
    for unit, (kind, out) in zip(FRENCH_SPOONS, got):
        parsed = parse_ingredient_line(f"1 {unit} sel")
        assert parsed["unit"] in ("tbsp", "tsp") and parsed["name"] == "sel", (unit, parsed)
        assert (kind, out) == ("converted", {"tbsp": "18 g sel", "tsp": "6 g sel"}[parsed["unit"]]), (unit, out)


@pytest.mark.parametrize("text,want", [("1 c. sucre", "200 g sucre"), ("1 c. flour", "120 g flour"),
                                       ("1 c. sugar", "200 g sugar"), ("1 c sucre", "1 c sucre")])
def test_c_dot_alone_is_still_a_cup(text, want):
    """"c." before a word is a cup; "c. s." is not; a bare "c" is nothing."""
    assert _run([[text, 1]])[0][1] == want


def _weigh(cases):
    """[[text, lang], ...] through weightLineInfo with the recipe's language."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = amounts_js() + (f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([t, l]) => {{"
                          "const w = weightLineInfo(t, l); return [w.kept ? 'kept' : w.converted ? 'converted' : 'same', w.text]; })));")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.parametrize("text,want", [
    ("2 cups cooked rice", ("converted", "315 g cooked rice")),            # 158 g a cup cooked, not 198 dry
    ("1 cup cooked brown rice", ("converted", "195 g cooked brown rice")),
    ("1 cup cooked quinoa", ("converted", "185 g cooked quinoa")),
    ("1 cup cooked lentils", ("converted", "200 g cooked lentils")),
    ("1 cup cooked basmati rice", ("kept", "1 cup cooked basmati rice")),  # cooked, not in the table
    ("1 cup whipped cream", ("kept", "1 cup whipped cream")),
    ("1 cup granulated sugar substitute", ("kept", "1 cup granulated sugar substitute")),
    ("1 cup peanut butter chips", ("converted", "170 g peanut butter chips")),
    ("1/2 cup oil-packed sun-dried tomatoes", ("converted", "55 g oil-packed sun-dried tomatoes")),
    ("1 cup chopped red pepper", ("kept", "1 cup chopped red pepper")),    # a bell pepper, not black pepper
    ("1/8 tsp ground red pepper", ("converted", "0.2 g ground red pepper")),
    ("1 cup heavy whipping cream", ("converted", "240 g heavy whipping cream")),   # "whipping" isn't "whipped"
    ("1 cup cooked chicken", ("converted", "140 g cooked chicken")),       # "cooked" is part of the name
    ("1 cup half and half", ("converted", "240 g half and half")),         # not cut at "and"
    ("1 cup 00 flour", ("converted", "115 g 00 flour")),                   # "00" isn't a second amount
    ("500 ml d'eau", ("converted", "500 g d'eau")),                        # water 1 g per ml
    ("1 tasse d'eau", ("converted", "250 g d'eau")),
    ("0 cup flour", ("converted", "0 g flour")),                           # GPT review of 0155
    ("1 stick cinnamon", ("same", "1 stick cinnamon")),                    # a stick is a volume for butter only
    ("1 bâton de cannelle", ("same", "1 bâton de cannelle")),
    ("1 stick butter", ("converted", "115 g butter")),
])
def test_more_weight_conversions(text, want):
    """Claude review of 0155: words that change what a cup weighs, names
    that couldn't be matched, liquids at the 8-oz convention, cinnamon
    sticks counted as volumes; GPT's zero."""
    assert tuple(_weigh([[text, "en"]])[0]) == want


def test_french_recipes_get_a_decimal_comma():
    assert _weigh([["1/8 c. à thé de sel", "fr"], ["1/8 tsp salt", "en"], ["1,5 tasse de sucre", "en"]]) == [
        ["converted", "0,8 g de sel"], ["converted", "0.8 g salt"], ["converted", "315 g de sucre"]]


def test_scaling_reads_00_flour_too():
    run = _run([["1 cup 00 flour", 2]])
    assert run == [["converted", "230 g 00 flour"]]        # 2 x 116 g
