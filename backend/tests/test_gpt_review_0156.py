"""GPT review of 0156: a link import's 30-second budget didn't cover the
name lookup or each connection (each hop got a full 15-second timeout);
one URL had no length limit (fetched, then refused at save); full URLs,
query and user:password included, went into the log."""
import socket
import threading
import time
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


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
