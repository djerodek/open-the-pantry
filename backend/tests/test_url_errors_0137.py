"""Add from URL showed requests' own text when a site didn't answer
("HTTPSConnectionPool(host='www.example.com', port=443): Read timed out.
(read timeout=15)"), in English only. Each network failure now has a plain
sentence, in English and French; the details stay in the log."""
import pytest
import requests

H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.fixture
def public(monkeypatch):
    from app.ingestion import url_ingest
    monkeypatch.setattr(url_ingest, "validate_public_url", lambda url: None)
    return url_ingest


def _failing(exc):
    def get(*a, **k):
        raise exc
    return get


@pytest.mark.parametrize("exc,english,french", [
    (requests.exceptions.ReadTimeout("HTTPSConnectionPool(host='www.example.com', port=443): Read timed out."),
     "www.example.com didn't answer within 15 seconds; some sites only answer web browsers. Save the page as a "
     "PDF or take a screenshot, and add that instead.",
     "www.example.com n'a pas répondu en 15 secondes; certains sites ne répondent qu'aux navigateurs web."),
    (requests.exceptions.ConnectTimeout("x"), "www.example.com didn't answer within 15 seconds", "n'a pas répondu"),
    (requests.exceptions.SSLError("certificate verify failed"),
     "www.example.com's security certificate couldn't be verified, so the page wasn't fetched.",
     "Le certificat de sécurité de www.example.com n'a pas pu être vérifié"),
    (requests.exceptions.ConnectionError("Connection refused"),
     "Couldn't connect to www.example.com. Check the address, or try again later.",
     "Impossible de se connecter à www.example.com."),
    (requests.exceptions.ChunkedEncodingError("broken"), "www.example.com couldn't be reached. Try again later.",
     "www.example.com est injoignable."),
])
def test_a_plain_message_for_each_failure(client, public, monkeypatch, exc, english, french):
    monkeypatch.setattr(public, "http_get", _failing(exc))
    with pytest.raises(public.SiteUnreachableError) as info:
        public.safe_get("https://www.example.com/recipe")
    assert str(info.value).startswith(english) and info.value.__cause__ is exc
    r = client.post("/api/ingest/url", headers=H, json={"url": "https://www.example.com/recipe"})
    assert r.status_code == 422 and r.json()["detail"].startswith(english)
    assert "HTTPSConnectionPool" not in r.text
    r = client.post("/api/ingest/url", headers={**H, "X-App-Lang": "fr"},
                    json={"url": "https://www.example.com/recipe"})
    assert french in r.json()["detail"], r.json()["detail"]


def test_the_log_keeps_the_details(public, monkeypatch):
    seen = []
    monkeypatch.setattr(public.log, "warning", lambda msg, *a, **k: seen.append(msg % a))
    monkeypatch.setattr(public, "http_get", _failing(requests.exceptions.ReadTimeout("Read timed out. (read timeout=15)")))
    with pytest.raises(public.SiteUnreachableError):
        public.safe_get("https://www.example.com/recipe")
    assert any("ReadTimeout" in s and "read timeout=15" in s for s in seen)


def test_an_emailed_link_gives_the_short_reason(public, monkeypatch, tmp_path):
    import email
    from app.ingestion import email_processing
    monkeypatch.setattr(public, "http_get", _failing(requests.exceptions.ReadTimeout("x")))
    raw = (b"From: a@example.com\r\nSubject: [RECIPE]\r\nContent-Type: text/plain\r\n\r\n"
           b"https://www.example.com/recipe\r\n")
    result = email_processing.process_tagged_email(email.message_from_bytes(raw), str(tmp_path))
    assert result["success"] is False
    assert ("link https://www.example.com/recipe: www.example.com didn't answer within 15 seconds; "
            "some sites only answer web browsers.;") in result["error"]
    assert "HTTPSConnectionPool" not in result["error"]
