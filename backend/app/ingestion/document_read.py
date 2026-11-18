"""Text from a Word (.docx), plain-text (.txt) or Markdown (.md) file, for
the same rules as a PDF's text (segment_raw_text, the learned segmenter,
then the review screen). Owner, 2026-11-18: one "Document" button for
PDFs and these.

A .docx is a zip; only word/document.xml is read, with the same kind of
size limits as the archives elsewhere, and its XML through ElementTree,
which doesn't fetch external entities (and Python's expat refuses entity
expansion bombs). A text file is anything that decodes as UTF-8 (or, failing
that, Windows-1252, what Word's "Plain text" often writes) without NUL
bytes; .txt and .md can't be told apart by their bytes, and Markdown's
marks are taken off either way.
"""
import re
import zipfile
import xml.etree.ElementTree as ET

from ..logging_setup import get_logger

log = get_logger("document")

DOCX_MAGIC = b"PK\x03\x04"
DOCX_BODY = "word/document.xml"
MAX_DOCX_ENTRIES = 2000
MAX_DOCX_XML_BYTES = 20 * 1024 * 1024
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class DocumentError(ValueError):
    """A file that isn't a readable document; the message is for the person."""


def document_kind(path: str, first: bytes) -> str | None:
    """"docx" or "text" for a file that is one, from its bytes, never its
    name; None otherwise."""
    if first.startswith(DOCX_MAGIC):
        try:
            with zipfile.ZipFile(path) as z:
                return "docx" if DOCX_BODY in z.namelist() else None
        except (zipfile.BadZipFile, OSError):
            log.info("document_kind: a zip that can't be opened", exc_info=True)
            return None
    with open(path, "rb") as f:
        head = f.read(64 * 1024)
    if b"\x00" in head:
        return None
    return "text" if _decode(head, partial=True) is not None else None


def _decode(data: bytes, partial: bool = False) -> str | None:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        # A sample cut in the middle of a character ends in an incomplete one.
        if partial and e.start >= len(data) - 3:
            return data[:e.start].decode("utf-8-sig", errors="strict")
        log.debug("_decode: not UTF-8", exc_info=True)
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        log.debug("_decode: not Windows-1252 either", exc_info=True)
        return None


def extract_document_text(path: str, kind: str) -> str:
    if kind == "docx":
        return _docx_text(path)
    with open(path, "rb") as f:
        text = _decode(f.read())
    if text is None:
        raise DocumentError("This file isn't text that can be read.")
    return markdown_to_text(text)


def _docx_text(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            infos = z.infolist()
            if len(infos) > MAX_DOCX_ENTRIES:
                raise DocumentError("This Word file has too many parts to read.")
            body = z.getinfo(DOCX_BODY)
            if body.file_size > MAX_DOCX_XML_BYTES:
                raise DocumentError("This Word file's text is too large to read.")
            with z.open(body) as f:
                xml = f.read(MAX_DOCX_XML_BYTES + 1)    # the size above can lie
            if len(xml) > MAX_DOCX_XML_BYTES:
                raise DocumentError("This Word file's text is too large to read.")
        root = ET.fromstring(xml)
    except DocumentError:
        raise
    except (zipfile.BadZipFile, KeyError, ET.ParseError, OSError, RuntimeError) as e:
        log.info("_docx_text: unreadable Word file", exc_info=True)
        raise DocumentError("This Word file can't be read.") from e
    lines = []
    for p in root.iter(f"{_W}p"):
        parts = []
        for node in p.iter():
            if node.tag == f"{_W}t" and node.text:
                parts.append(node.text)
            elif node.tag == f"{_W}tab":
                parts.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines).strip()


_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_MD_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1|(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)")
_MD_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?")
_MD_FENCE = re.compile(r"^\s{0,3}(```|~~~)")


def markdown_to_text(text: str) -> str:
    """Markdown's marks off, the words kept: headings, bold and italics,
    links (their text), images, rules, quotes and code fences. List markers
    stay: the ingredient rules read "- 1 cup flour" already. Plain text
    passes through unchanged but for those marks."""
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if _MD_FENCE.match(line) or _MD_RULE.match(line):
            continue
        line = _MD_QUOTE.sub("", line)
        m = _MD_HEADING.match(line)
        if m:
            line = m.group(1)
        line = _MD_IMAGE.sub("", line)
        line = _MD_LINK.sub(r"\1", line)
        line = _MD_EMPHASIS.sub(lambda mm: mm.group(2) or mm.group(3), line)
        out.append(line.strip())
    return "\n".join(out).strip()
