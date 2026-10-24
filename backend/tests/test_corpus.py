"""Import corpus (app/corpus.py): every checked case read again by the
import code, with a baseline, no field more than corpus.TOLERANCE below it.

Runs the self-written cases in tests/corpus/ (CI) and, when
RECIPE_APP_CORPUS_DIR is set, a private corpus too. Accept new scores
with `python -m app.corpus report <corpus> --save-baseline`."""
import io
import json
import os
import zipfile
from pathlib import Path

import pytest

from app import corpus

PUBLIC = Path(__file__).parent / "corpus"
CORPORA = [PUBLIC] + ([Path(os.environ["RECIPE_APP_CORPUS_DIR"])] if os.environ.get("RECIPE_APP_CORPUS_DIR") else [])
CASES = [c for d in CORPORA for c in corpus.cases(d) if corpus.load_meta(c).get("checked")]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c.parent.name}/{c.name}")
def test_no_case_got_worse(case):
    meta = corpus.load_meta(case)
    if corpus.needs_ocr(case, meta) and not corpus.ocr_available():
        pytest.skip("needs Tesseract")
    # Public and private alike: a checked case without a baseline would pass
    # whatever the import gives (GPT review of 0123).
    assert not (found := corpus.problems(corpus.evaluate(case))), "; ".join(found)


def test_scoring():
    s = corpus.score({"title": "Pâte  Brisée", "ingredients": ["1 cup flour", "2 eggs"], "steps": [],
                      "total_time": "35", "servings": None},
                     {"title": "pâte brisée", "ingredients": ["1 cup flour", "2 eggs"], "steps": ["Mix."],
                      "total_time": "PT35M", "servings": None, "cook_time": None},
                     corpus.URL_FIELDS)
    assert s == {"title": 1.0, "ingredients": 1.0, "steps": 0.0, "total_time": 1.0, "servings": 1.0, "cook_time": 1.0}
    # A merged line costs a little, a missed one a lot.
    want = ["Mix the flour and salt.", "Add the water.", "Knead 10 minutes."]
    merged = corpus.field_score("steps", ["Mix the flour and salt. Add the water.", "Knead 10 minutes."], want)
    missed = corpus.field_score("steps", ["Mix the flour and salt.", "Knead 10 minutes."], want)
    assert 0.95 < merged < 1.0 and missed < merged
    assert corpus.field_score("total_time", "1 h 30", "90 minutes") == 1.0
    assert corpus.field_score("total_time", "45 min", "PT1H") == 0.0


def _library(client, made):
    """The test library as a full backup, the way export reads one."""
    return client.get("/api/backup/database.zip").content


def test_export_from_your_library(client, tmp_path):
    from app.database import SessionLocal
    from app import models
    page = (PUBLIC / "p001-en-blog-json-ld" / "input.html").read_text(encoding="utf-8")
    want = json.loads((PUBLIC / "p001-en-blog-json-ld" / "expected.json").read_text(encoding="utf-8"))
    raw = (PUBLIC / "p003-fr-email" / "input.txt").read_text(encoding="utf-8")

    def post(**body):
        r = client.post("/api/recipes", json={"ingredients": [{"raw_line": "1 cup rice"}], "steps": ["Cook."], **body})
        assert r.status_code == 200, r.text
        return r.json()["id"]
    from_pdf = post(title="Pâte brisée", source_type="pdf", raw_text=raw, servings="1 abaisse",
                    ingredients=[{"raw_line": x} for x in ("250 g de farine", "125 g de beurre froid, en dés",
                                                           "1 pincée de sel", "60 ml d'eau glacée")],
                    steps=["Sabler la farine, le sel et le beurre du bout des doigts.",
                           "Ajouter l'eau et former une boule sans trop travailler la pâte.",
                           "Envelopper et réfrigérer 30 minutes avant d'abaisser."])
    from_link = post(title=want["title"], source_type="url", source_url="https://example.com/oat-pancakes",
                     ingredients=[{"raw_line": x} for x in want["ingredients"]], steps=want["steps"],
                     servings=want["servings"], total_time="35")
    typed = post(title="Typed in", source_type="manual")
    unchecked = post(title="Not checked", source_type="pdf", raw_text="x")
    binned = post(title="Binned", source_type="pdf", raw_text="x")
    ids = [from_pdf, from_link, typed, unchecked, binned]
    try:
        with SessionLocal() as db:
            db.get(models.Recipe, unchecked).needs_review = True
            db.commit()
        client.delete(f"/api/recipes/{binned}")
        backup = tmp_path / "backup.zip"
        backup.write_bytes(_library(client, ids))
        fetched = []
        out = tmp_path / "mine"
        res = corpus.export_library(backup, out, ids=ids, fetcher=lambda url: fetched.append(url) or page)
        assert fetched == ["https://example.com/oat-pancakes"]
        assert len(res["added"]) == 2 and [rid for rid, _ in res["skipped"]] == [typed]   # To review, bin: not offered
        pdf_case = out / f"{from_pdf:05d}-pate-brisee"
        meta = corpus.load_meta(pdf_case)
        assert meta["type"] == "text" and meta["language"] == "fr" and meta["checked"] is True
        assert (pdf_case / "input.txt").read_text(encoding="utf-8") == raw
        link_case = next(out.glob(f"{from_link:05d}-*"))
        assert corpus.load_meta(link_case)["url"] == "https://example.com/oat-pancakes"
        # Scored like any case: the rules took the greeting for the title.
        scores = corpus.evaluate(pdf_case)["scores"]
        assert scores["ingredients"] == 1.0 and scores["title"] < 0.5
        assert corpus.evaluate(link_case)["scores"]["total_time"] == 1.0
        # Run again: cases already there are kept (they may have been edited).
        again = corpus.export_library(backup, out, ids=ids, fetch=False)
        assert again["added"] == [] and len(again["kept"]) == 1
        # Baseline, then a drop is reported.
        assert corpus.report(out, save_baseline=True, out=io.StringIO()) == 0
        e = json.loads((pdf_case / "expected.json").read_text(encoding="utf-8"))
        e["ingredients"] = ["something else entirely"]
        (pdf_case / "expected.json").write_text(json.dumps(e), encoding="utf-8")
        text = io.StringIO()
        assert corpus.report(out, out=text) == 1 and "DROPPED" in text.getvalue()
        # A recipes.db works as well as a backup.
        db_file = tmp_path / "recipes.db"
        db_file.write_bytes(zipfile.ZipFile(backup).read("recipes.db"))
        assert len(corpus.export_library(db_file, tmp_path / "other", ids=[from_pdf])["added"]) == 1
    finally:
        for rid in ids:
            client.delete(f"/api/recipes/{rid}")
            client.delete(f"/api/recently-deleted/{rid}")


def test_add_makes_a_draft(tmp_path):
    src = tmp_path / "email.txt"
    src.write_text((PUBLIC / "p003-fr-email" / "input.txt").read_text(encoding="utf-8"), encoding="utf-8")
    case = corpus.add_case(tmp_path / "c", "text", [str(src)], name="Pâte")
    meta = corpus.load_meta(case)
    assert case.name == "a0001-pate" and meta["checked"] is False and meta["language"] == "fr"
    expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
    assert expected["title"] == "Salut!" and len(expected["ingredients"]) == 4    # the import's own result
    out = io.StringIO()
    corpus.report(tmp_path / "c", out=out)
    assert "1 draft case(s) not scored" in out.getvalue()
    assert corpus.add_case(tmp_path / "c", "text", [str(src)]).name.startswith("a0002-")


def test_command_line(capsys):
    assert corpus.main(["report", str(PUBLIC)]) == 0
    assert "Fields right without edits" in capsys.readouterr().out


def test_a_checked_case_needs_a_baseline(tmp_path):
    """GPT review of 0123: in a private corpus, a checked case with no
    baseline passed whatever the import gave."""
    import shutil
    shutil.copytree(PUBLIC / "p003-fr-email", tmp_path / "p003-fr-email")
    r = corpus.evaluate(tmp_path / "p003-fr-email")
    assert corpus.problems(r) == ["no baseline yet: accept its scores with report --save-baseline"]
    out = io.StringIO()
    corpus.report(tmp_path, out=out)
    assert "1 case(s) without a baseline, not yet guarded" in out.getvalue()
    corpus.report(tmp_path, save_baseline=True, out=io.StringIO())
    assert corpus.problems(corpus.evaluate(tmp_path / "p003-fr-email")) == []


def test_export_refuses_an_oversized_database(tmp_path, monkeypatch):
    backup = tmp_path / "b.zip"
    with zipfile.ZipFile(backup, "w") as zf:
        zf.writestr("recipes.db", b"x" * 5000)
    monkeypatch.setattr(corpus, "MAX_DB_BYTES", 1000)
    with pytest.raises(ValueError, match="larger than"):
        corpus.export_library(backup, tmp_path / "c")
