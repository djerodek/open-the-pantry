"""DeepSeek review and security audit of 0143: a failed first sweep stopped
the app starting; a restore interrupted between its renames left uploads/
empty; JSON-LD sections nested very deep skipped the whole JSON-LD tier;
a compressed JSON response would have been rewritten under its old
header; RECIPE_APP_ALLOWED_HOSTS=* turned the host check off silently;
a cPanel username or token with a line break or a colon went into the
Authorization header; a NAT64 address (64:ff9b::7f00:1) passed as public.
The backup sent twice after the password prompt is in tests_browser."""
import os
import socket
from unittest.mock import patch

import pytest

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_a_failed_first_sweep_doesnt_stop_the_app_starting(client, monkeypatch):
    import sqlalchemy.exc
    from fastapi.testclient import TestClient
    from app import main

    async def locked():
        raise sqlalchemy.exc.OperationalError("DELETE", {}, Exception("database is locked"))
    monkeypatch.setattr(main, "_run_sweeps", locked)
    logged = []
    monkeypatch.setattr(main.log, "exception", lambda msg, *a, **k: logged.append(msg))
    with TestClient(main.app) as c:                       # runs the start-up
        assert c.get("/healthz").status_code == 200
    assert any("Start-up sweep failed" in m for m in logged)


def test_an_interrupted_restore_puts_the_photos_back(tmp_path):
    from app.restore import recover_interrupted_swap
    live = tmp_path / "uploads"
    old = tmp_path / "uploads-before-restore-20261101-101010"
    old.mkdir()
    (old / "img-1.jpg").write_bytes(b"x")
    live.mkdir()                                           # recreated empty at import
    assert recover_interrupted_swap(str(live)) == str(old)
    assert (live / "img-1.jpg").exists() and not old.exists()


@pytest.mark.parametrize("setup", ["not empty", "two left", "none left"])
def test_otherwise_nothing_is_moved(tmp_path, setup):
    from app.restore import recover_interrupted_swap
    live = tmp_path / "uploads"
    live.mkdir()
    if setup == "not empty":
        (live / "img-2.jpg").write_bytes(b"y")
    if setup != "none left":
        (tmp_path / "uploads-before-restore-20261101-101010").mkdir()
    if setup == "two left":
        (tmp_path / "uploads-before-restore-20261101-111111").mkdir()
    assert recover_interrupted_swap(str(live)) is None
    assert live.is_dir()


def test_very_deep_sections_dont_lose_the_page():
    from app.ingestion.url_ingest import _flatten_instructions, MAX_INSTRUCTION_DEPTH
    node = {"@type": "HowToStep", "text": "Too deep."}
    for _ in range(5000):
        node = {"@type": "HowToSection", "name": "Part", "itemListElement": [node]}
    assert _flatten_instructions([{"@type": "HowToStep", "text": "Mix."}, node])[0] == "Mix."
    # Ordinary nesting is read whole.
    two = [{"@type": "HowToSection", "name": "Dough", "itemListElement": [
        {"@type": "HowToSection", "name": "Starter", "itemListElement": [{"@type": "HowToStep", "text": "Feed."}]}]}]
    assert _flatten_instructions(two) == ["Dough:", "Starter:", "Feed."]
    assert MAX_INSTRUCTION_DEPTH >= 10


def test_a_compressed_json_response_is_left_alone():
    import asyncio
    from starlette.requests import Request
    from starlette.responses import Response
    from app import main
    body = b"\x1f\x8b compressed"
    resp = Response(content=body, media_type="application/json", headers={"Content-Encoding": "gzip"})

    async def call_next(request):
        return resp
    req = Request({"type": "http", "method": "GET", "path": "/api/x", "headers": [(b"x-app-lang", b"fr")],
                   "query_string": b""})
    out = asyncio.run(main.interface_language(req, call_next))
    assert out is resp


def test_a_wildcard_host_list_is_warned_about():
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "main.py"), encoding="utf-8").read()
    assert 'if "*" in ALLOWED_HOSTS:' in src and "DNS rebinding" in src
    readme = open(os.path.join(os.path.dirname(__file__), "..", "..", "README.md"), encoding="utf-8").read()
    assert "RECIPE_APP_ALLOWED_HOSTS=*" in readme


@pytest.mark.parametrize("user,token,ok", [
    ("marc", "ABCDEF0123456789ABCDEF0123456789", True), ("marc.d", "tok-en_1", True),
    ("marc\n", "TOKEN", False), ("marc", "TOKEN\r\nX-Evil: 1", False), ("ma:rc", "TOKEN", False),
    ("marc", "TO KEN", False), ("", "TOKEN", False),
])
def test_cpanel_values_are_checked_before_the_header(user, token, ok):
    from app import https_setup
    if ok:
        assert https_setup.CpanelDns("https://cp.example.test:2083", user, token).auth == f"cpanel {user}:{token}"
    else:
        with pytest.raises(https_setup.HttpsError):
            https_setup.CpanelDns("https://cp.example.test:2083", user, token)


def test_settings_refuse_a_bad_cpanel_token(client):
    from app import admin_lock
    with patch.object(admin_lock, "is_locked_path", return_value=False):
        r = client.put("/api/https/credentials", headers=H,
                       json={"username": "marc", "base_url": "https://cp.example.test:2083",
                             "domain": "pantry.example.test", "token": "TOKEN\nX"})
    assert r.status_code == 400 and "letters and digits" in r.json()["detail"]


@pytest.mark.parametrize("address,blocked", [("64:ff9b::7f00:1", True), ("64:ff9b::c0a8:101", True),
                                              ("64:ff9b::5db8:d822", False)])
def test_nat64_addresses_are_judged_by_the_ipv4_inside(address, blocked):
    from app.ingestion.url_ingest import validate_public_url, UrlValidationError
    answer = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0))]
    with patch("app.ingestion.url_ingest.socket.getaddrinfo", return_value=answer):
        if blocked:
            with pytest.raises(UrlValidationError):
                validate_public_url("http://nat64.example.com/")
        else:
            assert validate_public_url("http://nat64.example.com/") == "64:ff9b::5db8:d822"


def test_no_page_sets_html_from_data():
    """Audit of 0143: a future innerHTML of recipe text would run (the CSP
    allows <img onerror>). innerHTML is only ever emptied, and el()'s
    trustedStaticHtml only gets the fixed SVG constants."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for f in [root / "frontend/js/app.js", *sorted((root / "frontend-picker").glob("*.js"))]:
        js = f.read_text(encoding="utf-8")
        assert "insertAdjacentHTML" not in js and "outerHTML" not in js and "document.write" not in js, f.name
        for m in re.finditer(r"\.innerHTML\s*=\s*([^;\n]+)", js):
            assert m.group(1).strip() in ('""', "v"), (f.name, m.group(0))
        for m in re.finditer(r"trustedStaticHtml:\s*([^,}\n]+)", js):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*_SVG", m.group(1).strip()), (f.name, m.group(0))
        # The one `= v` is el()'s trustedStaticHtml branch.
        assert len(re.findall(r"\.innerHTML\s*=\s*v\b", js)) <= 1
