"""The quick reference charts' content (frontend/js/reference-data.json, 0233).

What a typo would get wrong at the stove: a metric figure that isn't the
amount beside it, a French line with other numbers than the English one,
a °F that isn't the °C, or a Health Canada temperature changed by mistake.
How the page shows the numbers (kg, min/kg, French decimals) is checked
in tests_browser/test_pages.py."""
import json
import re
from pathlib import Path

DATA = json.loads((Path(__file__).resolve().parents[2] / "frontend" / "js" / "reference-data.json")
                  .read_text(encoding="utf-8"))

GLYPHS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125}

# Canadian kitchen measures (1 cup = 250 mL), written the way Canadian
# recipes print them: ⅓ cup is 75 mL and ¾ cup 175 mL, not 83 and 188.
CANADIAN_ML = {
    ("tsp", 0.125): 0.5, ("tsp", 0.25): 1, ("tsp", 0.5): 2, ("tsp", 1.5): 7,
    ("cup", 0.25): 60, ("cup", 1 / 3): 75, ("cup", 0.5): 125, ("cup", 0.75): 175, ("cup", 1.25): 300,
}
BASE_ML = {"tsp": 5, "tbsp": 15, "cup": 250}
UNIT = {"tsp": "tsp", "tbsp": "tbsp", "cup": "cup", "cups": "cup", "c. à thé": "tsp", "c. à soupe": "tbsp",
        "tasse": "cup", "tasses": "cup", "lb": "lb", "oz": "oz"}

QTY = r"(\d+(?:[.,]\d+)?|\d*[½¼¾⅓⅔⅛])"
NUM = r"(\d+(?:[.,]\d+)?)"
AMOUNT = {
    "en": re.compile(rf"{QTY}(?: to {QTY})? (tsp|tbsp|cups?|lb|oz) \({NUM}(?: to {NUM})? (mL|g)\)"),
    "fr": re.compile(rf"{QTY}(?: à {QTY})? (c\. à thé|c\. à soupe|tasses?|lb|oz) \({NUM}(?: à {NUM})? (mL|g)\)"),
}
# Any unit at all, to catch one written without its metric figure.
ANY_UNIT = {
    "en": re.compile(r"\b(tsp|tbsp|cups?)\b|[\d½¼¾⅓⅔⅛] (lb|oz)\b"),
    "fr": re.compile(r"c\. à (thé|soupe)|\btasses?\b|[\d½¼¾⅓⅔⅛] (lb|oz)\b"),
}
DEGREES = re.compile(r"(\d+) ?°([CF]) \((\d+) ?°([CF])\)")


def qty(s):
    s = s.replace(",", ".")
    if s[-1] in GLYPHS:
        return (int(s[:-1]) if s[:-1] else 0) + GLYPHS[s[-1]]
    return float(s)


def expected_metric(unit, q):
    if unit == "lb":
        return 450 * q
    if unit == "oz":
        return round(q * 28.35)
    return CANADIAN_ML.get((unit, q), BASE_ML[unit] * q)


def bilingual(node, path="data"):
    """Every {"en": ..., "fr": ...} object in the file, with where it is."""
    if isinstance(node, dict):
        if "en" in node and isinstance(node["en"], str):
            yield path, node
        for k, v in node.items():
            yield from bilingual(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from bilingual(v, f"{path}[{i}]")


def entry_texts():
    for chart in ("baking", "cooking"):
        for i, e in enumerate(DATA[chart]["entries"]):
            texts = [e["item"], *e["use"]] + ([e["note"]] if "note" in e else [])
            for j, text in enumerate(texts):
                yield f"{chart}[{i}].{j}", text


def test_every_text_is_in_both_languages():
    found = 0
    for path, node in bilingual(DATA):
        found += 1
        assert node["en"].strip() and node.get("fr", "").strip(), path
    assert found > 250


def test_every_amount_has_its_canadian_metric_figure():
    checked = 0
    for where, text in entry_texts():
        for lang in ("en", "fr"):
            s = text[lang]
            matches = list(AMOUNT[lang].finditer(s))
            assert len(matches) == len(ANY_UNIT[lang].findall(s)), f"{where} {lang}: an amount without its metric figure: {s}"
            for m in matches:
                lo, hi, unit, mlo, mhi = m.group(1), m.group(2), UNIT[m.group(3)], m.group(4), m.group(5)
                pairs = [(lo, mlo)] + ([(hi, mhi)] if hi else [])
                assert bool(hi) == bool(mhi), f"{where} {lang}: {m.group(0)}"
                for q, metric in pairs:
                    want = expected_metric(unit, qty(q))
                    assert abs(qty(metric) - want) <= max(0.01, want * 0.01), f"{where} {lang}: {m.group(0)} (expected {want})"
                    checked += 1
    assert checked > 150


def test_the_french_line_has_the_english_line_s_numbers():
    """Every number, not just the measures: "let stand 5 minutes",
    "by 25°F (14°C)", "1 slice"."""
    def numbers(text):
        return sorted(re.findall(r"\d+(?:\.\d+)?", re.sub(r"(\d),(\d)", r"\1.\2", text)))

    for where, text in entry_texts():
        assert numbers(text["en"]) == numbers(text["fr"]), f"{where}: {text}"


def test_every_degree_pair_written_in_the_text_agrees():
    checked = 0
    for path, node in bilingual(DATA):
        for lang in ("en", "fr"):
            s = node[lang]
            for m in DEGREES.finditer(s):
                a, ua, b, ub = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
                c, f = (a, b) if ua == "C" else (b, a)
                assert {ua, ub} == {"C", "F"}, f"{path} {lang}: {m.group(0)}"
                # "lower the oven by 25°F (14°C)" is a difference, not a reading.
                if re.search(r"(by|de) $", s[:m.start()]):
                    assert abs(f - c * 9 / 5) <= 1, f"{path} {lang}: {m.group(0)}"
                else:
                    assert abs(f - (c * 9 / 5 + 32)) <= 1, f"{path} {lang}: {m.group(0)}"
                checked += 1
    assert checked >= 10


def test_pounds_written_in_the_text_have_their_kilograms():
    pattern = re.compile(r"(\d+) (?:to|à) (\d+) lb \((\d+[.,]\d) (?:to|à) (\d+[.,]\d) kg\)")
    found = 0
    for path, node in bilingual(DATA):
        for lang in ("en", "fr"):
            for m in pattern.finditer(node[lang]):
                for lb, kg in ((m.group(1), m.group(3)), (m.group(2), m.group(4))):
                    assert round(int(lb) * 0.45359237, 1) == qty(kg), f"{path} {lang}: {m.group(0)}"
                found += 1
    assert found == 2   # the thawing note, both languages


def test_health_canada_temperatures():
    t = DATA["meat"]["temps"]
    assert [(x["c"], x["f"]) for x in t["beef"]] == [(63, 145), (71, 160), (77, 170)]
    assert [x["label"]["en"] for x in t["beef"]] == ["medium-rare", "medium", "well done"]
    for key, (c, f) in {"63": (63, 145), "70": (70, 158), "71": (71, 160), "74": (74, 165), "82": (82, 180)}.items():
        assert t[key] == [{"c": c, "f": f}]
    for levels in t.values():
        for x in levels:
            assert abs(x["f"] - (x["c"] * 9 / 5 + 32)) <= 1


def _section(title):
    return next(s for s in DATA["meat"]["sections"] if s["title"]["en"] == title)


def test_each_meat_row_points_at_health_canada_s_temperature_for_it():
    for title in ("Beef", "Lamb", "Veal"):
        assert {r["temp"] for r in _section(title)["rows"]} == {"beef"}
    for title in ("Pork", "Smoked ham, to cook before eating", "Fresh ham, uncooked"):
        assert {r["temp"] for r in _section(title)["rows"]} == {"71"}, title
    # Owner, 2026-11-23: no temperature for a fully cooked ham, an asterisk instead.
    cooked = _section("Smoked ham, fully cooked, to reheat")["rows"]
    assert {(r["temp"], r.get("mark")) for r in cooked} == {("cooked", "cooked")}
    for r in _section("Poultry")["rows"]:
        whole = "whole" in r["cut"]["en"].lower()
        assert r["temp"] == ("82" if whole else "74"), r["cut"]["en"]
    assert not any("country" in r["cut"]["en"].lower() for s in DATA["meat"]["sections"] for r in s["rows"])


def test_the_7_to_9_lb_leg_of_lamb_is_fsis_s_figure():
    """FoodSafety.gov's 10–15 min/lb would leave a 7 lb leg 70 to 105
    minutes short of the 5–7 lb row's 20–25 min/lb."""
    rows = [r for r in _section("Lamb")["rows"] if r.get("lb") == [7, 9]]
    assert len(rows) == 1 and rows[0]["time"] == {"per_lb": [15, 20]} and rows[0]["mark"] == "fsis"


def test_ranges_run_upwards_and_ovens_agree():
    def up(values):
        return all(a < b for a, b in zip(values, values[1:]))

    for s in DATA["meat"]["sections"]:
        for r in s["rows"]:
            name = f"{s['title']['en']} / {r['cut']['en']}"
            for key in ("lb", "oz"):
                if key in r:
                    assert up(r[key]) and r[key][0] > 0, name
            time = r["time"]
            assert ("per_lb" in time) != ("total" in time), name
            assert up(time.get("per_lb") or time["total"]), name
            assert len(r["oven"]["f"]) == len(r["oven"]["c"]), name
            for f, c in zip(r["oven"]["f"], r["oven"]["c"]):
                assert abs(c - (f - 32) * 5 / 9) <= 1, name
            assert r["temp"] in DATA["meat"]["temps"], name
            if "mark" in r:
                assert r["mark"] in DATA["meat"]["marks"], name
    turkey = DATA["meat"]["turkey"]["rows"]
    thaw = DATA["meat"]["thawing"]["rows"]
    for rows in (turkey, thaw):
        assert all(a["lb"][1] == b["lb"][0] for a, b in zip(rows, rows[1:]))   # no gaps between sizes
    for r in turkey:
        assert up(r["unstuffed"]) and (r["stuffed"] is None or up(r["stuffed"]))
    for r in thaw:
        assert up(r["fridge_days"]) and up(r["water_hours"])


def test_every_source_named_exists_and_links_out():
    sources = DATA["sources"]
    for chart in ("baking", "cooking", "meat"):
        for sid in DATA[chart]["sources"]:
            assert sid in sources, (chart, sid)
    for chart in ("baking", "cooking"):
        listed = set(DATA[chart]["sources"])
        for e in DATA[chart]["entries"]:
            assert e["src"] and set(e["src"]) <= listed, e["item"]["en"]
    for sid, s in sources.items():
        urls = [s["url"]] if isinstance(s["url"], str) else [s["url"]["en"], s["url"]["fr"]]
        assert all(u.startswith("https://") for u in urls), sid
        assert s["title"]["en"] and s["publisher"]["en"] and s["date"]["en"], sid
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", DATA["checked"])
