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


# ---------------------------------------------------------------------------
# Keep screen awake and optional HTTPS
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not (ROOT / "docker-compose.https.yml").exists(), reason="repo files not present")
def test_https_setup_is_off_by_default_and_names_no_real_domain():
    """The HTTPS override takes every site-specific value from .env and
    refuses to start without them; the repo carries only placeholders."""
    yml = (ROOT / "docker-compose.https.yml").read_text(encoding="utf-8")
    example = (ROOT / "https.env.example").read_text(encoding="utf-8")
    for var in ("PANTRY_DOMAIN", "ACME_EMAIL", "ACME_DNS_PROVIDER"):
        assert re.search(r"\$\{" + var + r":\?", yml), var
    # Any domain-looking name must be a documentation placeholder.
    names = set(re.findall(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|ca|io|dev|app|fr|uk|de)\b", yml + example))
    allowed = {"example.com", "pantry.example.com", "cpanel.example.com", "ns1.example.com", "go-acme.github.io"}
    assert names <= allowed, names - allowed
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and "letsencrypt/" in ignored
    # Plain `docker compose up -d` doesn't include it.
    assert "traefik" not in (ROOT / "docker-compose.yml").read_text(encoding="utf-8").lower()


def test_source_link_only_for_web_addresses():
    """The "View original" link is built from stored data, which a restored
    backup can fill with anything; only http(s) may become a link."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function sourceLink("):js.index("function confidenceBadge(")]
    assert 'url.protocol !== "http:" && url.protocol !== "https:"' in body
    assert 'rel: "noopener noreferrer"' in body and 'target: "_blank"' in body
    assert "sourceLink(recipe)" in js
