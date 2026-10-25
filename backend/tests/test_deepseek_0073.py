"""DeepSeek review of 0073."""
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first even
    when this file runs on its own."""


def test_a_section_label_in_front_of_a_quantity_is_kept():
    from app.ingestion.pdf_ingest import _clean_ingredients
    assert _clean_ingredients(["For the dough: 2 cups flour", "1 tsp salt", "Noodles: 8 oz wheat noodles"]) == \
        ["For the dough:", "2 cups flour", "1 tsp salt", "8 oz wheat noodles"]
    assert _clean_ingredients(["Pour la pâte: 250 g de farine"]) == ["Pour la pâte:", "250 g de farine"]


@pytest.mark.parametrize("value", ["24:00", "30:15", "00:24:00"])
def test_hours_are_under_24_in_both_forms(value):
    from app.time_utils import ddhhmm_to_minutes, ddhhmm_error
    assert ddhhmm_to_minutes(value) is None and ddhhmm_error(value)


def test_long_cook_time_buckets_stay_a_short_row():
    from app.time_utils import available_time_buckets
    b = available_time_buckets([60 * 1440])
    days = [x["minutes"] // 1440 for x in b if x["minutes"] > 1440]
    assert days == [2, 3, 4, 5, 6, 7, 14, 21, 28, 35, 42, 49, 56, 60]
    assert b[-1]["label"] == "60d"
    assert len([x for x in b if x["minutes"] % 60 == 0]) <= 40       # the first row of the filter
    assert [x["minutes"] // 1440 for x in available_time_buckets([8 * 1440 + 380])][-3:] == [6, 7, 9]


def test_translation_keeps_every_set_cookie():
    import asyncio
    from starlette.requests import Request
    from starlette.responses import StreamingResponse
    from app.main import interface_language

    async def call_next(request):      # what Starlette hands a middleware: a streamed body
        body = b'{"detail": "Not found"}'

        async def chunks():
            yield body
        r = StreamingResponse(chunks(), media_type="application/json", headers={"content-length": str(len(body))})
        r.set_cookie("a", "1")
        r.set_cookie("b", "2")
        return r
    scope = {"type": "http", "method": "GET", "path": "/x", "headers": [(b"x-app-lang", b"fr")],
             "query_string": b""}
    out = asyncio.run(interface_language(Request(scope), call_next))
    cookies = [v for k, v in out.raw_headers if k == b"set-cookie"]
    assert len(cookies) == 2
    lengths = [v for k, v in out.raw_headers if k == b"content-length"]
    assert lengths == [str(len(out.body)).encode()]


def test_only_draft_names_can_be_discarded(client):
    from app.database import TMP_DIR
    other = os.path.join(TMP_DIR, "keep-me.txt")
    with open(other, "w") as f:
        f.write("x")
    try:
        r = client.delete("/api/ingest/draft/keep-me.txt")
        assert r.status_code == 200 and os.path.exists(other)
    finally:
        os.remove(other)


def test_a_reading_failure_leaves_no_attachment_behind(tmp_path, monkeypatch):
    from weasyprint import HTML
    from app.ingestion import email_processing
    from tests.test_email_ingest import _make_email

    def boom(text):
        raise RuntimeError("segmenter broke")
    monkeypatch.setattr(email_processing, "segment_raw_text", boom)
    pdf = HTML(string="<h1>X</h1><p>2 cups flour</p>").write_pdf()
    msg = _make_email(body="See attached.", attachment=pdf, attach_type="application/pdf", filename="r.pdf")
    result = email_processing.process_tagged_email(msg, str(tmp_path))
    assert result["success"] is False and "Could not read PDF attachment" in result["error"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field,value", [
    ("title", "x" * 501),
    ("raw_text", "x" * 1_000_001),
    ("ingredients", [{"raw_line": "x" * 2001}]),
    ("steps", ["x" * 20_001]),
    ("steps", ["Mix."] * 501),
], ids=["title-501", "raw_text-1000001", "ingredient-2001", "step-20001", "steps-501"])
# Short ids: pytest -v prints each test's id, and the values themselves
# made one line a megabyte long, which stalled GitHub's live log view.
def test_recipe_fields_are_capped(client, sample_recipe_payload, field, value):
    r = client.post("/api/recipes", json={**sample_recipe_payload, field: value})
    assert r.status_code == 422


def test_busy_state_stays_in_its_own_dialog():
    body = JS[JS.index("function setBusy("):JS.index("function setBusy(") + 500]
    assert "topOverlay() || document" in body and "busyDisabled" in body


def test_startup_survives_a_failed_tag_or_time_load():
    for name in ("async function loadTags()", "async function loadTimeBuckets()"):
        body = JS[JS.index(name):JS.index(name) + 700]
        assert "try {" in body and "catch" in body


def test_rating_and_notes_saves_never_reject_and_update_the_list():
    for name in ("async function patchRating(", "async function patchNotes("):
        body = JS[JS.index(name):JS.index(name) + 500]
        assert "catch {" in body and "syncListEntry(" in body
    close = JS[JS.index("function closeRecipeDetail()"):JS.index("function closeRecipeDetail()") + 400]
    assert "renderRecipeList(currentRecipes)" in close
    assert "if (openOverlay === detailOverlay) { closeRecipeDetail(); return; }" in JS
