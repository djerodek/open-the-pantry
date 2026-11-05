"""0154: the recipe view's Volumes choice (weights shown as cups/spoons,
from frontend/js/weights.js, in the recipe's language). Runs amounts.js's
functions in Node, as test_weights.py does (skipped without Node)."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from .test_weights import amounts_js

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _run(cases):
    """cases: [text, lang, factor] -> [[kind, text], ...]"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    src = (amounts_js() +
           f"\nconsole.log(JSON.stringify({json.dumps(cases)}.map(([t, lang, f]) => {{"
           "const s = scaleLineInfo(t, f || 1); if (s.kept) return ['scaled-kept', s.text];"
           "const v = volumeLineInfo(s.text, lang); return [v.kept ? 'kept' : v.converted ? 'converted' : 'same', v.text]; })));")
    out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _check(cases, lang):
    got = _run([[t, lang, 1] for t, _ in cases])
    for (text, want), (kind, out) in zip(cases, got):
        assert (kind, out) == ("converted", want), text


def test_weights_become_us_volumes_in_english():
    _check([
        ("200 g flour", "1 2/3 cups flour"),            # 1.67 cups at 120 g
        ("250 g all-purpose flour", "2 cups all-purpose flour"),
        ("100 g sugar", "1/2 cup sugar"),
        ("15 g butter", "1 tbsp butter"),
        ("5 g salt", "3/4 tsp salt"),
        ("1 g cinnamon", "1/2 tsp cinnamon"),
        ("0.3 g cayenne pepper", "1/8 tsp cayenne pepper"),   # never "0"
        ("1 lb butter", "2 cups butter"),
        ("8 oz milk", "1 cup milk"),
        ("1 kg flour", "8 1/3 cups flour"),
        ("200g sugar", "1 cup sugar"),
        ("200-250 g flour", "1 2/3-2 cups flour"),      # one unit for the range
        ("100 grams of oats", "1 cup of oats"),
    ], "en")


def test_weights_become_french_volumes_in_french():
    _check([
        ("125 g de farine", "1 tasse de farine"),       # 250 ml tasse
        ("500 g de sucre", "2 1/3 tasses de sucre"),
        ("30 g de beurre", "2 c. à soupe de beurre"),
        ("5 g de sel", "3/4 c. à thé de sel"),
        ("240 g d'eau", "1 tasse d'eau"),
        ("190 g de farine", "1 1/2 tasse de farine"),   # singular below 2
        ("1 livre de beurre", "2 tasses de beurre"),
    ], "fr")


def test_after_scaling():
    assert _run([["100 g sugar", "en", 2], ["200 g flour", "en", 0.5]]) == [
        ["converted", "1 cup sugar"], ["converted", "3/4 cup flour"]]


@pytest.mark.parametrize("text", ["200 g chicken breast", "100 g kosher salt", "1,000 g flour",
                                  "1 lb 8 oz flour", "30 g each salt and pepper", "50 g harissa"])
def test_weights_it_cant_measure_are_left_as_written(text):
    assert _run([[text, "en", 1]]) == [["kept", text]]


@pytest.mark.parametrize("text", ["2 cups flour", "250 ml milk", "8 fl oz cream", "2 eggs", "Salt to taste", "Sauce:", ""])
def test_volumes_counts_and_plain_lines_are_unchanged(text):
    assert _run([[text, "en", 1]]) == [["same", text]]


def test_weights_and_volumes_round_trip_closely():
    """Volumes then Weights lands near the start for common baking amounts."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    for text, lang in (("1 cup flour", "en"), ("1/2 cup sugar", "en"), ("2 tbsp butter", "en"), ("1 tasse de farine", "fr")):
        src = (amounts_js() +
               f"\nconsole.log(volumeLineInfo(weightLineInfo({json.dumps(text)}).text, {json.dumps(lang)}).text);")
        out = subprocess.run([node, "-e", src], capture_output=True, text=True, timeout=20)
        assert out.stdout.strip() == text, (text, out.stdout, out.stderr)


@pytest.mark.parametrize("text,want", [
    ("1-100 g flour", "1/2 tsp-3/4 cup flour"),           # 1 g isn't 1/4 cup (GPT review of 0155)
    ("1-1000 g flour", "1/2 tsp-8 1/3 cups flour"),
    ("10 à 200 g de farine", "1 1/2 c. à soupe à 1 1/2 tasse de farine"),
    ("200-250 g flour", "1 2/3-2 cups flour"),            # close ends keep one unit
    ("100-120 g sugar", "1/2-2/3 cup sugar"),
])
def test_broad_ranges_get_a_unit_for_each_end(text, want):
    lang = "fr" if " de " in text else "en"
    assert _run([[text, lang, 1]]) == [["converted", want]]


def test_cooked_and_whipped_in_volumes_too():
    assert _run([["400 g de riz cuit", "fr", 1], ["400 g de riz basmati cuit", "fr", 1],
                 ["100 g whipped cream", "en", 1]]) == [
        ["converted", "2 1/3 tasses de riz cuit"], ["kept", "400 g de riz basmati cuit"], ["kept", "100 g whipped cream"]]
