"""The learned segmenter: which line of an imported text is the title, an
ingredient, part of a step, or neither.

Training data is the user's own saved recipes. For one imported from a
photo, PDF or email, the original text is kept (raw_text) next to what was
saved after the review screen, so lining the two up labels every line of
the original -- including the corrections made by hand. Recipes typed in or
read from a web page's recipe data have no such text; their ingredient and
step lines are used too, at a lower weight, so the model knows the user's
vocabulary.

The model is a logistic regression over each line's features (features.py),
followed by a Viterbi pass so sections stay in one piece. Its output goes
through the same clean-up as the rules' (wrapped lines joined, step numbers
removed). It is only used when it beats the rules on the user's recipes in
cross-validation; otherwise the rules carry on alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from ..ingestion import pdf_ingest as P
from . import linear
from .features import line_features, words

LABELS = ["other", "title", "ingredient", "step"]
OTHER, TITLE, ING, STEP = range(4)

MIN_IMPORTED = 8          # imported recipes (with their original text) needed before it's tried
MIN_GAIN = 0.02           # must beat the rules by this much (content-line accuracy) to be used
PSEUDO_WEIGHT = 0.3       # weight of lines from recipes without an original text
# An OCR'd empty checkbox in front of an ingredient: "LJ 2 eggs", "L Salt".
_CHECKBOX = re.compile(r"^\s*(?:[LJIl|\[\]_]{1,3}|[☐□○])\s+(?=[\d¼½¾⅓⅔⅛⅜⅝⅞A-ZÀ-ÖØ-Þ])")
# Share of a line's words found in a saved item for it to count as that
# item. Below 1 so a line whose OCR typo was corrected on the review screen
# still matches (one word in four may differ). A judgement call, not tuned
# on data: a mislabelled line only adds noise to training, and the
# cross-validation against the rules decides whether the model is used.
MATCH = 0.75


@dataclass
class Example:
    """A saved recipe as training material."""
    recipe_id: int
    title: str
    ingredients: list[str]
    steps: list[str]
    raw_text: str | None = None
    lines: list[str] = field(default_factory=list)
    labels: list[int] = field(default_factory=list)
    rule_labels: list[int] = field(default_factory=list)
    weight: float = 1.0
    imported: bool = False


def _tokens(text: str) -> set[str]:
    text = P._MARKER_GARBAGE.sub("", P._STEP_START.sub("", text))
    return set(words(text))


def align(lines: list[str], title: str, ingredients: list[str], steps: list[str]) -> tuple[list[int], float, float]:
    """Label each line by the saved item it belongs to. Returns the labels
    and the share of saved ingredients and steps that some line matched
    (low when the text isn't really the source of what was saved)."""
    title_t = _tokens(title or "")
    items = [(ING, k, _tokens(s)) for k, s in enumerate(ingredients)] + \
            [(STEP, k, _tokens(s)) for k, s in enumerate(steps)]
    items = [it for it in items if it[2]]
    labels: list[int | None] = []
    ambiguous: dict[int, set[int]] = {}
    hit_ing, hit_step = set(), set()
    for i, line in enumerate(lines):
        t = _tokens(line)
        if not t:
            labels.append(OTHER)
            continue
        if title_t:
            inter = len(t & title_t)
            if inter / len(t) >= 0.8 and inter / len(title_t) >= 0.8:
                labels.append(TITLE)
                continue
        best = {ING: (0.0, None), STEP: (0.0, None)}
        for lab, k, s in items:
            inter = len(t & s)
            c = inter / len(t)
            if c < MATCH:
                continue
            score = c + 0.5 * inter / len(s)
            if score > best[lab][0]:
                best[lab] = (score, k)
        (si, ki), (ss, ks) = best[ING], best[STEP]
        if not si and not ss:
            labels.append(OTHER)
            continue
        if si and ss:
            ambiguous[i] = ING if si > ss else STEP
            labels.append(None)
        else:
            labels.append(ING if si > ss else STEP)
        if si:
            hit_ing.add(ki)
        if ss:
            hit_step.add(ks)
    # Lines that fit both ("salt and pepper." ends a step, and is an
    # ingredient): take the section of the line before, else the one after,
    # else the closer match.
    for i in sorted(ambiguous):
        prev = labels[i - 1] if i > 0 and labels[i - 1] in (ING, STEP) else None
        nxt = next((labels[j] for j in range(i + 1, len(labels)) if labels[j] in (ING, STEP)), None)
        labels[i] = prev if prev is not None else (nxt if nxt is not None else ambiguous[i])
    # A step number on a line of its own belongs to the step after it.
    for i, line in enumerate(lines):
        if labels[i] == OTHER and P._BARE_NUMBER.match(line):
            nxt = next((labels[j] for j in range(i + 1, len(labels)) if labels[j] != OTHER), None)
            if nxt == STEP:
                labels[i] = STEP
    n_ing = sum(1 for lab, _k, _s in items if lab == ING)
    n_step = len(items) - n_ing
    cov_ing = len(hit_ing) / n_ing if n_ing else 1.0
    cov_step = len(hit_step) / n_step if n_step else 1.0
    return [int(l) for l in labels], cov_ing, cov_step


def rule_labels(lines: list[str], rule_result: dict) -> list[int]:
    labels, _, _ = align(lines, rule_result.get("title_guess") or "", rule_result.get("ingredients") or [],
                         rule_result.get("steps") or [])
    return labels


def prepare(ex: Example) -> bool:
    """Fills in lines and labels. False if the example can't be used."""
    if ex.raw_text:
        lines = P.prepare_lines(ex.raw_text)
        labels, cov_ing, cov_step = align(lines, ex.title, ex.ingredients, ex.steps)
        if len(lines) >= 3 and cov_ing >= 0.5 and cov_step >= 0.5 and ex.ingredients and ex.steps:
            ex.lines, ex.labels, ex.imported, ex.weight = lines, labels, True, 1.0
            ex.rule_labels = rule_labels(lines, P.rule_segment_raw_text(ex.raw_text))
            return True
    # No usable original text: the saved recipe as a plain list.
    if not ex.ingredients or not ex.steps:
        return False
    text = "\n".join([ex.title] + ex.ingredients + ex.steps)
    ex.lines = P.prepare_lines(text)
    ex.labels, _, _ = align(ex.lines, ex.title, ex.ingredients, ex.steps)
    ex.rule_labels = rule_labels(ex.lines, P.rule_segment_raw_text(text))
    ex.imported, ex.weight = False, PSEUDO_WEIGHT
    return True


# What OCR makes of a step number drawn in a circle: a symbol or a few
# letters with a bracket, before the capital that starts the step.
_SYMBOL_MARK = re.compile(rf"^\s*(?:[^\w\s]{{1,2}}|[a-z]{{1,3}}\))\s+(?=[{P._UPPER}])")
_DIGITS = re.compile(r"\d{1,2}")


def _real_step_numbers(lines: list[str]) -> bool:
    """Printed step numbers that can be split on: at least two, counting up
    by one, and no circle-marker leftovers among them. A screenshot's "9"
    and "3)" are misread circles, not numbers."""
    if any(_SYMBOL_MARK.match(l) for l in lines):
        return False
    nums = []
    for l in lines:
        m = P._STEP_START.match(l) or P._BARE_NUMBER.match(l)
        if m:
            nums.append(int(_DIGITS.search(m.group(0)).group(0)))
    return len(nums) >= 2 and nums == list(range(nums[0], nums[0] + len(nums)))


@dataclass
class SegmenterModel:
    W: np.ndarray
    log_trans: np.ndarray
    log_start: np.ndarray
    log_prior: np.ndarray

    def label(self, lines: list[str], rules: list[int]) -> tuple[list[int], np.ndarray]:
        X = linear.Sparse(line_features(lines, [LABELS[r] for r in rules]))
        probs = linear.predict_softmax(X, self.W)
        path = linear.viterbi(np.log(probs + 1e-9) - self.log_prior, self.log_trans, self.log_start)
        return path, probs

    def segment(self, raw_text: str, rule_result: dict) -> dict | None:
        lines = P.prepare_lines(raw_text)
        if len(lines) < 2:
            return None
        path, probs = self.label(lines, rule_labels(lines, rule_result))
        ingredients = P._clean_ingredients([_CHECKBOX.sub("", l) for l, y in zip(lines, path) if y == ING])
        step_lines = [l for l, y in zip(lines, path) if y == STEP]
        if _real_step_numbers(step_lines):
            steps = P._clean_steps(step_lines)
        else:
            # No step numbers to go by: split at sentence ends and drop what
            # OCR made of numbers drawn in circles ("@", "©", "faa)", "9").
            steps = P._prose_steps(step_lines)
        # Never worse than nothing: if the model finds no ingredients or no
        # steps where the rules did, keep the rules' result.
        if (not ingredients and rule_result.get("ingredients")) or (not steps and rule_result.get("steps")):
            return None
        titles = [i for i, y in enumerate(path) if y == TITLE]
        title = lines[max(titles, key=lambda i: probs[i, TITLE])] if titles else rule_result.get("title_guess")
        return {"title_guess": title or "Untitled Recipe", "ingredients": ingredients, "steps": steps,
                "servings": rule_result.get("servings")}

    def arrays(self) -> dict:
        return {"W": self.W.astype(np.float32), "log_trans": self.log_trans,
                "log_start": self.log_start, "log_prior": self.log_prior}

    @classmethod
    def from_arrays(cls, a) -> "SegmenterModel":
        return cls(W=a["W"].astype(np.float64), log_trans=a["log_trans"], log_start=a["log_start"],
                   log_prior=a["log_prior"])


def _fit(examples: list[Example]) -> SegmenterModel:
    rows, y, w = [], [], []
    for ex in examples:
        rows += line_features(ex.lines, [LABELS[r] for r in ex.rule_labels])
        y += ex.labels
        w += [ex.weight] * len(ex.lines)
    y = np.asarray(y)
    W = linear.train_softmax(linear.Sparse(rows), y, len(LABELS), weights=w, l2=1.0)
    trans = np.ones((4, 4))
    start = np.ones(4)
    for ex in examples:
        if not ex.imported:
            continue
        start[ex.labels[0]] += 1
        for a, b in zip(ex.labels, ex.labels[1:]):
            trans[a, b] += 1
    prior = np.bincount(y, minlength=4) + 1.0
    return SegmenterModel(W=W, log_trans=np.log(trans / trans.sum(axis=1, keepdims=True)),
                          log_start=np.log(start / start.sum()), log_prior=np.log(prior / prior.sum()))


def content_accuracy(true: list[int], pred: list[int]) -> tuple[int, int]:
    """Right / counted, over lines that either side puts in the recipe.
    Lines both call "other" (page furniture) are left out: every method
    gets those, and they'd hide the differences that matter."""
    counted = right = 0
    for t, p in zip(true, pred):
        if t == OTHER and p == OTHER:
            continue
        counted += 1
        right += t == p
    return right, counted


def train(examples: list[Example], folds: int = 5) -> tuple[SegmenterModel | None, dict]:
    """Cross-validates against the rules, then trains on everything.
    Returns (model or None, report)."""
    usable = [ex for ex in examples if prepare(ex)]
    imported = [ex for ex in usable if ex.imported]
    report = {"imported_recipes": len(imported), "other_recipes": len(usable) - len(imported),
              "needed": MIN_IMPORTED, "lines": sum(len(ex.lines) for ex in imported), "min_gain": MIN_GAIN}
    if len(imported) < MIN_IMPORTED:
        report["status"] = "not_enough_data"
        return None, report
    k = min(folds, len(imported))
    fold_rows = linear.grouped_folds([ex.recipe_id for ex in imported], k)
    ml_r = ml_n = rule_r = rule_n = 0
    for f in range(k):
        held = {imported[i].recipe_id for i in fold_rows[f]}
        model = _fit([ex for ex in usable if ex.recipe_id not in held])
        for ex in imported:
            if ex.recipe_id not in held:
                continue
            path, _ = model.label(ex.lines, ex.rule_labels)
            r, n = content_accuracy(ex.labels, path)
            ml_r, ml_n = ml_r + r, ml_n + n
            r, n = content_accuracy(ex.labels, ex.rule_labels)
            rule_r, rule_n = rule_r + r, rule_n + n
    ml_score = ml_r / ml_n if ml_n else 0.0
    rule_score = rule_r / rule_n if rule_n else 0.0
    report.update(learned_accuracy=round(ml_score, 4), rules_accuracy=round(rule_score, 4))
    if ml_score < rule_score + MIN_GAIN:
        report["status"] = "rules_better"
        return None, report
    report["status"] = "active"
    return _fit(usable), report
