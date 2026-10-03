"""Things kept in two places that have to stay the same (Qwen review)."""
import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[2] / "frontend" / "css" / "styles.css"


def _declarations(block: str) -> dict:
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return dict(re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", block))


@pytest.mark.skipif(not CSS.exists(), reason="frontend not present")
def test_dark_theme_blocks_match():
    css = CSS.read_text(encoding="utf-8")
    media = re.search(r':root:not\(\s*\[\s*data-theme\s*=\s*"light"\s*\]\s*\)\s*\{(.*?)\}', css, re.S)
    forced = re.search(r'html\[\s*data-theme\s*=\s*"dark"\s*\]\s*\{(.*?)\}', css, re.S)
    assert media and forced, "dark-theme blocks not found in styles.css; update this test's selectors"
    media, forced = media.group(1), forced.group(1)
    a, b = _declarations(media), _declarations(forced)
    assert len(a) > 20 and a == b


def test_restore_instructions_give_the_same_commands():
    from app.backup import RESTORE_INSTRUCTIONS, RESTORE_INSTRUCTIONS_FR

    def commands(text):
        # Indented lines are the commands and compose snippets; the archive
        # name's date placeholder differs by language (YYYY vs AAAA).
        lines = [l.strip() for l in text.splitlines() if l.startswith("    ") and l.strip()]
        return [re.sub(r"backup-\S+\.zip", "backup-X.zip", l) for l in lines
                if not l.startswith(("recipes.db", "uploads/", "manifest.json"))]

    en, fr = commands(RESTORE_INSTRUCTIONS), commands(RESTORE_INSTRUCTIONS_FR)
    en = [l.replace("/path/to/new/folder", "PATH") for l in en]
    fr = [l.replace("/chemin/vers/nouveau/dossier", "PATH") for l in fr]
    assert en == fr and any("docker compose down" in l for l in en)


def test_ocr_skips_a_zero_size_render():
    from PIL import Image
    from app.ingestion import pdf_ingest
    assert pdf_ingest._ocr_image(Image.new("L", (0, 10))) == ("", None)
