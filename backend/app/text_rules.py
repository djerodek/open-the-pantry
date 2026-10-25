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


def is_steps_only(raw_text, steps) -> bool:
    """True when a recipe's stored original text is nothing but its own
    steps: an emailed link saved before the app kept the link's address
    (0047) stored the page's instructions as its original text, which no
    import ever had to sort into a title, ingredients and steps. Learning
    and the import corpus leave such text out (2026-10-25: four of the
    owner's emailed recipes)."""
    norm = lambda t: " ".join((t or "").split()).casefold()     # noqa: E731
    lines = [norm(l) for l in (raw_text or "").splitlines() if norm(l)]
    saved = {norm(s) for s in steps or [] if norm(s)}
    return bool(lines) and bool(saved) and all(l in saved for l in lines)
