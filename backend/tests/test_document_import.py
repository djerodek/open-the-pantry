"""Word, text and Markdown files through the Document button (owner,
2026-11-18): the /api/ingest/pdf routes read them with the same rules as a
PDF's text, and say plainly what isn't one of them."""
import io
import os
import uuid
import zipfile

import pytest

from app.ingestion.document_read import markdown_to_text

RECIPE_LINES = ["Ingredients", "1 cup flour", "2 eggs", "1/2 cup milk", "Steps",
                "Mix everything together.", "Cook in a hot pan until golden."]


def _docx(paragraphs, extra=None):
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{w}"><w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def _zip_without_word():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("a.txt", "x")
    return buf.getvalue()


def _post(client, name, data, mime="application/octet-stream"):
    return client.post("/api/ingest/pdf", files={"file": (name, data, mime)})


@pytest.mark.parametrize("name,data", [
    ("r.docx", _docx(["Word pancakes"] + RECIPE_LINES)),
    ("r.txt", "\n".join(["Text pancakes"] + RECIPE_LINES).encode()),
    ("r.md", ("# Markdown pancakes\n\n## Ingredients\n\n- 1 cup flour\n- 2 eggs\n- 1/2 cup **milk**\n\n"
              "## Steps\n\n1. Mix everything together.\n2. Cook in a hot pan until [golden](https://example.com).\n").encode()),
    ("r.txt", "\n".join(["Crêpes de grand-mère", "Ingrédients", "250 ml de farine", "2 œufs", "Préparation",
                         "Mélanger.", "Cuire à la poêle."]).encode("cp1252")),
], ids=["docx", "txt", "md", "txt-windows-1252"])
def test_a_document_gives_a_draft(client, name, data):
    r = _post(client, name, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_type"] == "document" and body["incomplete"] is None, body
    assert body["stored_file"] is None and body["image_path"] is None
    assert body["ingredients"] and body["steps"], body
    assert "**" not in body["raw_text"] and "](" not in body["raw_text"] and "#" not in body["raw_text"]


def test_its_file_isnt_kept(client):
    from app.database import TMP_DIR
    before = set(os.listdir(TMP_DIR))
    assert _post(client, "r.txt", "\n".join(["Kept pancakes"] + RECIPE_LINES).encode()).status_code == 200
    assert set(os.listdir(TMP_DIR)) == before


@pytest.mark.parametrize("name,data,detail", [
    ("x.bin", b"\x00\x01\x02binary", "This file isn't a PDF, Word, text or Markdown file."),
    ("x.zip", _zip_without_word(), "This file isn't a PDF, Word, text or Markdown file."),
    ("x.docx", b"PK\x03\x04 not really a zip", "This file isn't a PDF, Word, text or Markdown file."),
], ids=["binary", "zip-without-word", "broken-zip"])
def test_what_isnt_a_document_is_refused(client, name, data, detail):
    r = _post(client, name, data)
    assert r.status_code == 400 and r.json()["detail"] == detail, r.text
    fr = client.post("/api/ingest/pdf", files={"file": (name, data, "application/octet-stream")},
                     headers={"X-App-Lang": "fr"})
    assert fr.json()["detail"] == "Ce fichier n'est pas un fichier PDF, Word, texte ou Markdown."


def test_a_word_file_with_too_much_text_is_refused(client, monkeypatch):
    from app.ingestion import document_read
    monkeypatch.setattr(document_read, "MAX_DOCX_XML_BYTES", 200)
    r = _post(client, "big.docx", _docx(["Big pancakes"] + RECIPE_LINES * 5))
    assert r.status_code == 400 and r.json()["detail"] == "This Word file's text is too large to read.", r.text


def test_a_batch_takes_documents_and_pdfs_alike(client):
    sfx = uuid.uuid4().hex[:6]
    files = [("files", ("a.docx", _docx([f"Batch word {sfx}"] + RECIPE_LINES), "application/octet-stream")),
             ("files", ("b.txt", "\n".join([f"Batch text {sfx}"] + RECIPE_LINES).encode(), "text/plain"))]
    r = client.post("/api/ingest/pdf/batch", files=files)
    assert r.status_code == 200 and len(r.json()["succeeded"]) == 2, r.text
    ids = [s["recipe_id"] for s in r.json()["succeeded"]]
    assert {client.get(f"/api/recipes/{i}").json()["source_type"] for i in ids} == {"document"}
    client.post("/api/recipes/batch-delete", headers={"X-Requested-With": "XMLHttpRequest"}, json={"ids": ids})


def test_markdown_marks_come_off_the_words_stay():
    md = ("# Title\n> a quote\n---\n```\ncode\n```\n**bold** and *it* and __b__\n"
          "![photo](x.jpg) [link text](https://example.com)\n- 1 cup flour\n1. Mix.")
    assert markdown_to_text(md).split("\n") == ["Title", "a quote", "code", "bold and it and b",
                                               "link text", "- 1 cup flour", "1. Mix."]
