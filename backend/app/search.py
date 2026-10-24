"""Recipe search (0117): typo tolerance, words to leave out, best-match
ranking and the excerpt that shows where a recipe matched.

The query is split into words. A word starting with "-" (e.g. "-nuts")
leaves out recipes containing it; every other word must match, from its
start ("chick" finds chicken), in the title, original text, ingredients,
steps, notes or tags.

Typo tolerance: a word that nothing in the library starts with is
matched to the closest words that are in it ("chiken" -> chicken,
"tomatoe" -> tomato), taken from the full-text index's own vocabulary
(the fts5vocab table recipes_fts_vocab). Words under four letters aren't
corrected; four to seven letters allow one edit (a letter added, missing,
changed, or two swapped), eight and more allow two. The page is told what
was corrected, to show "Showing results for ...".

Every word given to FTS5 is quoted, so nothing typed is read as query
syntax; only the * and OR/NOT here are ours."""
from __future__ import annotations

import bisect
import re
import threading
import time
import unicodedata

from sqlalchemy import text
from sqlalchemy.orm import Session

from .logging_setup import get_logger

log = get_logger("search")

TEXT_COLUMNS = "{title raw_text content_text notes}"
TAG_COLUMNS = "{tags_text}"
# bm25 weights, in the table's column order: title, raw_text, tags_text,
# content_text (ingredients and steps), notes. A word in the title counts
# most; the original text least (it repeats the rest, with page noise).
BM25_WEIGHTS = "10.0, 1.0, 5.0, 3.0, 2.0"
MARK_START, MARK_END = "\u0002", "\u0003"   # around matched words in an excerpt
MAX_WORDS = 12
MAX_CANDIDATES = 3
VOCAB_TTL_SECONDS = 30

_vocab_lock = threading.Lock()


class Vocabulary:
    """The index's words, arranged for quick lookups: sorted (does any word
    start with this?), by length (whole-word typos only compare words of
    about the same length) and by first letter (a misspelt start of a
    longer word is only looked for among words starting the same way)."""

    def __init__(self, terms: dict[str, int]):
        self.docs = terms
        self.sorted = sorted(terms)
        self.by_length: dict[int, list[str]] = {}
        self.by_first: dict[str, list[str]] = {}
        for t in terms:
            if len(t) >= 3:
                self.by_length.setdefault(len(t), []).append(t)
                self.by_first.setdefault(t[0], []).append(t)

    def has_prefix(self, w: str) -> bool:
        i = bisect.bisect_left(self.sorted, w)
        return i < len(self.sorted) and self.sorted[i].startswith(w)

    def commonest_with_prefix(self, w: str) -> str:
        """The word starting with w that most recipes contain (for saying
        what was searched: "berry", not the stem "berr")."""
        i, best = bisect.bisect_left(self.sorted, w), None
        while i < len(self.sorted) and self.sorted[i].startswith(w):
            t = self.sorted[i]
            if best is None or self.docs[t] > self.docs[best]:
                best = t
            i += 1
        return best or w


_vocab: dict = {"at": 0.0, "index": Vocabulary({})}


def normalize(word: str) -> str:
    """What FTS5's default tokenizer makes of a word: lower case, accents
    removed."""
    decomposed = unicodedata.normalize("NFKD", word)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def _quote(term: str) -> str:
    return '"' + term.replace('"', '""') + '"'


def parse(q: str) -> tuple[list[str], list[str]]:
    """(words to find, words to leave out), as typed."""
    include, exclude = [], []
    for w in [w for w in re.split(r"\s+", q.strip()) if w][:MAX_WORDS]:
        if w.startswith("-") and len(w) > 1:
            exclude.append(w[1:])
        elif w != "-":
            include.append(w)
    return include, exclude


def _vocabulary(db: Session) -> Vocabulary:
    """The index's words and how many recipes contain each. Cached for a
    few seconds: one search after another needn't reread it."""
    with _vocab_lock:
        if time.monotonic() - _vocab["at"] < VOCAB_TTL_SECONDS:
            return _vocab["index"]
    try:
        rows = db.execute(text("SELECT term, doc FROM recipes_fts_vocab")).fetchall()
        terms = {t: d for t, d in rows}
    except Exception:
        log.warning("Search: couldn't read the index vocabulary; no typo tolerance", exc_info=True)
        terms = {}
    index = Vocabulary(terms)
    with _vocab_lock:
        _vocab.update(at=time.monotonic(), index=index)
    return index


def forget_vocabulary():
    with _vocab_lock:
        _vocab["at"] = 0.0


def edit_distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (a swap of two neighbouring letters
    counts as one edit), giving up past limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        best = cur[0]
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
            best = min(best, cur[j])
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def stem(word: str) -> str:
    """A word's plural or final-vowel ending taken off, for matching from
    the start: "berries" and "berry" both become "berr", "tomatoes"
    "tomato", "pommes" "pomm". Never shorter than four letters."""
    for ending in ("ies", "es", "s", "y", "e", "x"):
        if word.endswith(ending) and len(word) - len(ending) >= 4:
            return word[: -len(ending)]
    return word


def allowed_edits(word: str) -> int:
    n = len(word)
    return 0 if n < 4 else 1 if n < 8 else 2


def corrections(word: str, vocab) -> list[str]:
    """Close spellings of a word nothing in the library starts with; [] if
    something does, or nothing is close. vocab: a Vocabulary (or a plain
    {term: docs} dict)."""
    if not isinstance(vocab, Vocabulary):
        vocab = Vocabulary(vocab)
    w = normalize(word)
    if not re.fullmatch(r"\w+", w) or allowed_edits(w) == 0:
        return []
    if vocab.has_prefix(w):
        return []
    limit = allowed_edits(w)
    best: dict[str, int] = {}
    for n in range(len(w) - limit, len(w) + limit + 1):
        for term in vocab.by_length.get(n, ()):
            d = edit_distance(w, term, limit)
            if d <= limit:
                best[term] = d
    # A misspelt start of a longer word ("chikc" for chicken): the same
    # number of letters from the term's start, one edit allowed.
    for term in vocab.by_first.get(w[0], ()):
        if len(term) > len(w) and term not in best and edit_distance(w, term[:len(w)], 1) <= 1:
            best[term] = limit + 1      # after whole-word matches
    scored = sorted((d, -vocab.docs[t], t) for t, d in best.items())
    return [t for _, _, t in scored[:MAX_CANDIDATES]]


def build(q: str, db: Session) -> dict:
    """The FTS5 expressions for a query, and what was corrected."""
    include, exclude = parse(q)
    vocab = _vocabulary(db) if include else Vocabulary({})
    parts, corrected = [], {}
    for w in include:
        n = normalize(w)
        if not re.fullmatch(r"\w+", n) or vocab.has_prefix(n):
            parts.append(_quote(w) + "*")
            continue
        # Nothing starts with it: its stem ("berries" for berry), then close
        # spellings, each matched from its stem (so "quorkleberry" also
        # finds "quorkleberries").
        alternatives = ([stem(n)] if stem(n) != n and vocab.has_prefix(stem(n)) else []) + corrections(w, vocab)
        if alternatives:
            corrected[w] = list(dict.fromkeys(vocab.commonest_with_prefix(a) for a in alternatives))
        terms = dict.fromkeys([n] + [stem(a) for a in alternatives])
        parts.append("(" + " OR ".join(_quote(t) + "*" for t in terms) + ")" if len(terms) > 1 else _quote(w) + "*")
    leave_out = []
    for w in exclude:
        n = normalize(w)
        # The word and its plural forms, whole: "-egg" mustn't drop eggplant.
        forms = dict.fromkeys([w, n, n + "s", n + "es", n + "x"])
        leave_out.append(" OR ".join(_quote(f) for f in forms))
    return {
        "include": " ".join(parts),
        "exclude": " OR ".join(f"({x})" for x in leave_out),
        "corrected": corrected,
    }


def run(db: Session, q: str) -> dict:
    """Runs a query against the index. Returns:
    matched: {recipe id: {"text", "tag"}} for the words to find (None when
             there are only words to leave out: every recipe, then);
    excluded: ids to leave out;
    rank: {id: bm25 score, lower is better};
    snippets: {id: excerpt with MARK_START/END around matches} where the
              match isn't in the title;
    corrected: {word as typed: [words used instead]}."""
    built = build(q, db)
    out = {"matched": None, "excluded": set(), "rank": {}, "snippets": {}, "corrected": built["corrected"]}
    if built["include"]:
        matched: dict[int, set[str]] = {}
        rows = db.execute(text(
            f"SELECT rowid, bm25(recipes_fts, {BM25_WEIGHTS}), "
            f"highlight(recipes_fts, 0, :a, :b), "
            f"snippet(recipes_fts, 3, :a, :b, '…', 10), snippet(recipes_fts, 4, :a, :b, '…', 10), "
            f"snippet(recipes_fts, 1, :a, :b, '…', 10) "
            f"FROM recipes_fts WHERE recipes_fts MATCH :m"),
            {"m": f"{TEXT_COLUMNS} : ({built['include']})", "a": MARK_START, "b": MARK_END}).fetchall()
        for rowid, score, title, *excerpts in rows:
            matched.setdefault(rowid, set()).add("text")
            out["rank"][rowid] = score
            if MARK_START not in (title or ""):
                found = next((e for e in excerpts if e and MARK_START in e), None)
                if found:
                    out["snippets"][rowid] = found
        for rowid, score in db.execute(text(
                f"SELECT rowid, bm25(recipes_fts, {BM25_WEIGHTS}) FROM recipes_fts WHERE recipes_fts MATCH :m"),
                {"m": f"{TAG_COLUMNS} : ({built['include']})"}).fetchall():
            matched.setdefault(rowid, set()).add("tag")
            out["rank"][rowid] = min(score, out["rank"].get(rowid, score))
        out["matched"] = matched
    if built["exclude"]:
        out["excluded"] = {r[0] for r in db.execute(
            text("SELECT rowid FROM recipes_fts WHERE recipes_fts MATCH :m"),
            {"m": f"{{title raw_text content_text notes tags_text}} : ({built['exclude']})"}).fetchall()}
    return out
