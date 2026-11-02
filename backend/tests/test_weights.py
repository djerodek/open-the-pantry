"""0150: the recipe view's Weights button (volume amounts shown as grams,
from frontend/js/weights.js). Runs app.js's functions in Node, as the
scaling tests do (skipped without Node)."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _run(cases):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    js = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    weights = (FRONTEND / "js" / "weights.js").read_text(encoding="utf-8")
    block = js[js.index("  const UNICODE_FRACTIONS"):js.index("  // The three buttons; onChange(factor)")]
    part = js[js.index("  function isPartName("):js.index("  function renderRecipeDetail(")]
    src = ("const self = globalThis;\n" + weights + part + block +
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
        ("1/2 cup buttermilk", "115 g buttermilk"),
        ("1 1/2 cups bread flour", "180 g bread flour"),
        ("1-2 cups flour", "120-240 g flour"),
        ("1/8 tsp cayenne pepper", "0.2 g cayenne pepper"),
        ("1 tsp ground cinnamon", "2.5 g ground cinnamon"),
        ("1 cup water or broth", "225 g water or broth"),
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
        ("250 ml de lait", "240 g de lait"),
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
    "3/4 tsp each salt and pepper", "1 cup lettuce, shredded", "2 tbsp taco seasoning", "1 stick cinnamon",
    "1/2 cup white wine and sea salt",
])
def test_volumes_it_cant_weigh_are_left_as_written(text):
    (kind, out), = _run([[text, 1]])
    assert (kind, out) == ("kept", text) or (text.startswith("1/2 cup white wine") and kind == "converted")


@pytest.mark.parametrize("text", ["2 eggs", "200 g sugar", "8 oz cream cheese", "Salt to taste", "Sauce:", ""])
def test_weights_counts_and_plain_lines_are_unchanged(text):
    (kind, out), = _run([[text, 1]])
    assert (kind, out) == ("same", text)


def test_the_table():
    """Every entry has names and a positive weight (or None to leave a line
    as written); no name is in two entries."""
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
        for name in e["en"] + e["fr"]:
            assert name == name.lower() and name.strip() == name, name
            assert seen.setdefault(name, i) == i, f"{name} in two entries"
    assert len(table) > 150


def test_the_page_loads_the_table_and_the_service_worker_keeps_it():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert html.index('src="js/weights.js"') < html.index('src="js/app.js"')
    assert '"/js/weights.js"' in (FRONTEND / "service-worker.js").read_text(encoding="utf-8")
