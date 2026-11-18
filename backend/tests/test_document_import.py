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


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def _docx_body(body):
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W_NS}" xmlns:mc="{MC_NS}">'
           f'<w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def _p(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_a_word_table_row_is_one_line_and_a_text_box_is_read_once(tmp_path):
    """Review of 0216: each cell was a line ("2 cups", "flour"), and a text
    box came out four times (Word's two copies, each read twice)."""
    from app.ingestion.document_read import extract_document_text
    row = lambda *cells: "<w:tr>" + "".join(f"<w:tc>{_p(c)}</w:tc>" for c in cells) + "</w:tr>"  # noqa: E731
    box = f"<w:txbxContent>{_p('Serves 4')}</w:txbxContent>"
    body = (_p("Table pancakes") + "<w:tbl>" + row("2 cups", "flour") + row("2", "eggs") + "</w:tbl>"
            + f'<w:p><w:r><mc:AlternateContent><mc:Choice Requires="wps">{box}</mc:Choice>'
            + f"<mc:Fallback>{box}</mc:Fallback></mc:AlternateContent></w:r></w:p>"
            + _p("Mix."))
    path = tmp_path / "t.docx"
    path.write_bytes(_docx_body(body))
    assert extract_document_text(str(path), "docx").split("\n") == [
        "Table pancakes", "2 cups flour", "2 eggs", "", "Serves 4", "Mix."]


@pytest.mark.parametrize("data,detail", [
    (b"a" * 70_000 + b"\x00tail", "This file isn't text that can be read."),        # NUL past the first 64 KB
    (b"<!DOCTYPE html><html><body>Pancakes</body></html>",
     "This is a web page or an SVG image, not text: save the page as a PDF instead."),
    (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>',
     "This is a web page or an SVG image, not text: save the page as a PDF instead."),
], ids=["nul-late", "html", "svg"])
def test_text_that_isnt_a_recipe_text_is_refused(client, data, detail):
    r = _post(client, "x.txt", data)
    assert r.status_code == 400 and r.json()["detail"] == detail, r.text


def test_a_text_file_over_the_limit_is_refused(client, monkeypatch):
    from app.ingestion import document_read
    monkeypatch.setattr(document_read, "MAX_TEXT_BYTES", 100)
    r = _post(client, "big.txt", "\n".join(["Big pancakes"] + RECIPE_LINES * 3).encode())
    assert r.status_code == 400 and r.json()["detail"] == "This text file is too large to be a recipe (over 1 MB).", r.text
    fr = client.post("/api/ingest/pdf", files={"file": ("big.txt", b"x" * 200, "text/plain")}, headers={"X-App-Lang": "fr"})
    assert fr.json()["detail"] == "Ce fichier texte est trop volumineux pour être une recette (plus de 1 Mo)."


def test_a_long_markdown_line_is_quick():
    """Review of 0216: 23 KB of " *a" took 2.1 s, four times longer each
    time the line doubled. Past 2,000 characters it's left as written."""
    import time
    line = " *a" * 8_000
    t = time.monotonic()
    assert markdown_to_text("Title\n" + line).split("\n")[1] == line.strip()
    assert time.monotonic() - t < 0.5


def test_a_document_draft_saves(client):
    """DeepSeek review of 0214: the single draft through Save, as the review
    screen sends it: source "document", no photo, no stored file."""
    draft = _post(client, "r.txt", "\n".join([f"Saved pancakes {uuid.uuid4().hex[:6]}"] + RECIPE_LINES).encode()).json()
    body = {k: draft[k] for k in ("title", "source_type", "raw_text", "image_path", "stored_file")}
    body.update(ingredients=[{"raw_line": i["raw_line"]} for i in draft["ingredients"]],
                steps=[s["text"] for s in draft["steps"]], tags=[])
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["source_type"] == "document" and saved["image_path"] is None and saved["ingredients"]
    client.post("/api/recipes/batch-delete", headers={"X-Requested-With": "XMLHttpRequest"}, json={"ids": [saved["id"]]})
