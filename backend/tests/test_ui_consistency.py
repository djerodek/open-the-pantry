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
    # On paper the link can't be followed, so print shows the address.
    css = CSS.read_text(encoding="utf-8")
    print_css = css[css.index("@media print"):]
    assert ".badge-link::after { content: attr(href)" in print_css


def test_search_clear_button_is_wired_up():
    """The X in the search box: present in the page, kept in step with the
    box (including when "Clear filters" empties it from code, which fires no
    input event), and the browser's own clear control hidden so there is one."""
    root = CSS.parents[1]
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "js" / "app.js").read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    assert 'id="search-clear"' in html and 'aria-label="Clear search"' in html
    assert '$("#search-input").addEventListener("input", syncSearchClear)' in js
    clear_all = js[js.index("function clearAllFilters("):js.index("function syncClearFiltersButton(")]
    assert "syncSearchClear()" in clear_all
    assert "::-webkit-search-cancel-button" in css
    # Text the browser restores (back button, Firefox reload) fires no input event.
    assert 'window.addEventListener("pageshow", syncSearchClear)' in js


def test_card_controls_do_not_follow_the_card_link():
    """The favourite and rating buttons sit inside the card's <a href>. The
    row stops the click from reaching the card, so it must also cancel the
    link, or a tap on the heart navigates (on iOS: list redrawn at the top)."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function cardQuickControls("):js.index("function recipeCard(")]
    assert "onclick: (e) => { e.stopPropagation(); e.preventDefault(); }" in body
    row = body[:body.index("return wrap;")]
    assert 'el("a"' not in row and 'el("input"' not in row, "a real link or field in the row would be cancelled too"


def test_card_share_sheet_skips_the_missing_email_button():
    """Without email sending, Email PDF is null in the share actions; the
    card's share sheet appended it anyway, threw, and lost the buttons after it."""
    js = (CSS.parents[1] / "js" / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function openShareSheet("):js.index("function openShareSheet(") + 600]
    assert ".filter(Boolean).forEach((b) => actions.appendChild(b))" in body
