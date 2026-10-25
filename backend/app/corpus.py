"""Import corpus: real recipe inputs, each with the result an import should
give, read again by the import code after every change. A drop in a
case's score is a regression: a change somewhere (OCR preparation, a
segmentation rule, a link tier) made an import that used to come out
right come out wrong.

A corpus is a folder of cases:

    <corpus>/
      baseline.json            last accepted scores, per case and field
      00012-chicken-curry/
        meta.json              {"type": "url" | "text" | "pdf" | "image",
                                "url": ..., "language": "en" | "fr",
                                "checked": true, "origin": ..., "note": ...}
        input.html | input.txt | input.pdf | input-1.png, input-2.png…
        expected.json          {"title", "ingredients", "steps", "servings",
                                "prep_time", "cook_time", "total_time"}

Cases come from two places:
  export  your own checked recipes (python -m app.corpus export): each
          recipe's original text and what you saved, the pairs Learning
          trains on (ml/service._examples), or, for a recipe from a link,
          the page fetched again. Your corrections are the expected result.
  add     a file or link (python -m app.corpus add): the import's own
          result is written as a draft, "checked": false, to correct by
          hand; drafts aren't scored until "checked" is true.

What's measured is the import code without Learning (the rules and the
link tiers): Learning differs per library and scores itself (Settings ->
Learning). Text, PDF and photo cases are scored on title, ingredients,
steps and servings, the parts those imports read; link cases on the times
too.

Score per field: 1.0 when it matches once case, spacing and quotes are
evened out (times: the same length of time however written); otherwise the share of characters in common (difflib), lines
joined, so a split or merged line costs a little and a missed one a lot.
A case may not drop more than TOLERANCE below its baseline in any field.

Run in the app's image (docker exec <container> python -m app.corpus
report /app/data/corpus) to get the same Tesseract as production; OCR
output differs between Tesseract versions. Cases hold other people's
recipes: keep a private corpus private (the data folder is fine; it isn't
in backups). backend/tests/corpus/ holds the self-written public cases CI
runs; tests/test_corpus.py also runs RECIPE_APP_CORPUS_DIR when set.
"""
from __future__ import annotations

import argparse
import difflib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path

from .logging_setup import get_logger
from .language import guess_language
from .text_rules import is_steps_only

log = get_logger("corpus")

TOLERANCE = 0.02
MAX_DB_BYTES = 4 * 1024 ** 3      # as restore.MAX_DB_BYTES: a backup's database, unpacked
TEXT_FIELDS = ("title", "ingredients", "steps", "servings")
URL_FIELDS = TEXT_FIELDS + ("prep_time", "cook_time", "total_time")
LIST_FIELDS = ("ingredients", "steps")
TYPES = ("url", "text", "pdf", "image")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")


# ---------------------------------------------------------------- running

def fields_for(meta: dict) -> tuple[str, ...]:
    return URL_FIELDS if meta["type"] == "url" else TEXT_FIELDS


def inputs(case: Path, meta: dict) -> list[Path]:
    t = meta["type"]
    if t == "url":
        return [case / "input.html"]
    if t == "text":
        return [case / "input.txt"]
    if t == "pdf":
        return [case / "input.pdf"]
    return sorted((p for p in case.iterdir() if p.name.startswith("input") and p.suffix.lower() in IMAGE_SUFFIXES),
                  key=lambda p: [int(n) if n.isdigit() else n for n in re.split(r"(\d+)", p.name)])


def needs_ocr(case: Path, meta: dict) -> bool:
    """True for photos, and for PDFs: a PDF page without a text layer is
    read by OCR."""
    return meta["type"] in ("image", "pdf")


def _segmented(raw_text: str) -> dict:
    from .ingestion.pdf_ingest import rule_segment_raw_text
    seg = rule_segment_raw_text(raw_text)
    return {"title": seg.get("title_guess") or "", "ingredients": list(seg.get("ingredients") or []),
            "steps": list(seg.get("steps") or []), "servings": seg.get("servings")}


def run_case(case: Path, meta: dict | None = None) -> dict:
    """What the import code makes of the case's input now, as the review
    screen would show it before any correction. A failed import is a
    result with nothing in it, and "error" saying why."""
    meta = meta or load_meta(case)
    t = meta["type"]
    try:
        if t == "url":
            from .ingestion.url_ingest import parse_html
            r = parse_html((case / "input.html").read_text(encoding="utf-8", errors="replace"), meta.get("url") or "")
            return {"title": r.title or "", "ingredients": list(r.ingredients or []), "steps": list(r.steps or []),
                    "servings": r.servings, "prep_time": r.prep_time, "cook_time": r.cook_time,
                    "total_time": r.total_time}
        if t == "text":
            return _segmented((case / "input.txt").read_text(encoding="utf-8"))
        if t == "pdf":
            from .ingestion.pdf_ingest import extract_pdf_text
            return _segmented(extract_pdf_text(str(case / "input.pdf")).raw_text)
        if t == "image":
            # As Add from photos does: each upload re-encoded, read in
            # order, the texts joined, segmented once. On copies: reading
            # may turn the stored file upright.
            from .file_validation import reencode_image
            from .ingestion.image_ingest import ingest_image
            texts = []
            with tempfile.TemporaryDirectory(prefix="otp-corpus-") as tmp:
                for i, src in enumerate(inputs(case, meta)):
                    dst = os.path.join(tmp, f"{i}{src.suffix.lower()}")
                    shutil.copyfile(src, dst)
                    reencode_image(dst)
                    texts.append(ingest_image(dst).raw_text)
            return _segmented("\n\n".join(texts))
    except Exception as e:      # an import that fails scores nothing; why goes in the report
        log.debug("corpus: %s failed to import", case.name, exc_info=True)
        return {"error": f"{type(e).__name__}: {e}"}
    raise ValueError(f"{case.name}: unknown type {t!r}")


# ---------------------------------------------------------------- scoring

_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', " ": " ", " ": " "})


def norm(s) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFC", str(s)).translate(_QUOTES).casefold()
    return " ".join(s.split())


def _ratio(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()


TIME_FIELDS = ("prep_time", "cook_time", "total_time")


def field_score(field: str, got, want) -> float:
    if field in TIME_FIELDS:
        # The same length of time however it's written: "35", "35 min",
        # "PT35M" (the reading the JSON export uses).
        from .interchange import iso_duration
        g, w = iso_duration(got), iso_duration(want)
        if g and w:
            return 1.0 if g == w else 0.0
    if field in LIST_FIELDS:
        g = [norm(x) for x in (got or []) if norm(x)]
        w = [norm(x) for x in (want or []) if norm(x)]
        return 1.0 if g == w else _ratio("\n".join(g), "\n".join(w))
    return _ratio(norm(got), norm(want))


def score(got: dict, want: dict, fields) -> dict:
    """{field: 0..1} for the fields expected.json has."""
    return {f: round(field_score(f, got.get(f), want.get(f)), 4) for f in fields if f in want}


# ---------------------------------------------------------------- the corpus

def load_meta(case: Path) -> dict:
    meta = json.loads((case / "meta.json").read_text(encoding="utf-8"))
    if meta.get("type") not in TYPES:
        raise ValueError(f"{case.name}: meta.json type must be one of {', '.join(TYPES)}")
    return meta


def cases(corpus: Path) -> list[Path]:
    corpus = Path(corpus)
    if not corpus.is_dir():
        return []
    return sorted(p for p in corpus.iterdir() if p.is_dir() and (p / "meta.json").is_file())


def load_baseline(corpus: Path) -> dict:
    path = Path(corpus) / "baseline.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def evaluate(case: Path) -> dict:
    """One case: its meta, the result now, the scores and any field more
    than TOLERANCE below the baseline."""
    meta = load_meta(case)
    want = json.loads((case / "expected.json").read_text(encoding="utf-8"))
    got = run_case(case, meta)
    scores = score(got, want, fields_for(meta))
    floor = load_baseline(case.parent).get(case.name, {})
    dropped = {f: (floor[f], v) for f, v in scores.items() if f in floor and v < floor[f] - TOLERANCE}
    return {"case": case.name, "meta": meta, "got": got, "want": want, "scores": scores,
            "dropped": dropped, "new": case.name not in load_baseline(case.parent)}


def problems(result: dict) -> list[str]:
    """Why a scored case fails the regression check: a field dropped, or no
    baseline yet (a checked case is only a guard once its scores are
    accepted with report --save-baseline)."""
    if result["new"]:
        return ["no baseline yet: accept its scores with report --save-baseline"]
    return [f"{f} {was:.2f} -> {now:.2f}" for f, (was, now) in result["dropped"].items()]


def ocr_available() -> bool:
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        log.debug("corpus: no Tesseract", exc_info=True)
        return False


# ---------------------------------------------------------------- export (your library)

def slug(title: str) -> str:
    s = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:40] or "recipe"


def _open_library(path: Path, tmp: str) -> sqlite3.Connection:
    """A recipes.db, or a full backup's zip, opened read-only."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as zf:
            if "recipes.db" not in zf.namelist():
                raise ValueError(f"{path}: not a full backup (no recipes.db inside)")
            if zf.getinfo("recipes.db").file_size > MAX_DB_BYTES:
                raise ValueError(f"{path}: its recipes.db is larger than {MAX_DB_BYTES // 1024 ** 3} GB")
            # Copied with a ceiling as well: the size a zip declares is
            # what zipfile checks against, but nothing here trusts it alone.
            copied = 0
            with zf.open("recipes.db") as src, open(os.path.join(tmp, "recipes.db"), "wb") as dst:
                while chunk := src.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_DB_BYTES:
                        raise ValueError(f"{path}: its recipes.db is larger than {MAX_DB_BYTES // 1024 ** 3} GB")
                    dst.write(chunk)
        path = Path(tmp) / "recipes.db"
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def library_examples(con: sqlite3.Connection, ids=None):
    """The checked recipes Learning trains on (ml/service._examples: not
    To review), still in the library, as (recipe, ingredient lines, steps)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(recipes)")}
    where = ["1 = 1"]
    if "needs_review" in cols:
        where.append("COALESCE(needs_review, 0) = 0")
    if "deleted_at" in cols:
        where.append("deleted_at IS NULL")
    rows = con.execute(
        "SELECT id, title, source_type, source_url, raw_text, servings, prep_time, cook_time, total_time "
        f"FROM recipes WHERE {' AND '.join(where)} ORDER BY id").fetchall()
    wanted = set(ids) if ids else None
    for rid, title, stype, url, raw, servings, prep, cook, total in rows:
        if wanted is not None and rid not in wanted:
            continue
        ings = [r[0] for r in con.execute(
            "SELECT COALESCE(NULLIF(raw_line, ''), name) FROM ingredients WHERE recipe_id = ? ORDER BY position", (rid,))
            if r[0] and r[0].strip()]
        steps = [r[0] for r in con.execute(
            "SELECT text FROM steps WHERE recipe_id = ? ORDER BY position", (rid,)) if r[0] and r[0].strip()]
        yield ({"id": rid, "title": title or "", "source_type": stype, "source_url": url, "raw_text": raw,
                "servings": servings, "prep_time": prep, "cook_time": cook, "total_time": total}, ings, steps)


def case_kind(recipe: dict) -> str | None:
    """url: from a link, including a link sent by email (its raw text is
    the page's instructions, not what the import read). text: a PDF,
    photo or emailed recipe, re-segmented from the text the import read.
    None: typed in, imported from a file, or no original text kept."""
    if recipe["source_url"] and recipe["source_type"] in ("url", "email"):
        return "url"
    if recipe["source_type"] in ("pdf", "screenshot", "email") and (recipe["raw_text"] or "").strip():
        return "text"
    return None


def _write_case(case: Path, meta: dict, expected: dict, files: dict[str, bytes]):
    case.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (case / name).write_bytes(data)
    (case / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (case / "expected.json").write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def export_library(library: Path, corpus: Path, ids=None, fetch=True, overwrite=False, fetcher=None) -> dict:
    """Your checked recipes as cases, your saved version as the expected
    result. Returns {"added": [...], "kept": [...], "skipped": [(id, why)]}.
    A case already there is kept (you may have edited it) unless
    overwrite."""
    corpus = Path(corpus)
    corpus.mkdir(parents=True, exist_ok=True)
    out = {"added": [], "kept": [], "skipped": []}
    with tempfile.TemporaryDirectory(prefix="otp-corpus-") as tmp:
        con = _open_library(library, tmp)
        try:
            for recipe, ings, steps in list(library_examples(con, ids)):
                kind = case_kind(recipe)
                if kind == "text" and is_steps_only(recipe["raw_text"], steps):
                    out["skipped"].append((recipe["id"], "its original text is only its steps (an emailed link "
                                                         "saved before the app kept links)"))
                    continue
                if kind is None:
                    out["skipped"].append((recipe["id"], f"{recipe['source_type']}: no original input kept"))
                    continue
                if kind == "url" and not fetch:
                    out["skipped"].append((recipe["id"], "from a link (fetching turned off)"))
                    continue
                case = corpus / f"{recipe['id']:05d}-{slug(recipe['title'])}"
                if case.exists() and not overwrite:
                    out["kept"].append(case.name)
                    continue
                expected = {"title": recipe["title"], "ingredients": ings, "steps": steps,
                            "servings": recipe["servings"]}
                meta = {"type": kind, "language": guess_language(" ".join([recipe["title"], *ings, *steps])),
                        "checked": True, "origin": f"library recipe {recipe['id']} ({recipe['source_type']})",
                        "note": "Expected = the recipe as you saved it. Edit expected.json where you changed "
                                "more than the import's mistakes (a reworded step, a renamed title)."}
                if kind == "url":
                    expected.update(prep_time=recipe["prep_time"], cook_time=recipe["cook_time"],
                                    total_time=recipe["total_time"])
                    meta["url"] = recipe["source_url"]
                    try:
                        html = (fetcher or _fetch)(recipe["source_url"])
                    except Exception as e:      # a page gone or refusing: reported, the rest go on
                        log.debug("corpus: %s not fetched", recipe["source_url"], exc_info=True)
                        out["skipped"].append((recipe["id"], f"page not fetched: {type(e).__name__}: {e}"))
                        continue
                    meta["note"] += " The page was fetched again now; it may have changed since you saved it."
                    files = {"input.html": html.encode("utf-8")}
                else:
                    files = {"input.txt": recipe["raw_text"].encode("utf-8")}
                _write_case(case, meta, expected, files)
                out["added"].append(case.name)
        finally:
            con.close()
    return out


def _fetch(url: str) -> str:
    from .ingestion.url_ingest import safe_get, validate_public_url
    validate_public_url(url)
    return safe_get(url).text


# ---------------------------------------------------------------- add (a file or link, as a draft)

def add_case(corpus: Path, kind: str, sources: list[str], url: str | None = None, language: str | None = None,
             name: str | None = None, fetcher=None) -> Path:
    """A new case from files or a link, with the import's own result as a
    draft expected.json ("checked": false) to correct by hand."""
    corpus = Path(corpus)
    corpus.mkdir(parents=True, exist_ok=True)
    files = {}
    if kind == "url":
        files["input.html"] = (fetcher or _fetch)(url).encode("utf-8")
    elif kind == "image":
        for i, src in enumerate(sources, 1):
            files[f"input-{i}{Path(src).suffix.lower()}"] = Path(src).read_bytes()
    else:
        files["input.txt" if kind == "text" else "input.pdf"] = Path(sources[0]).read_bytes()
    number = 1 + max([int(m.group(1)) for c in cases(corpus) if (m := re.match(r"a(\d+)-", c.name))] or [0])
    case = corpus / f"a{number:04d}-{slug(name or (url or Path(sources[0]).stem))}"
    meta = {"type": kind, "checked": False, "origin": url or ", ".join(Path(s).name for s in sources),
            "note": "Draft: expected.json is what the import made of it. Correct it, then set checked to true."}
    if url:
        meta["url"] = url
    _write_case(case, meta, {}, files)
    got = run_case(case, meta)
    expected = {f: got.get(f) for f in fields_for(meta)}
    meta["language"] = language or guess_language(" ".join([expected["title"] or "", *expected["ingredients"],
                                                            *expected["steps"]]))
    if "error" in got:
        meta["note"] += f" The import failed: {got['error']}"
    _write_case(case, meta, expected, {})
    return case


# ---------------------------------------------------------------- report

def report(corpus: Path, save_baseline=False, show=None, out=None) -> int:
    """Prints each case's scores, the means by type and language, and any
    drop below the baseline. Returns the number of cases that dropped."""
    out = out or sys.stdout
    corpus = Path(corpus)
    all_cases = cases(corpus)
    drafts = [c for c in all_cases if not load_meta(c).get("checked")]
    ocr = ocr_available()
    results, skipped = [], []
    for c in all_cases:
        if c in drafts:
            continue
        if needs_ocr(c, load_meta(c)) and not ocr:
            skipped.append(c.name)
            continue
        results.append(evaluate(c))

    w = max([len(r["case"]) for r in results] + [4])
    cols = URL_FIELDS
    print(f"{'case':<{w}}  type   lang  " + "  ".join(f"{f[:11]:>11}" for f in cols), file=out)
    for r in results:
        cells = "  ".join(f"{r['scores'][f]:>11.2f}" if f in r["scores"] else f"{'':>11}" for f in cols)
        flag = " DROPPED " + ", ".join(r["dropped"]) if r["dropped"] else (" (new)" if r["new"] else "")
        err = f"  [{r['got']['error']}]" if "error" in r["got"] else ""
        print(f"{r['case']:<{w}}  {r['meta']['type']:<5}  {r['meta'].get('language', '?'):<4}  {cells}{flag}{err}",
              file=out)

    groups: dict[str, list[dict]] = {}
    for r in results:
        groups.setdefault(f"{r['meta']['type']}/{r['meta'].get('language', '?')}", []).append(r)
    print("", file=out)
    for key, rs in sorted(groups.items()):
        means = {f: [r["scores"][f] for r in rs if f in r["scores"]] for f in cols}
        line = "  ".join(f"{f}: {sum(v) / len(v):.2f}" for f, v in means.items() if v)
        print(f"{key} ({len(rs)}): {line}", file=out)
    checked = [v for r in results for v in r["scores"].values()]
    exact = sum(1 for v in checked if v == 1.0)
    whole = sum(1 for r in results if r["scores"] and all(v == 1.0 for v in r["scores"].values()))
    if checked:
        print(f"Fields right without edits: {exact} of {len(checked)} ({100 * exact / len(checked):.0f}%). "
              f"Cases right without edits: {whole} of {len(results)}.", file=out)
    new = [r["case"] for r in results if r["new"]]
    if new and not save_baseline:
        print(f"{len(new)} case(s) without a baseline, not yet guarded: {', '.join(new)}. "
              "Check them, then accept with --save-baseline.", file=out)
    if drafts:
        print(f"{len(drafts)} draft case(s) not scored until checked: {', '.join(c.name for c in drafts)}", file=out)
    if skipped:
        print(f"{len(skipped)} case(s) need Tesseract, not found here: {', '.join(skipped)}", file=out)
    dropped = [r for r in results if r["dropped"]]
    for r in dropped:
        for f, (was, now) in r["dropped"].items():
            print(f"DROPPED {r['case']} {f}: {was:.2f} -> {now:.2f}", file=out)

    for r in results:
        if show and r["case"] in show:
            print(f"\n== {r['case']}", file=out)
            for f in fields_for(r["meta"]):
                if f not in r["want"] or r["scores"].get(f) == 1.0:
                    continue
                def lines(v):
                    return v if isinstance(v, list) else ([] if v is None else [str(v)])
                want, got = lines(r["want"].get(f)), lines(r["got"].get(f))
                print(f"-- {f}", file=out)
                for line in difflib.unified_diff(want, got, "expected", "import", lineterm="", n=1):
                    print(line, file=out)

    if save_baseline:
        base = load_baseline(corpus)
        base.update({r["case"]: r["scores"] for r in results})
        names = {c.name for c in all_cases}
        base = {k: v for k, v in sorted(base.items()) if k in names}
        (corpus / "baseline.json").write_text(json.dumps(base, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Baseline saved: {len(results)} case(s).", file=out)
    return len(dropped)


# ---------------------------------------------------------------- command line

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.corpus", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="your checked recipes as cases")
    e.add_argument("library", help="recipes.db, or a full backup .zip")
    e.add_argument("corpus")
    e.add_argument("--ids", help="only these recipe ids, e.g. 4,12,30")
    e.add_argument("--no-fetch", action="store_true", help="leave out recipes from a link")
    e.add_argument("--overwrite", action="store_true", help="replace cases already there")
    a = sub.add_parser("add", help="a file or link as a draft case")
    a.add_argument("corpus")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--url")
    g.add_argument("--pdf")
    g.add_argument("--text")
    g.add_argument("--image", nargs="+", help="one or more photos, in reading order")
    a.add_argument("--language", choices=("en", "fr"))
    a.add_argument("--name", help="a short name for the folder")
    r = sub.add_parser("report", help="score every checked case")
    r.add_argument("corpus")
    r.add_argument("--save-baseline", action="store_true", help="accept the scores now as the baseline")
    r.add_argument("--show", nargs="+", metavar="CASE", help="what differs, line by line")
    args = p.parse_args(argv)
    otp_log = logging.getLogger("otp")
    level = otp_log.level
    otp_log.setLevel(logging.ERROR)    # the report says why an import failed
    try:
        return _run(args)
    finally:
        otp_log.setLevel(level)


def _run(args) -> int:

    if args.cmd == "export":
        ids = [int(x) for x in args.ids.split(",")] if args.ids else None
        res = export_library(Path(args.library), Path(args.corpus), ids, fetch=not args.no_fetch,
                             overwrite=args.overwrite)
        print(f"{len(res['added'])} case(s) added, {len(res['kept'])} already there (kept), "
              f"{len(res['skipped'])} recipe(s) left out.")
        for rid, why in res["skipped"]:
            print(f"  recipe {rid}: {why}")
        return 0
    if args.cmd == "add":
        kind, sources = next((k, v) for k, v in (("url", None), ("pdf", args.pdf), ("text", args.text),
                                                  ("image", args.image)) if (v or (k == "url" and args.url)))
        sources = [sources] if isinstance(sources, str) else (sources or [])
        case = add_case(Path(args.corpus), kind, sources, url=args.url, language=args.language, name=args.name)
        print(f"Draft case {case}: correct expected.json, then set \"checked\": true in meta.json.")
        return 0
    return 1 if report(Path(args.corpus), save_baseline=args.save_baseline, show=args.show) else 0


if __name__ == "__main__":
    sys.exit(main())
