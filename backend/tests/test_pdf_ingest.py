import os
import sys

import pytest

from app.ingestion.pdf_ingest import extract_pdf_text, segment_raw_text, PdfTooLargeError  # noqa: E402


def test_segment_handles_ingredients_before_steps():
    text = (
        "My Recipe\nIngredients\n2 cups flour\n1 tsp salt\n"
        "Instructions\n1. Mix\n2. Bake\n"
    )
    result = segment_raw_text(text)
    assert result["ingredients"] == ["2 cups flour", "1 tsp salt"]
    assert result["steps"] == ["1. Mix", "2. Bake"]


def test_segment_handles_steps_before_ingredients():
    """A prior third-party review claimed this ordering produces empty
    lists. It doesn't -- the ing_start < step_start branch in
    segment_raw_text already handles both orderings. This test locks that
    in so a future refactor can't silently reintroduce the bug that was
    incorrectly reported."""
    text = (
        "My Recipe\nInstructions\n1. Do the first thing\n2. Do the second thing\n"
        "Ingredients\n2 cups flour\n1 tsp salt\n"
    )
    result = segment_raw_text(text)
    assert result["ingredients"] == ["2 cups flour", "1 tsp salt"]
    assert result["steps"] == ["1. Do the first thing", "2. Do the second thing"]


def test_segment_no_headings_falls_back_to_pattern_matching():
    text = "Quick Snack\n2 cups popcorn\n1. Pop the popcorn\n2. Add butter"
    result = segment_raw_text(text)
    assert any("popcorn" in i for i in result["ingredients"])
    assert any("Pop the popcorn" in s for s in result["steps"])


def test_pdf_with_text_layer_skips_ocr(tmp_path):
    from weasyprint import HTML

    html = "<h1>Test</h1><h2>Ingredients</h2><p>2 cups flour</p><h2>Instructions</h2><p>1. Mix</p>"
    pdf_path = tmp_path / "test.pdf"
    HTML(string=html).write_pdf(str(pdf_path))

    result = extract_pdf_text(str(pdf_path))
    assert result.ocr_used_on_pages == []
    assert "flour" in result.raw_text


def test_pdf_page_cap(tmp_path):
    from weasyprint import HTML

    html = "".join(f"<div style='page-break-after: always;'>Page {i}</div>" for i in range(65))
    pdf_path = tmp_path / "big.pdf"
    HTML(string=html).write_pdf(str(pdf_path))

    with pytest.raises(PdfTooLargeError):
        extract_pdf_text(str(pdf_path))
