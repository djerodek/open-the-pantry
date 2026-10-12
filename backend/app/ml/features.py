"""What the learned models look at.

Lines (for the segmenter): the words and letter groups of the line, its
shape (starts with a quantity, has a unit, ends a sentence, all capitals,
starts with a cooking verb...), where it sits in the text, the same shape
facts about the lines either side, and what the built-in rules decided for
it and its neighbours. Giving the model the rules' answer means it learns
where the rules go wrong for this user's sources, rather than everything
from scratch.

Recipes (for tags): the words of the title, the ingredients and the steps,
kept apart so "chicken" in the title counts differently from "chicken
stock" in step 4.
"""
from __future__ import annotations

import math
import re
import unicodedata

from ..ingestion import pdf_ingest as P
from ..ingestion.ingredient_parser import UNIT_PATTERN
from ..ingestion.tagger import _fold

_WORD = re.compile(r"\w+", re.UNICODE)
_UNIT = re.compile(rf"(?<!\w){UNIT_PATTERN}(?!\w)", re.IGNORECASE)
_FRACTION = re.compile(r"[¼½¾⅓⅔⅛⅜⅝⅞]|\d+/\d+")


def words(text: str) -> list[str]:
    return _WORD.findall(_fold(text))


def _first_word_folded(line: str) -> str:
    stripped = P._MARKER_GARBAGE.sub("", line).strip()
    if not stripped:
        return ""
    first = unicodedata.normalize("NFKD", stripped.split()[0].lower())
    return "".join(c for c in first if not unicodedata.combining(c)).strip(",.;:!")


def _len_bucket(n: int) -> str:
    for limit, name in ((1, "1"), (2, "2"), (3, "3"), (6, "4-6"), (12, "7-12"), (25, "13-25")):
        if n <= limit:
            return name
    return "26+"


def line_shape(line: str) -> list[str]:
    """Yes/no facts about one line, as feature names."""
    n_words = len(line.split())
    letters = [c for c in line if c.isalpha()]
    out = [f"len:{_len_bucket(n_words)}"]
    checks = {
        "qty": P._QTY_START.match(line),
        "qtylead": P.QUANTITY_LEAD.match(line),
        "unit": _UNIT.search(line),
        "fraction": _FRACTION.search(line),
        "period": re.search(r"[.!?]\s*$", line),
        "colon": line.rstrip().endswith(":"),
        "upper": letters and all(c.isupper() for c in letters),
        "title_case": line.istitle(),
        "lower_start": line[:1].islower(),
        "verb": _first_word_folded(line) in P._STEP_VERBS,
        "prose": P._is_prose(line),
        "junk": P._is_junk(line),
        "head_ing": P.HEADING_INGREDIENTS.match(line),
        "head_steps": P.HEADING_STEPS.match(line),
        "steps_end": P._STEPS_END.match(line),
        "step_start": P._STEP_START.match(line),
        "numbered": P.NUMBERED_STEP.match(line),
        "bare_number": P._BARE_NUMBER.match(line),
        "byline": P._BYLINE.match(line),
        "section": P._looks_like_section_label(line),
        "ing_tail": P._INGREDIENT_TAIL.search(line),
        "open_paren": line.count("(") > line.count(")"),
        "url": re.search(r"https?://|www\.|\.com\b", line, re.IGNORECASE),
        "price": re.search(r"\$\d", line),
    }
    out += [f"is:{k}" for k, v in checks.items() if v]
    return out


def line_features(lines: list[str], rule_labels: list[str]) -> list[dict[str, float]]:
    """One feature dict per line."""
    shapes = [line_shape(l) for l in lines]
    n = len(lines)
    seen_ing = seen_steps = False
    rows = []
    for i, line in enumerate(lines):
        f: dict[str, float] = {}
        ws = words(P._MARKER_GARBAGE.sub("", line))
        scale = 1.0 / math.sqrt(max(len(ws), 1))
        for w in ws:
            key = "w:" + ("#" if w.isdigit() else w)
            f[key] = f.get(key, 0.0) + scale
        for a, b in zip(ws, ws[1:]):
            f[f"b:{a}_{b}"] = scale
        if ws:
            f["first:" + ("#" if ws[0].isdigit() else ws[0])] = 1.0
            f["last:" + ("#" if ws[-1].isdigit() else ws[-1])] = 1.0
        folded = " " + _fold(line)[:60] + " "
        for j in range(len(folded) - 2):
            f["c:" + folded[j:j + 3]] = f.get("c:" + folded[j:j + 3], 0.0) + 0.3 * scale
        for s in shapes[i]:
            f[s] = 1.0
        f[f"pos:{min(int(10 * i / max(n, 1)), 9)}"] = 1.0
        if i == 0:
            f["pos:first"] = 1.0
        if i == n - 1:
            f["pos:last"] = 1.0
        if seen_ing:
            f["after:head_ing"] = 1.0
        if seen_steps:
            f["after:head_steps"] = 1.0
        f["rule:" + rule_labels[i]] = 2.0
        for off, tag in ((-1, "p"), (1, "n"), (-2, "pp"), (2, "nn")):
            j = i + off
            if 0 <= j < n:
                f[f"{tag}rule:{rule_labels[j]}"] = 1.0
                if abs(off) == 1:
                    for s in shapes[j]:
                        f[f"{tag}:{s}"] = 1.0
                    f[f"{tag}rule_same:{rule_labels[j] == rule_labels[i]}"] = 1.0
            else:
                f[f"{tag}:none"] = 1.0
        if "is:head_ing" in shapes[i]:
            seen_ing = True
        if "is:head_steps" in shapes[i]:
            seen_steps = True
        rows.append(f)
    return rows


def recipe_features(title: str, ingredients: list[str], step_text: str) -> dict[str, float]:
    """Bag of words for the tag model, log-scaled and normalised."""
    counts: dict[str, float] = {}

    def add(prefix, text, weight):
        ws = words(text)
        for w in ws:
            if len(w) > 1 and not w.isdigit():
                counts[prefix + w] = counts.get(prefix + w, 0.0) + weight
        for a, b in zip(ws, ws[1:]):
            if not a.isdigit() and not b.isdigit():
                counts[f"{prefix}{a}_{b}"] = counts.get(f"{prefix}{a}_{b}", 0.0) + weight * 0.5

    add("t:", title or "", 1.0)
    for ing in ingredients:
        add("i:", ing or "", 1.0)
    add("s:", step_text or "", 0.5)
    feats = {k: math.log1p(v) for k, v in counts.items()}
    norm = math.sqrt(sum(v * v for v in feats.values())) or 1.0
    return {k: 3.0 * v / norm for k, v in feats.items()}
