"""The review queue: recipes saved without anyone checking them.

Batch links, batch PDFs and emailed recipes are saved straight from the
parser (there's no one at the review screen), so they're flagged "to
review" (Recipe.needs_review). The flag clears when the recipe is saved
from Edit or marked "Looks right". Recipes still to review are left out of
learning (ml/service._examples): they're the parser's own output, and
training on them would teach the model its own mistakes.

hints() lists what is most likely wrong in one, for the review view. It
points at places to look; it doesn't decide anything.
"""
from __future__ import annotations

import re

from . import models
from .text_rules import is_part_name as _part_name

OCR_CHECK_BELOW = 80          # the confidence badge's "high" tier starts here
SHORT_STEP = 12               # characters
LONG_STEP = 700
# Lines that rightly have no amount.
_NO_AMOUNT_OK = re.compile(
    r"\b(to taste|as needed|for serving|for garnish|optional|pinch|dash|au go[uû]t|au besoin|"
    r"pour servir|pour garnir|facultatif|une pinc[ée]e|un trait)\b", re.I)
_AMOUNT = re.compile(r"[\d¼½¾⅓⅔⅛⅜⅝⅞]|\b(a|an|one|two|three|four|five|six|half|un|une|deux|trois|quatre|cinq|six|demi)\b",
                     re.I)



def title_guessed(recipe: models.Recipe) -> bool:
    """Read from the text (a PDF, a photo, an email body), not from a web
    page's recipe data."""
    return recipe.source_type in ("pdf", "screenshot", "email") and not recipe.source_url and bool(recipe.raw_text)


def hints(recipe: models.Recipe, uncertain: set[str] | None = None) -> dict:
    """What to check. `uncertain`: lines of the original text the learned
    segmenter wasn't sure about (None when no learned model is in use)."""
    from .ml.segmenter import MATCH, _tokens
    unsure_tokens = [t for t in (_tokens(l) for l in (uncertain or ())) if t]

    def unsure(text: str) -> bool:
        t = _tokens(text)
        return bool(t) and any(len(t & u) / len(t) >= MATCH for u in unsure_tokens)

    ingredients = []
    for k, ing in enumerate(recipe.ingredients):
        line = ing.raw_line or ing.name or ""
        if _part_name(line):
            continue
        if not ing.quantity and not _AMOUNT.search(line) and not _NO_AMOUNT_OK.search(line):
            ingredients.append({"index": k, "text": line, "reason": "no_amount"})
        elif unsure(line):
            ingredients.append({"index": k, "text": line, "reason": "uncertain"})
    steps = []
    for k, step in enumerate(recipe.steps):
        text = step.text or ""
        if _part_name(text):
            continue
        if len(text.strip()) < SHORT_STEP:
            steps.append({"index": k, "text": text, "reason": "short"})
        elif len(text) > LONG_STEP:
            steps.append({"index": k, "text": text[:120], "reason": "long"})
        elif unsure(text):
            steps.append({"index": k, "text": text[:120], "reason": "uncertain"})
    conf = recipe.ocr_confidence
    return {
        "needs_review": bool(recipe.needs_review),
        "title_guessed": title_guessed(recipe),
        "servings_missing": not (recipe.servings or "").strip(),
        "ocr_confidence": round(conf) if conf is not None and conf < OCR_CHECK_BELOW else None,
        "ingredients": ingredients,
        "steps": steps,
        "no_ingredients": not recipe.ingredients,
        "no_steps": not recipe.steps,
        "line_scores": uncertain is not None,
    }
