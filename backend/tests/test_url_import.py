"""Add from URL: what is read from a page, the download budget, and URLs kept out of the log.

Address checks (private ranges, pinned connections) are in test_security.py."""
import io
import json
import socket
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .test_restore import H

ROOT = Path(__file__).resolve().parents[2]


def _page(recipe):
    return f'<script type="application/ld+json">{json.dumps(recipe)}</script>'


class _TricklingRaw:
    """Mimics a server that sends one byte at a time: read1 returns
    whatever is available right now rather than waiting to fill amt, and
    each read costs real time -- the delay iter_content's full-chunk wait
    couldn't be interrupted out of."""

    def __init__(self, data: bytes, delay: float = 0.0):
        self._buf = io.BytesIO(data)
        self._delay = delay

    def read1(self, amt=None, decode_content=None):
        import time as _time
        if self._delay:
            _time.sleep(self._delay)
        return self._buf.read(1)

    def close(self):
        pass


def _trickling_response(data: bytes, delay: float = 0.0):
    import requests
    from requests.structures import CaseInsensitiveDict

    resp = requests.Response()
    resp.status_code = 200
    resp.headers = CaseInsensitiveDict({"Content-Type": "text/html; charset=utf-8"})
    resp.raw = _TricklingRaw(data, delay=delay)
    resp.encoding = "utf-8"
    return resp


def _streaming_response(chunks, content_length=None):
    """A real requests.Response over an in-memory body, as stream=True gives."""
    import io

    import requests
    from requests.structures import CaseInsensitiveDict
    resp = requests.Response()
    resp.status_code = 200
    resp.headers = CaseInsensitiveDict({"Content-Type": "text/html; charset=utf-8"})
    if content_length:
        resp.headers["Content-Length"] = str(content_length)
    resp.raw = io.BytesIO(b"".join(chunks))
    resp.encoding = "utf-8"
    return resp


def test_prep_and_cook_times_from_recipe_scrapers():
    from app.ingestion.url_ingest import _try_recipe_scrapers
    html = (ROOT / "backend/tests/corpus/p001-en-blog-json-ld/input.html").read_text(encoding="utf-8")
    r = _try_recipe_scrapers("https://www.example.com/pancakes", html)
    assert (r.prep_time, r.cook_time, r.total_time) == ("15", "20", "35")


def test_a_missing_time_is_none_not_the_word():
    from app.ingestion.url_ingest import _try_recipe_scrapers
    html = ('<script type="application/ld+json">{"@type": "Recipe", "name": "Toast", "recipeIngredient": '
            '["1 slice bread"], "recipeInstructions": "Toast it."}</script>')
    r = _try_recipe_scrapers("https://www.example.com/toast", html)
    assert r is None or (r.prep_time, r.cook_time, r.total_time) == (None, None, None)


@pytest.mark.parametrize("recipe,title,ingredients,steps", [
    ({"name": ["Soup", "Soupe"], "recipeIngredient": ["1 cup rice"], "recipeInstructions": "Cook."},
     "Soup", ["1 cup rice"], ["Cook."]),
    ({"name": "S", "recipeIngredient": ["", "   ", 3, None, {"a": 1}, ["x"], "1 <b>cup</b> rice &amp; beans"],
      "recipeInstructions": "Cook."},
     "S", ["1 cup rice & beans"], ["Cook."]),
    ({"name": "<i>Soupe</i> d&#39;&eacute;t&eacute;", "recipeIngredient": ["1&nbsp;egg"],
      "recipeInstructions": [{"@type": "HowToSection", "name": ["Pâte"], "itemListElement": [{"text": "<p>Boil.</p><p>Drain.</p>"}]}]},
     "Soupe d'été", ["1 egg"], ["Pâte:", "Boil. Drain."]),
    ({"name": 5, "recipeIngredient": "1 egg", "recipeInstructions": "Cook.\nServe.", "image": [5], "recipeYield": {"a": 1},
      "prepTime": ["PT5M"], "cookTime": 10},
     "5", ["1 egg"], ["Cook.", "Serve."]),
    ({"name": None, "recipeIngredient": {"not": "a list"}, "recipeInstructions": [{"text": 7}, {"name": "Stir."}]},
     "Untitled Recipe", [], ["Stir."]),
])
def test_the_fallback_reads_text_only(recipe, title, ingredients, steps):
    from app.ingestion.ingredient_parser import parse_ingredient_block
    from app.ingestion.url_ingest import _try_json_ld
    got = _try_json_ld(_page({"@type": "Recipe", **recipe}))
    assert (got.title, got.ingredients, got.steps) == (title, ingredients, steps)
    assert all(isinstance(x, str) for x in [got.title, *got.ingredients, *got.steps])
    for field in (got.servings, got.prep_time, got.cook_time, got.total_time, got.image_url):
        assert field is None or isinstance(field, str)
    parse_ingredient_block(got.ingredients)          # used to raise on "" or 3


def test_image_servings_and_times_shapes():
    from app.ingestion.url_ingest import _try_json_ld
    base = {"@type": "Recipe", "name": "R", "recipeIngredient": ["1 egg"], "recipeInstructions": "Cook."}
    for image, want in (([{"url": "https://example.com/a.jpg"}], "https://example.com/a.jpg"), ([None], None),
                        ({"url": 3}, None), ("  ", None), ("https://example.com/b.jpg", "https://example.com/b.jpg")):
        assert _try_json_ld(_page({**base, "image": image})).image_url == want, image
    got = _try_json_ld(_page({**base, "recipeYield": [4, "4 servings"], "prepTime": "PT10M"}))
    assert (got.servings, got.prep_time) == ("4", "PT10M")


def test_add_from_url_with_a_list_as_the_name(client, monkeypatch):
    """The whole path, recipe-scrapers or not: no 500."""
    from app.ingestion import url_ingest
    html = _page({"@type": "Recipe", "name": ["Lentil soup"], "recipeIngredient": ["1 cup lentils", ""],
                  "recipeInstructions": [{"@type": "HowToStep", "text": "Simmer <b>30</b> minutes."}]})
    got = url_ingest.parse_html(html, "https://example.com/soup")
    assert got.title and isinstance(got.title, str)
    assert all(isinstance(x, str) and x for x in got.ingredients)


@pytest.mark.parametrize("text,secret,kept", [
    ("No recipe found. Tried: link https://example.com/a?token=SECRET1: no recipe", "SECRET1", "https://example.com/a?…"),
    ("HTTPSConnectionPool(host='example.com', port=443): Max retries exceeded with url: /b?token=SECRET2", "SECRET2",
     "with url: /b?…"),
    ("Soup (from \"x\", via URL in body (https://u:pw@example.com/c?sig=SECRET3))", "SECRET3", "https://example.com/c?…"),
])
def test_redact_urls(text, secret, kept):
    """Separate assertions: `a and b or c` let the second case pass on "/b?…"
    alone (GPT review of 0160)."""
    from app.ingestion.url_ingest import redact_urls
    out = redact_urls(text)
    assert secret not in out
    assert "pw@" not in out
    assert kept in out, out


def test_no_recipe_log_has_no_query(monkeypatch):
    import logging

    from app.ingestion import url_ingest
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    url_ingest.log.addHandler(handler)
    try:
        with pytest.raises(url_ingest.NoRecipeFoundError):
            url_ingest.parse_html("<html><title>x</title><p>nothing</p></html>", "https://example.com/a?token=SECRET1")
    finally:
        url_ingest.log.removeHandler(handler)
    assert "SECRET1" not in buf.getvalue() and "example.com/a" in buf.getvalue()


def test_a_batch_link_recipe_can_be_saved_again(client, monkeypatch):
    from app import schemas
    from app.ingestion.url_ingest import UrlIngestResult
    from app.routes import ingest
    long = UrlIngestResult(title="T" * 600, ingredients=["1 cup flour"] + ["x" * 2500], steps=["s" * 24_000, "Bake."],
                           prep_time="p" * 300, raw_text="1 cup flour\nBake.")
    monkeypatch.setattr(ingest, "ingest_url", lambda url: long)
    monkeypatch.setattr(ingest, "require_complete", lambda r: r)
    r = client.post("/api/ingest/url/batch", headers=H, json={"urls": ["https://example.com/long"]}).json()
    rid = r["succeeded"][0]["recipe_id"]
    try:
        got = client.get(f"/api/recipes/{rid}").json()
        schemas.RecipeCreate(title=got["title"], source_type="url", prep_time=got["prep_time"],
                             ingredients=[{"raw_line": i["raw_line"]} for i in got["ingredients"]],
                             steps=[s["text"] if isinstance(s, dict) else s for s in got["steps"]])
        assert len(got["title"]) == 500
    finally:
        client.delete(f"/api/recipes/{rid}", headers=H)
        client.delete(f"/api/recently-deleted/{rid}", headers=H)


@pytest.mark.parametrize("url,host", [("https://café.example/recette", "xn--caf-dma.example"),
                                      ("https://例え.jp/", "xn--r8jz45g.jp"),
                                      ("https://straße.example/", "xn--strae-oqa.example")])
def test_unicode_host_names(monkeypatch, url, host):
    import socket

    from app.ingestion import url_ingest
    looked_up = []
    monkeypatch.setattr(url_ingest.socket, "getaddrinfo",
                        lambda h, p: looked_up.append(h) or [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))])
    assert url_ingest.validate_public_url(url) == "93.184.216.34"
    assert looked_up == [host]


def test_a_relative_photo_is_resolved_against_the_page(monkeypatch):
    from app.ingestion import url_ingest
    page = ('<script type="application/ld+json">' + json.dumps({
        "@type": "Recipe", "name": "Soup", "image": "/img/soup.jpg", "recipeIngredient": ["1 egg"],
        "recipeInstructions": "Boil."}) + "</script>")
    resp = type("R", (), {"text": page, "status_code": 200, "url": "https://example.com/recipes/soup"})()
    monkeypatch.setattr(url_ingest, "validate_public_url", lambda url, deadline=None: "93.184.216.34")
    monkeypatch.setattr(url_ingest, "safe_get", lambda url, deadline=None: resp)
    assert url_ingest.ingest_url("https://example.com/recipes/soup").image_url == "https://example.com/img/soup.jpg"


def test_a_recipe_under_main_entity_is_found():
    from app.ingestion.url_ingest import _try_json_ld
    recipe = {"@type": "Recipe", "name": "Lemon Rice", "recipeIngredient": ["1 cup rice"],
              "recipeInstructions": "Cook the rice."}
    for page in ({"@type": "WebPage", "mainEntity": recipe},
                 {"@type": "WebPage", "mainEntity": [{"@type": "Person"}, recipe]},
                 {"@graph": [{"@type": "WebPage", "mainEntityOfPage": recipe}]},
                 {"@type": "WebPage", "mainEntity": None}):
        html = f'<script type="application/ld+json">{json.dumps(page)}</script>'
        found = _try_json_ld(html)
        if page.get("mainEntity", 1) is None:
            assert found is None
        else:
            assert found and found.title == "Lemon Rice", page


def test_very_deep_sections_dont_lose_the_page():
    from app.ingestion.url_ingest import MAX_INSTRUCTION_DEPTH, _flatten_instructions
    node = {"@type": "HowToStep", "text": "Too deep."}
    for _ in range(5000):
        node = {"@type": "HowToSection", "name": "Part", "itemListElement": [node]}
    assert _flatten_instructions([{"@type": "HowToStep", "text": "Mix."}, node])[0] == "Mix."
    # Ordinary nesting is read whole.
    two = [{"@type": "HowToSection", "name": "Dough", "itemListElement": [
        {"@type": "HowToSection", "name": "Starter", "itemListElement": [{"@type": "HowToStep", "text": "Feed."}]}]}]
    assert _flatten_instructions(two) == ["Dough:", "Starter:", "Feed."]
    assert MAX_INSTRUCTION_DEPTH >= 10


def test_a_page_without_a_yield_has_no_servings():
    from unittest.mock import patch

    from app.ingestion import url_ingest

    scraper = MagicMock()
    scraper.title.return_value = "No Yield Stew"
    scraper.ingredients.return_value = ["1 cup lentils", "2 cups water"]
    scraper.instructions_list.return_value = ["Simmer."]
    scraper.instructions.return_value = "Simmer."
    scraper.yields.return_value = None
    with patch("recipe_scrapers.scrape_html", return_value=scraper):
        result = url_ingest._try_recipe_scrapers("https://example.com/stew", "<html></html>")
    assert result is not None and result.title == "No Yield Stew"
    assert result.servings is None


def test_a_slow_lookup_is_given_up_at_the_deadline(monkeypatch):
    from app.ingestion import url_ingest
    release = threading.Event()

    def slow(host, port):
        release.wait(5)
        return [(socket.AF_INET, None, None, None, ("93.184.216.34", 0))]
    monkeypatch.setattr(url_ingest.socket, "getaddrinfo", slow)
    start = time.monotonic()
    try:
        with pytest.raises(url_ingest.SiteUnreachableError, match="took too long"):
            url_ingest.validate_public_url("https://slow-dns.example/", time.monotonic() + 0.3)
        assert time.monotonic() - start < 2
    finally:
        release.set()


def test_each_hop_gets_only_what_is_left(monkeypatch):
    """A redirect chain: the second hop's timeout is the budget left, not
    another full 15 seconds; once it's spent, no further connection."""
    from app.ingestion import url_ingest
    seen = []

    class Redirect:
        status_code, is_redirect, is_permanent_redirect = 302, True, False
        headers = {"Location": "https://two.example/"}
        url = "https://one.example/"
        text = content = b""

        def close(self):
            pass

    def fake_get(url, timeout, **kw):
        seen.append(timeout)
        time.sleep(0.25)
        return Redirect()
    monkeypatch.setattr(url_ingest, "TOTAL_FETCH_SECONDS", 0.6)
    monkeypatch.setattr(url_ingest, "validate_public_url", lambda url, deadline=None: "93.184.216.34")
    monkeypatch.setattr(url_ingest, "http_get", fake_get)
    monkeypatch.setattr(url_ingest, "_read_limited", lambda resp, url, max_bytes, deadline: b"")
    with pytest.raises(url_ingest.SiteUnreachableError, match="didn't answer within"):
        url_ingest.safe_get("https://one.example/")
    assert seen[0] <= 0.6 and all(t < seen[0] for t in seen[1:]) and len(seen) <= 3


def test_one_url_has_the_batch_limit(client):
    long_url = "https://example.com/" + "a" * 2000
    r = client.post("/api/ingest/url", headers={"X-Requested-With": "OpenThePantry"}, json={"url": long_url})
    # Refused by the field's limit, before any lookup or fetch.
    assert r.status_code == 422 and r.json()["detail"][0]["type"] == "string_too_long", r.text


@pytest.mark.parametrize("url,want", [
    ("https://user:secret@example.com/r/soup?token=abc#x", "https://example.com/r/soup?…"),
    ("https://example.com:8443/soup", "https://example.com:8443/soup"),
    ("https://example.com/soup", "https://example.com/soup"),
    ("not a url?sig=1", "not a url"),
])
def test_log_url_drops_secrets(url, want):
    from app.ingestion.url_ingest import log_url
    assert log_url(url) == want


def test_the_log_never_has_the_query(monkeypatch):
    import io
    import logging

    import requests

    from app.ingestion import url_ingest
    monkeypatch.setattr(url_ingest, "validate_public_url", lambda url, deadline=None: "93.184.216.34")

    def refuse(*a, **k):
        raise requests.ConnectionError("refused")
    monkeypatch.setattr(url_ingest, "http_get", refuse)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    url_ingest.log.addHandler(handler)
    try:
        with pytest.raises(url_ingest.SiteUnreachableError):
            url_ingest.safe_get("https://cook:pw@example.com/soup?sig=SECRET123")
    finally:
        url_ingest.log.removeHandler(handler)
    text = buf.getvalue()
    assert "SECRET123" not in text and "cook:pw" not in text and "example.com/soup" in text


def test_time_limit_stops_a_byte_at_a_time_download(monkeypatch):
    from unittest.mock import patch

    from app.ingestion import url_ingest

    monkeypatch.setattr(url_ingest, "TOTAL_FETCH_SECONDS", 0.2)
    data = b"x" * 10_000
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest, "http_get", return_value=_trickling_response(data, delay=0.05)):
        with pytest.raises(url_ingest.UrlValidationError, match="longer than"):
            url_ingest.safe_get("https://slow.example/")


def _slow_server(mode: str, header_delay: float) -> int:
    """A real local server for one connection. "headers": the header bytes
    come one every 0.2 s for ten seconds; "body": headers after
    `header_delay` seconds, a few bytes of page, then silence. Returns its
    port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        try:
            if mode != "handshake":
                conn.recv(65536)
            if mode == "handshake":
                # A TLS record header announcing 16 KB, then a byte at a
                # time: the client waits for the rest of the record.
                conn.send(b"\x16\x03\x03\x40\x00")
                for _ in range(60):
                    conn.send(b"\x00")
                    time.sleep(0.2)
            elif mode == "headers":
                for b in b"HTTP/1.1 200 OK\r\nX-Pad: " + b"a" * 60:
                    conn.send(bytes([b]))
                    time.sleep(0.2)
            else:
                time.sleep(header_delay)
                conn.send(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 5000\r\n\r\n<html>")
                time.sleep(10)
        except OSError:
            pass            # the client shut the connection: what is being tested
        finally:
            conn.close()
            srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def _fetch_slow(mode: str, budget: float, header_delay: float = 0.8, **kwargs):
    """safe_get against _slow_server in a thread, so a fetch that outlasts
    its budget fails the test rather than hanging it. (outcome, seconds)"""
    from app.ingestion import url_ingest
    port = _slow_server(mode, header_delay)
    out = {}

    def run():
        try:
            url_ingest.safe_get(f"http://slow.example:{port}/", **kwargs)
            out["error"] = None
        except BaseException as e:      # noqa: BLE001 -- the type is what is asserted
            out["error"] = e

    with patch.object(url_ingest, "TOTAL_FETCH_SECONDS", budget), \
         patch.object(url_ingest, "validate_public_url", lambda url, deadline=None: "127.0.0.1"):
        t0 = time.monotonic()
        th = threading.Thread(target=run, daemon=True)
        th.start()
        th.join(6)
        return out.get("error", "still running after 6 s"), time.monotonic() - t0


def test_a_server_trickling_its_headers_is_given_up_at_the_deadline():
    """GPT review of 0174: the budget was checked between reads, and a
    timeout is per read, so headers sent a byte at a time held a 1.5-second
    budget for over 25 seconds (and would have for minutes)."""
    from app.ingestion import url_ingest
    err, seconds = _fetch_slow("headers", budget=1.0)
    assert isinstance(err, url_ingest.SiteUnreachableError), err
    assert "didn't answer within 1.0 seconds" in str(err)
    assert seconds < 3


def test_a_page_that_stalls_after_its_headers_ends_at_the_deadline():
    """The headers came at 0.8 s of a 1-second budget; the wait for the page
    then ran a full read timeout (1 s here, 15 in production) past it, and
    ended in urllib3's own error, not caught anywhere."""
    from app.ingestion import url_ingest
    err, seconds = _fetch_slow("body", budget=1.0)
    assert isinstance(err, url_ingest.SiteUnreachableError), err
    assert seconds < 1.6


def test_the_deadline_covers_the_tls_handshake():
    """GPT review of 0175: the connection was watched only once connect()
    returned, after the TLS handshake. Python times the whole handshake
    against the socket timeout, so a trickle can't hold it forever, but it
    got the full per-read timeout past the deadline: 4 s here for a
    1-second deadline. Now it ends at the deadline."""
    import requests

    from app import tls
    port = _slow_server("handshake", 0)
    out = {}

    def run():
        try:
            tls.get(f"https://slow.example:{port}/", pin="127.0.0.1", timeout=4, stream=True,
                    deadline=time.monotonic() + 1.0)
            out["error"] = None
        except Exception as e:      # noqa: BLE001 -- the type is what is asserted
            out["error"] = e

    t0 = time.monotonic()
    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(8)
    assert isinstance(out.get("error"), requests.RequestException), out
    assert time.monotonic() - t0 < 2.5


def test_a_page_that_stalls_longer_than_the_read_timeout_is_a_plain_error():
    """Inside the budget but past the per-read timeout: urllib3's
    ReadTimeoutError came out of the body read as it was, a server error on
    the page. Now the same sentence as any other site that didn't answer."""
    from app.ingestion import url_ingest
    err, _ = _fetch_slow("body", budget=10, header_delay=0, timeout=0.3)
    assert isinstance(err, url_ingest.SiteUnreachableError), err
    assert "didn't answer within 0.3 seconds" in str(err)


def test_normal_page_still_downloads_via_read1():
    """The switch away from iter_content must not break the ordinary path."""
    from unittest.mock import patch

    from app.ingestion import url_ingest

    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest, "http_get", return_value=_trickling_response(b"<html>ok</html>")):
        assert url_ingest.safe_get("https://fine.example/").text == "<html>ok</html>"


def test_downloads_stop_at_the_size_limit():
    """safe_get loaded whole responses into memory; the limit is enforced
    while reading, even when the server doesn't say how big it is."""
    from app.ingestion import url_ingest
    chunks = [b"x" * 65536] * 40            # 2.5 MB, no Content-Length
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest, "http_get", return_value=_streaming_response(chunks)):
        with pytest.raises(url_ingest.FetchTooLargeError):
            url_ingest.safe_get("https://big.example/", max_bytes=1024 * 1024)


def test_downloads_stop_at_the_time_limit(monkeypatch):
    import time

    from app.ingestion import url_ingest

    # While the body is read...
    with pytest.raises(url_ingest.UrlValidationError, match="longer than") as e:
        url_ingest._read_limited(_streaming_response([b"a", b"b"]), "https://slow.example/?token=x",
                                 10 ** 6, time.monotonic() - 1)
    assert "token=x" not in str(e.value)
    # ...and, since 0157, before a connection once the budget is spent.
    monkeypatch.setattr(url_ingest, "TOTAL_FETCH_SECONDS", 0)
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest, "http_get", return_value=_streaming_response([b"a", b"b"])) as get:
        with pytest.raises(url_ingest.SiteUnreachableError, match="didn't answer"):
            url_ingest.safe_get("https://slow.example/")
    assert not get.called


def test_normal_page_still_downloads():
    from app.ingestion import url_ingest
    with patch.object(url_ingest, "validate_public_url"), \
         patch.object(url_ingest, "http_get", return_value=_streaming_response([b"<html>ok</html>"])):
        assert url_ingest.safe_get("https://fine.example/").text == "<html>ok</html>"
