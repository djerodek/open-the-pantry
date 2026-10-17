"""Security headers on both ports (backlog item 1): Content-Security-Policy,
Referrer-Policy, Permissions-Policy, no framing. No HSTS: it would break
the plain-http addresses the app keeps on purpose."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _csp(r):
    return {d.split()[0]: d.split()[1:] for d in r.headers["content-security-policy"].split("; ")}


def test_main_port_headers(client):
    for path in ("/", "/js/app.js", "/api/recipes", "/healthz", "/api/recipes/0"):
        r = client.get(path)
        csp = _csp(r)
        assert csp["default-src"] == ["'self'"] and csp["script-src"] == ["'self'"], path
        assert csp["style-src"] == ["'self'", "'unsafe-inline'"]
        assert csp["img-src"] == ["'self'", "data:", "blob:"]
        assert csp["object-src"] == ["'none'"] and csp["frame-ancestors"] == ["'none'"]
        assert csp["base-uri"] == ["'self'"] and csp["form-action"] == ["'self'"]
        # Settings checks whether the What to eat? port answers.
        assert csp["connect-src"] == ["'self'", "http://testserver:8091"], path
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        pp = r.headers["permissions-policy"]
        assert "camera=()" in pp and "geolocation=()" in pp and "screen-wake-lock=(self)" in pp
        assert "strict-transport-security" not in r.headers


def test_connect_src_follows_the_picker_address(client, monkeypatch):
    from app import picker
    monkeypatch.setattr(picker, "PICKER_PUBLIC_PORT", 9000)
    assert _csp(client.get("/"))["connect-src"] == ["'self'", "http://testserver:9000"]
    monkeypatch.setattr(picker, "PICKER_PUBLIC_URL", "https://picker.example.com/sub")
    assert _csp(client.get("/"))["connect-src"] == ["'self'", "https://picker.example.com"]


def test_picker_port_headers(client):
    from app.main import picker_app
    pc = TestClient(picker_app)
    for path in ("/", "/api/picker/options", "/healthz", "/missing"):
        r = pc.get(path)
        csp = _csp(r)
        assert csp["connect-src"] == ["'self'"], path
        assert csp["script-src"] == ["'self'"] and csp["frame-ancestors"] == ["'none'"]
        assert r.headers["referrer-policy"] == "no-referrer"
        assert "strict-transport-security" not in r.headers


def test_pages_have_no_inline_script():
    """script-src 'self' blocks inline scripts and handlers: there must be
    none in the pages or the export template."""
    for f in (ROOT / "frontend" / "index.html", ROOT / "frontend-picker" / "index.html",
              ROOT / "backend" / "app" / "templates" / "recipe_export.html"):
        html = f.read_text(encoding="utf-8")
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), f
        assert not re.search(r"\son[a-z]+\s*=", html), f
        assert "javascript:" not in html, f
