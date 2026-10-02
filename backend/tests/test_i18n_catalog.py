"""The French catalog in frontend/js/i18n.js covers every English string
the interface shows, and each translation keeps the key's placeholders.

A missing entry isn't an error at run time (the English shows instead),
which is exactly why it needs a test: nothing else would notice.
"""
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
pytestmark = pytest.mark.skipif(not (FRONTEND / "js" / "i18n.js").exists(), reason="frontend not present")

_STR = r'"((?:\\.|[^"\\])*)"'


def _unescape(s):
    return json.loads(f'"{s}"')


def _catalog():
    src = (FRONTEND / "js" / "i18n.js").read_text(encoding="utf-8")
    block = src[src.index("const FR = {"):src.index("\n  };", src.index("const FR = {"))]
    pairs = re.findall(_STR + r":\s*" + _STR + r"(?:\s*\+\s*" + _STR + r")?", block)
    return {_unescape(k): _unescape(v) for k, v, _ in pairs}


def _js_keys():
    src = (FRONTEND / "js" / "app.js").read_text(encoding="utf-8")
    keys = [_unescape(m) for m in re.findall(r"\btx\(" + _STR, src)]
    for one, many in re.findall(r"\btxn\([^,]+,\s*" + _STR + r",\s*" + _STR, src):
        keys += [_unescape(one), _unescape(many)]
    # emailField(label, input, hint) translates its own literals.
    for label, hint in re.findall(r"emailField\(" + _STR + r",\s*\w+(?:,\s*" + _STR + r")?", src):
        keys.append(_unescape(label))
        if hint:
            keys.append(_unescape(hint))
    return keys


class _Text(HTMLParser):
    SKIP = {"code", "script", "style"}

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.found = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.depth += 1
        for name, value in attrs:
            if name in ("placeholder", "aria-label", "title", "alt") and value and value.strip():
                self.found.append(value.strip())

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.depth -= 1

    def handle_data(self, data):
        text = " ".join(data.split())
        if text and not self.depth and re.search(r"[A-Za-z]", text):
            self.found.append(text)


def _html_keys():
    p = _Text()
    p.feed((FRONTEND / "index.html").read_text(encoding="utf-8"))
    # The app's name and the text-size buttons' labels are the same in
    # both languages.
    return [k for k in p.found if k not in {"Open the Pantry", "A+", "A-", "A\u2212"}]


def test_every_interface_string_has_a_french_entry():
    catalog = _catalog()
    assert len(catalog) > 300
    missing = sorted({k for k in _js_keys() + _html_keys() if k not in catalog})
    assert not missing, f"no French for: {missing}"


def test_translations_keep_their_placeholders():
    for key, value in _catalog().items():
        assert sorted(re.findall(r"\{\w+\}", key)) == sorted(re.findall(r"\{\w+\}", value)), key


def test_language_buttons_are_not_translated():
    """Each language's own name stays as written in that language."""
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert 'data-lang-choice="en" lang="en">English<' in html
    assert 'data-lang-choice="fr" lang="fr">Français<' in html
