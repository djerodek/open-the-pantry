"""Small text rules shared by the importers, the review hints, the exports
and (as a copy in app.js and picker.js, checked by test_ui_consistency)
the pages."""
import re

# An ingredient line's usual ending: "Salt, to taste", "Parsley, for
# garnish". Such a line is an ingredient, never a step or a part name.
INGREDIENT_TAIL = re.compile(
    r"\b(to taste|for serving|to serve|for garnish|to garnish|for dusting|optional|as needed|au go[uû]t|pour servir|pour garnir|pour d[ée]corer|facultatif|au besoin|en quantit[ée] suffisante|pour (?:la )?friture|for frying|for deep[- ]frying)\b", re.IGNORECASE)


def is_part_name(text) -> bool:
    """"Sauce tartare:", "For the dough:": a part of the recipe, shown as a
    small heading. A short line ending in ":", but not an ingredient that
    happens to end in one ("Salt and pepper to taste:", DeepSeek review of
    0096)."""
    t = (text or "").strip()
    return t.endswith(":") and len(t.split()) <= 6 and not INGREDIENT_TAIL.search(t)
