"""Claude security audit of 0140. (1) A backslash in a link made urlparse
and requests disagree on the host ("http://127.0.0.1:8099\\@example.com/"
was checked as example.com and fetched from 127.0.0.1); (2) the name was
looked up again to connect, so a name answering differently the second
time (DNS rebinding) reached the LAN. Now the host is the one requests
uses and the connection goes to the address that was checked."""
import http.server
import socket
import threading
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8099\\@example.com/?action=x", "http://example.com\\@127.0.0.1/",
    "http://example.com/\\..\\x", "http://exa\tmple.com/", "http://example.com/a\nb", "http://example.com/\x00",
])
def test_a_backslash_or_control_character_is_refused(url):
    from app.ingestion.url_ingest import validate_public_url, UrlValidationError
    with patch("app.ingestion.url_ingest.socket.getaddrinfo", return_value=PUBLIC):
        with pytest.raises(UrlValidationError):
            validate_public_url(url)


def test_the_checked_address_is_returned():
    from app.ingestion.url_ingest import validate_public_url
    with patch("app.ingestion.url_ingest.socket.getaddrinfo", return_value=PUBLIC):
        assert validate_public_url("https://www.example.com/recipe?x=1") == "93.184.216.34"


def test_safe_get_connects_to_the_checked_address():
    from app.ingestion import url_ingest
    seen = {}

    def fake_get(url, **kwargs):
        seen.update(kwargs)
        raise url_ingest.requests.exceptions.ConnectionError("stop here")
    with patch.object(url_ingest.socket, "getaddrinfo", return_value=PUBLIC), \
         patch.object(url_ingest, "http_get", side_effect=fake_get):
        with pytest.raises(url_ingest.SiteUnreachableError):
            url_ingest.safe_get("http://rebind.example.com/recipe")
    assert seen["pin"] == "93.184.216.34"


@pytest.fixture
def local_server():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Host")))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1], seen
    httpd.shutdown()


def test_a_pinned_request_doesnt_look_the_name_up_again(local_server):
    """The name below doesn't resolve at all: the request still reaches the
    pinned address, with the name in the Host header."""
    from app import tls
    port, seen = local_server
    with patch("socket.getaddrinfo", wraps=socket.getaddrinfo) as lookups:
        r = tls.get(f"http://never-resolves.invalid:{port}/x?y=1", pin="127.0.0.1", timeout=5)
    assert r.text == "ok" and seen == [("/x?y=1", f"never-resolves.invalid:{port}")]
    assert not [c for c in lookups.call_args_list if c.args and c.args[0] == "never-resolves.invalid"]


def test_a_pinned_session_refuses_another_host(local_server):
    import requests
    from app import tls
    port, _ = local_server
    adapter = tls.PinnedAdapter("one.example", "127.0.0.1")
    with requests.Session() as s:
        s.trust_env = False
        s.mount("http://", adapter)
        with pytest.raises(requests.exceptions.InvalidURL):
            s.get(f"http://two.example:{port}/", timeout=5)


def test_the_original_bypass_reaches_nothing(local_server):
    """The audit's reproduction, end to end through safe_get."""
    from app.ingestion import url_ingest
    port, seen = local_server
    real = socket.getaddrinfo
    # example.com answers with a public address; everything else as usual
    # (socket is one module: a blanket patch would move 127.0.0.1 too).
    lookup = lambda host, *a, **k: PUBLIC if host == "example.com" else real(host, *a, **k)  # noqa: E731
    with patch.object(url_ingest.socket, "getaddrinfo", side_effect=lookup):
        with pytest.raises(url_ingest.UrlValidationError):
            url_ingest.safe_get(f"http://127.0.0.1:{port}\\@example.com/?action=x")
    assert seen == []


def test_pypdf_has_the_loop_and_memory_fixes():
    """(3) 7 advisories in 6.21.2 (crafted PDFs: infinite loops, memory)."""
    import pypdf
    from pathlib import Path
    assert tuple(int(x) for x in pypdf.__version__.split(".")[:3]) >= (6, 21, 4)
    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    assert "\npypdf==6.21.4\n" in req


def test_every_tesseract_run_has_a_time_limit():
    """(5) A crafted image held a heavy-job slot with no limit."""
    import ast
    from pathlib import Path
    from app.ingestion.pdf_ingest import OCR_TIMEOUT_SECONDS
    assert 0 < OCR_TIMEOUT_SECONDS <= 300
    calls = 0
    for f in (Path(__file__).resolve().parents[1] / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr.startswith("image_to_")):
                calls += 1
                assert any(k.arg == "timeout" for k in node.keywords), (f.name, node.lineno)
    assert calls == 5


def test_a_line_break_in_a_logged_name_stays_on_one_line(tmp_path):
    """(7) An attachment's filename with a line break added a fake entry."""
    import logging
    from app.logging_setup import OneLineFormatter
    handler_file = tmp_path / "app.log"
    h = logging.FileHandler(handler_file, encoding="utf-8")
    h.setFormatter(OneLineFormatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    log = logging.getLogger("otp.test-oneline")
    log.addHandler(h)
    log.propagate = False
    try:
        log.warning("Parts: %s", "recipe.pdf\n2026-10-31 12:00:00 INFO    otp.admin: Settings password created\rx")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("with a traceback")
    finally:
        log.removeHandler(h)
        h.close()
    lines = handler_file.read_text(encoding="utf-8").split("\n")
    assert "Settings password created" in lines[0] and "\\n2026-10-31" in lines[0] and "\\rx" in lines[0]
    assert not lines[1].startswith("2026-10-31 12:00:00")
    assert any(line.startswith("Traceback") for line in lines)          # still on its own lines


def test_the_app_logs_with_the_one_line_formatter():
    import logging
    from app.logging_setup import OneLineFormatter
    assert all(isinstance(h.formatter, OneLineFormatter) for h in logging.getLogger("otp").handlers)


def test_uploads_are_never_sniffed(client):
    """(10) nosniff on every response, /uploads included."""
    assert client.get("/healthz").headers["X-Content-Type-Options"] == "nosniff"


def test_a_pdf_draft_cant_be_a_recipe_photo(client):
    """(10) A PDF draft's name was accepted as image_path and served from
    /uploads."""
    from pydantic import ValidationError
    from app import schemas
    name = "pdf-" + "a" * 32 + ".pdf"
    with pytest.raises(ValidationError):
        schemas.RecipeCreate(title="x", source_type="manual", image_path=name)
    assert schemas.RecipeCreate(title="x", source_type="manual", image_path="img-" + "a" * 32 + ".jpg").image_path


def test_the_pdf_export_fetches_only_data_urls(monkeypatch):
    """(10) WeasyPrint's default fetcher reads file:// and http(s)."""
    from app import export
    seen = {}

    class FakeHTML:
        def __init__(self, string=None, url_fetcher=None, **kw):
            seen["fetcher"] = url_fetcher

        def write_pdf(self):
            return b"%PDF"
    monkeypatch.setattr(export, "HTML", FakeHTML)
    monkeypatch.setattr(export, "render_recipe_html", lambda *a, **k: "<p>x</p>")
    export.render_recipe_pdf(object())
    f = seen["fetcher"]
    with pytest.raises(Exception):
        f.fetch("file:///etc/passwd")
    assert f.fetch("data:text/plain,hi") is not None
