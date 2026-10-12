"""Learned tag suggestions.

One logistic regression per tag, over the words of the title, ingredients
and steps (features.recipe_features), trained on the user's own tagged
recipes. It adds to the keyword tagger rather than replacing it: a tag is
suggested when its model is confident and the keywords didn't already
suggest it.

Batch and email imports save suggestions without review, so a tag only
gets a model if, in cross-validation on the user's recipes, its extra
suggestions (the ones the keywords missed) were right at least PRECISION
of the time. Tags that can't show that are left to the keywords.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from . import linear
from .features import recipe_features

MIN_RECIPES = 15     # tagged recipes needed before anything is learned
MIN_POSITIVE = 5     # recipes carrying a tag before it gets a model
MIN_NEGATIVE = 5
PRECISION = 0.85     # required precision of the extra suggestions, cross-validated
MIN_HITS = 3         # ...over at least this many correct extra suggestions
FLOOR = 0.5          # never suggest below this probability
MAX_TAGS = 50        # only the most-used tags get a model: training time and memory stay bounded


@dataclass
class TagExample:
    recipe_id: int
    title: str
    ingredients: list[str]
    step_text: str
    tags: set            # {(name, category)}
    keyword: set         # what the keyword tagger suggests for it: {(name, category)}


@dataclass
class TagModel:
    W: np.ndarray                    # (dim, k)
    tags: list                       # [(name, category, subgroup)]
    thresholds: np.ndarray           # (k,)

    def suggest(self, title: str, ingredients: list[str], step_text: str) -> list[tuple[str, str, str | None]]:
        if not self.tags:
            return []
        X = linear.Sparse([recipe_features(title, ingredients, step_text)])
        p = linear.predict_sigmoid(X, self.W)[0]
        return [tuple(t) for t, prob, th in zip(self.tags, p, self.thresholds) if prob >= th]

    def arrays(self) -> dict:
        return {"W": self.W.astype(np.float32), "thresholds": self.thresholds}

    @classmethod
    def from_arrays(cls, a, tags) -> "TagModel":
        return cls(W=a["W"].astype(np.float32), tags=[tuple(t) for t in tags], thresholds=a["thresholds"])


def _threshold(probs: np.ndarray, truth: np.ndarray) -> float | None:
    """Lowest probability cut whose suggestions are right often enough."""
    order = np.argsort(-probs)
    best = None
    hits = 0
    for rank, i in enumerate(order, 1):
        if probs[i] < FLOOR:
            break
        hits += truth[i]
        if hits >= MIN_HITS and hits / rank >= PRECISION:
            best = float(probs[i])
    return best


def train(examples: list[TagExample], subgroups: dict, folds: int = 5) -> tuple[TagModel | None, dict]:
    """subgroups: {(name, category): subgroup} for the tags that exist."""
    # Every recipe counts, tagged or not: leaving out the untagged ones
    # taught "not pasta, so curry" when only pasta and curry recipes had tags.
    report = {"tagged_recipes": sum(1 for ex in examples if ex.tags), "needed": MIN_RECIPES, "learned": [], "tags_considered": 0}
    if report["tagged_recipes"] < MIN_RECIPES:
        report["status"] = "not_enough_data"
        return None, report
    counts: dict = {}
    for ex in examples:
        for t in ex.tags:
            counts[t] = counts.get(t, 0) + 1
    eligible = [t for t, c in counts.items()
                if c >= MIN_POSITIVE and len(examples) - c >= MIN_NEGATIVE and t in subgroups]
    # The most-used first; a library with hundreds of rarely used tags
    # trains the same MAX_TAGS models (Claude review round 6: 400 tags
    # took 72 s and 2.8 GB).
    candidates = sorted(sorted(eligible, key=lambda t: (-counts[t], t))[:MAX_TAGS])
    report["tags_considered"] = len(candidates)
    report["tags_skipped"] = len(eligible) - len(candidates)
    if not candidates:
        report["status"] = "no_tags"
        return None, report

    X = linear.Sparse([recipe_features(ex.title, ex.ingredients, ex.step_text) for ex in examples])
    Y = np.array([[1.0 if t in ex.tags else 0.0 for t in candidates] for ex in examples])
    KW = np.array([[t in ex.keyword for t in candidates] for ex in examples])

    # One tag at a time: the optimiser's memory is a few MB for one tag,
    # but it grew by that much for every tag trained together.
    oof = np.zeros_like(Y)
    k = min(folds, len(examples))
    for rows in linear.grouped_folds([ex.recipe_id for ex in examples], k, seed=1):
        train_rows = np.setdiff1d(np.arange(len(examples)), rows)
        X_train, X_held = X.take(train_rows), X.take(rows)
        for j in range(len(candidates)):
            w = linear.train_sigmoid(X_train, Y[train_rows][:, [j]])
            oof[rows, j] = linear.predict_sigmoid(X_held, w)[:, 0]

    keep, thresholds = [], []
    for j, t in enumerate(candidates):
        extra = ~KW[:, j]            # only what the keywords didn't already suggest
        th = _threshold(oof[extra, j], Y[extra, j])
        if th is not None:
            keep.append(j)
            thresholds.append(th)
    if not keep:
        report["status"] = "nothing_learned"
        return None, report
    W = np.zeros((X.dim, len(keep)), dtype=np.float32)
    for col, j in enumerate(keep):
        W[:, col] = linear.train_sigmoid(X, Y[:, [j]])[:, 0]
    tags = [(candidates[j][0], candidates[j][1], subgroups.get(candidates[j])) for j in keep]
    report["status"] = "active"
    report["learned"] = sorted(t[0] for t in tags)
    return TagModel(W=W, tags=tags, thresholds=np.asarray(thresholds)), report
