"""Word, text and Markdown attachments in a recipe email (owner,
2026-11-18): read by document_read, as the Document button reads them,
after a PDF and before a photo, a link or the body."""
import os

import pytest

from app.ingestion.email_processing import process_tagged_email

from .test_document_import import RECIPE_LINES, _docx
from .test_email_ingest import _make_email

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize("data,mime,name", [
    (_docx(["Mailed word pancakes"] + RECIPE_LINES), DOCX, "pancakes.docx"),
    (_docx(["Mailed word pancakes"] + RECIPE_LINES), "application/octet-stream", "pancakes.docx"),
    ("\n".join(["Mailed text pancakes"] + RECIPE_LINES).encode(), "text/plain", "pancakes.txt"),
    (("# Mailed markdown pancakes\n\n## Ingredients\n- 1 cup flour\n- 2 eggs\n\n"
      "## Steps\n1. Mix everything together.\n2. Cook in a hot pan.\n").encode(), "text/markdown", "pancakes.md"),
    ("\n".join(["Mailed unnamed pancakes"] + RECIPE_LINES).encode(), "text/plain", None),   # attachment, no name
], ids=["docx", "docx-untyped", "txt", "md", "txt-unnamed"])
def test_a_document_attachment_gives_the_recipe(tmp_path, data, mime, name):
    msg = _make_email(body="Here you go.", attachment=data, attach_type=mime, filename=name)
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is True, result
    assert result["source_detail"] == "document attachment"
    assert result["title"].startswith("Mailed") and "pancakes" in result["title"]
    assert [i["raw_line"] for i in result["ingredients"]][:2] == ["1 cup flour", "2 eggs"]
    assert "#" not in result["raw_text"]
    assert os.listdir(tmp_path) == []                     # nothing left behind


def test_a_document_that_isnt_one_is_tried_and_named(tmp_path):
    msg = _make_email(body="See attached.", attachment=b"\x00\x01binary", attach_type="application/octet-stream",
                      filename="notes.docx")
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is False
    assert "notes.docx: not a Word, text or Markdown file" in result["error"], result
    assert os.listdir(tmp_path) == []


def test_a_document_refused_by_its_reader_says_why(tmp_path):
    msg = _make_email(body="See attached.", attachment=b"<!DOCTYPE html><html><body>x</body></html>",
                      attach_type="text/plain", filename="page.txt")
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is False
    assert "page.txt: This is a web page or an SVG image, not text" in result["error"], result


def test_a_pdf_comes_before_a_document(tmp_path):
    """Both attached: the PDF is tried first, as before."""
    from email.message import EmailMessage
    from weasyprint import HTML
    pdf = HTML(string="<h1>PDF pancakes</h1><h2>Ingredients</h2><p>2 cups flour</p><p>1 egg</p>"
                      "<h2>Instructions</h2><p>1. Mix</p><p>2. Bake</p>").write_pdf()
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = "[RECIPE] two", "sender@example.com", "me@example.com"
    msg.set_content("Two files.")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename="a.pdf")
    msg.add_attachment("\n".join(["Text pancakes"] + RECIPE_LINES).encode(), maintype="text", subtype="plain",
                       filename="b.txt")
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is True and result["source_detail"] == "PDF attachment"


def test_the_french_reply_names_the_document():
    from app import i18n
    assert i18n.translate("No recipe found. Tried: no PDF, document, photo or link in the email; body: empty.", "fr") \
        .startswith("Aucune recette trouvée. Essais : aucun PDF, document, photo ni lien dans le courriel;")
    assert i18n.translate("No recipe found. Tried: notes.docx: not a Word, text or Markdown file.", "fr") == \
        "Aucune recette trouvée. Essais : notes.docx : pas un fichier Word, texte ou Markdown."
    assert i18n.translate('Saved "Pancakes" (from "Recipe", via document attachment)', "fr").endswith(
        "(de « Recipe », par le document joint)")


def test_a_document_over_the_upload_limit_is_named(tmp_path, monkeypatch):
    """Review of 0218: the email path's own size check, with the limit."""
    from app import i18n
    from app.ingestion import email_processing
    monkeypatch.setattr(email_processing, "MAX_UPLOAD_BYTES", 2 * 1024 * 1024)
    msg = _make_email(body="See attached.", attachment=b"a" * (2 * 1024 * 1024 + 1), attach_type="text/plain",
                      filename="huge.txt")
    result = process_tagged_email(msg, str(tmp_path))
    assert result["success"] is False
    assert "huge.txt: over the 2 MB limit" in result["error"], result
    assert "huge.txt : plus de 2 Mo" in i18n.translate(result["error"], "fr")
    assert os.listdir(tmp_path) == []


def test_the_attachments_passed_over_are_named_in_french():
    """Claude review of 0228: these reasons reach the result email and
    Check email, not only the log."""
    from app import i18n
    english = ("No recipe found. Tried: a.bmp: not a supported image (JPEG, PNG, GIF or WebP); "
               "b.jpg: 40x30, too small to be a recipe photo; c.zip: application/zip isn't a PDF, Word, "
               "text or Markdown file, or an image; d.jpg: a larger photo was used instead.")
    assert i18n.translate(english, "fr") == (
        "Aucune recette trouvée. Essais : a.bmp : pas une image prise en charge (JPEG, PNG, GIF ou WebP); "
        "b.jpg : 40x30, trop petite pour être une photo de recette; c.zip : application/zip n'est ni un "
        "fichier PDF, Word, texte ou Markdown, ni une image; d.jpg : une photo plus grande a été utilisée "
        "à la place.")
