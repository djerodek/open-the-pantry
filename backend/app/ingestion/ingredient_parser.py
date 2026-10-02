import re

# French (Quebec) spoon measures: "c. à soupe", "c. à s.", "c. à table",
# "cuillère(s) à soupe"; "c. à thé", "c. à café", "c. à c.". Listed first:
# they contain spaces and dots, and "c" alone must not be taken for a unit.
_FR_SPOON = r"(?:c\.?\s*[àa]\.?\s*|cuil\.\s*[àa]\s*|cuill?(?:[eè]re|er[ée]e)s?\s+[àa]\s+)"
FR_TBSP = _FR_SPOON + r"(?:soupe|table|s\.)"
FR_TSP = _FR_SPOON + r"(?:th[ée]|caf[ée]|t\.|c\.)"

UNIT_WORDS = [
    FR_TBSP, FR_TSP, r"c\.\s*s\.", r"c\.\s*c\.",
    "tasses?", "pinc[ée]es?", "gousses?", "bo[îi]tes?", "tranches?", "bottes?", "paquets?",
    "sachets?", "livres?", "grammes?", "kilos?",
    "cups?", "tbsp", "tablespoons?", "tsp", "teaspoons?",
    "g", "grams?", "kg", "kilograms?",
    "oz", "ounces?", "lb", "lbs", "pounds?",
    "ml", "milliliters?", "l", "liters?", "litres?",
    "pinch(?:es)?", "dash(?:es)?", "clove(?:s)?", "can(?:s)?",
    "slice(?:s)?", "stick(?:s)?", "bunch(?:es)?", "head(?:s)?",
    "package(?:s)?", "pkg", "jar(?:s)?",
]
UNIT_PATTERN = r"(?:%s)" % "|".join(UNIT_WORDS)

# Collapses spelled-out/plural variants to one canonical token per unit, so
# "tablespoon", "tablespoons", and "tbsp" are all stored/filterable the same
# way instead of as separate strings.
UNIT_CANONICAL = {
    "cup": "cup", "cups": "cup",
    "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "pinch": "pinch", "pinches": "pinch",
    "dash": "dash", "dashes": "dash",
    "clove": "clove", "cloves": "clove",
    "can": "can", "cans": "can",
    "slice": "slice", "slices": "slice",
    "stick": "stick", "sticks": "stick",
    "bunch": "bunch", "bunches": "bunch",
    "head": "head", "heads": "head",
    "package": "package", "packages": "package", "pkg": "package",
    "jar": "jar", "jars": "jar",
}

# Quantity: integer, decimal, simple fraction, unicode fraction, or a range ("1-2", "1 to 2")
_UFRAC = r"[\u00BC-\u00BE\u2150-\u215E]"   # ¼ ½ ¾ ⅓ ⅔ ⅛ ...
QTY_PATTERN = (
    r"(\d+\s*" + _UFRAC +      # "1½", "1 ½" -- before plain "\d+", or "1 ½" parsed as 1
    r"|\d+\s*\d*/\d+"       # "1 1/2"
    r"|\d+[\.,]\d+"         # "1.5", French "1,5"
    r"|\d+"                 # "2"
    r"|[\u00BC-\u00BE\u2150-\u215E]"  # unicode fraction chars e.g. ¼ ½ ¾
    r")"
    r"(?:\s*(?:-|–|to)\s*\d+\s*(?:\d*/\d+|" + _UFRAC + r")?)?"  # optional range "-2" / "to 2" / "–2½"
)

LINE_RE = re.compile(
    rf"^\s*(?P<qty>{QTY_PATTERN})?\s*(?:\((?P<paren>[^)]*)\)\s*)?(?P<unit>{UNIT_PATTERN})?\.?\s+(?P<name>.+?)\s*$",
    re.IGNORECASE,
)


_FR_CANONICAL = [
    (re.compile(FR_TBSP + "$|^c\\.\\s*s\\.$", re.I), "tbsp"),
    (re.compile(FR_TSP + "$|^c\\.\\s*c\\.$", re.I), "tsp"),
    (re.compile(r"^tasses?$", re.I), "cup"),
    (re.compile(r"^pinc[ée]es?$", re.I), "pinch"),
    (re.compile(r"^gousses?$", re.I), "clove"),
    (re.compile(r"^bo[îi]tes?$", re.I), "can"),
    (re.compile(r"^tranches?$", re.I), "slice"),
    (re.compile(r"^bottes?$", re.I), "bunch"),
    (re.compile(r"^(?:paquets?|sachets?)$", re.I), "package"),
    (re.compile(r"^livres?$", re.I), "lb"),
    (re.compile(r"^grammes?$", re.I), "g"),
    (re.compile(r"^kilos?$", re.I), "kg"),
]


def _canonical_unit(unit: str) -> str:
    u = unit.strip().lower()
    if u in UNIT_CANONICAL:
        return UNIT_CANONICAL[u]
    for pattern, canon in _FR_CANONICAL:
        if pattern.match(u):
            return canon
    return u


def parse_ingredient_line(raw_line: str) -> dict:
    """
    Parse a single raw ingredient line into quantity/unit/name using regex heuristics.
    No LLM. Falls back to storing the whole line as `name` if quantity/unit aren't
    confidently detected -- still searchable/displayable, just not filterable by amount.

    A parenthetical between quantity and unit (e.g. "1 (15 oz) can diced
    tomatoes") is relocated into the name rather than left blocking unit
    detection -- this deliberately doesn't try to extract structured data
    from *inside* the parenthetical (that would need real nested-quantity
    parsing); it just stops "(15 oz)" from preventing "can" from being
    recognized as the unit.
    """
    line = raw_line.strip().lstrip("-*\u2022").strip()  # strip leading bullet chars
    if not line:
        return {"raw_line": raw_line, "quantity": None, "unit": None, "name": ""}

    match = LINE_RE.match(line)
    if not match or not match.group("name"):
        return {"raw_line": raw_line, "quantity": None, "unit": None, "name": line}

    qty = match.group("qty")
    unit = match.group("unit")
    paren = match.group("paren")
    name = match.group("name").strip()

    if paren:
        name = f"{name} ({paren.strip()})"

    unit_normalized = None
    if unit:
        unit_normalized = _canonical_unit(unit)
        # "2 c. à soupe d'huile", "1 tasse de farine": the "de"/"d'" belongs
        # to the unit, not the ingredient's name.
        # French puts the size after the unit: "1 boîte (540 ml) de pois chiches".
        after = re.match(r"^\(([^)]*)\)\s*(.+)$", name)
        if after:
            name = f"{after.group(2)} ({after.group(1).strip()})"
        name = re.sub(r"^(?:de\s+|d['\u2019]\s*)", "", name, flags=re.IGNORECASE) or name

    return {
        "raw_line": raw_line,
        "quantity": qty.strip() if qty else None,
        "unit": unit_normalized,
        "name": name,
    }


def parse_ingredient_block(lines: list[str]) -> list[dict]:
    return [parse_ingredient_line(line) for line in lines if line.strip()]
