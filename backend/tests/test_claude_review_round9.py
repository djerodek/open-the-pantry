"""Claude review round 9 (0086-0087).

1. A link with a bad port ("https://example.com:443443/x") made
   duplicates.normalize_url raise: a whole batch of links returned 500, and
   once one saved recipe had such a link, every duplicate check, batch and
   emailed link failed. It's now "not a usable address"; the batch's link
   check sits inside the per-link try.
2. Any site could start an import: /?share_url= submitted Add from URL on
   load, also from a hidden frame. The shared link is now only filled in,
   and both ports refuse to be framed (X-Frame-Options, CSP
   frame-ancestors).
3. Scaling (test_ui_consistency): ambiguous thousands and compound amounts
   are left as written and flagged.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

H = {"X-Requested-With": "OpenThePantry"}
ROOT = Path(__file__).resolve().parents[2]
BAD = ["https://example.com:443443/stew", "https://example.com:abc/stew", "http://[::1/x"]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_bad_ports_are_not_addresses():
    from app.duplicates import normalize_url
    for url in BAD:
        assert normalize_url(url) is None, url
    assert normalize_url("https://example.com:8443/stew") == "example.com:8443/stew"


def test_a_saved_recipe_with_a_bad_port_breaks_nothing(client, monkeypatch):
    from app import main, models
    from app.database import SessionLocal
    from .test_duplicates import _Fetched
    db = SessionLocal()
    r = models.Recipe(title="R9 bad port", source_type="url", source_url=BAD[0])
    db.add(r)
    db.commit()
    rid = r.id
    db.close()
    monkeypatch.setattr(main, "ingest_url", lambda url: _Fetched("R9 batch " + url[-1]))
    made = []
    try:
        assert client.get("/api/recipes/duplicates", params={"url": "https://example.com/stew"}).json() == []
        assert client.get("/api/recipes/duplicates", params={"url": BAD[1]}).status_code == 200
        res = client.post("/api/ingest/url/batch", headers=H,
                          json={"urls": ["https://example.com/r9-a", BAD[0], "https://example.com/r9-b"]})
        assert res.status_code == 200
        body = res.json()
        made = [s["recipe_id"] for s in body["succeeded"]]
        assert len(made) == 3          # a bad link is just a link: the fetch decides (faked here)
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [rid] + made})


def test_a_failure_in_the_link_check_is_that_links_failure(client, monkeypatch):
    from app import main, duplicates
    from .test_duplicates import _Fetched
    monkeypatch.setattr(main, "ingest_url", lambda url: _Fetched("R9 second " + url[-1]))

    def boom(db, url):
        if url.endswith("boom"):
            raise RuntimeError("check failed")
        return None

    monkeypatch.setattr(duplicates, "same_link", boom)
    res = client.post("/api/ingest/url/batch", headers=H,
                      json={"urls": ["https://example.com/r9-1", "https://example.com/boom", "https://example.com/r9-3"]})
    body = res.json()
    try:
        assert res.status_code == 200
        assert [f["url"] for f in body["failed"]] == ["https://example.com/boom"]
        assert len(body["succeeded"]) == 2
    finally:
        client.post("/api/recipes/batch-delete", headers=H, json={"ids": [s["recipe_id"] for s in body["succeeded"]]})


def test_neither_port_can_be_framed(client):
    from app.main import picker_app
    pc = TestClient(picker_app)
    for c, paths in ((client, ["/", "/index.html", "/js/app.js", "/api/recipes", "/healthz", "/api/recipes/0"]),
                     (pc, ["/", "/api/picker/options", "/healthz", "/nothing-here"])):
        for path in paths:
            r = c.get(path)
            assert r.headers.get("x-frame-options") == "DENY", path
            assert r.headers.get("content-security-policy") == "frame-ancestors 'none'", path


def test_a_shared_link_waits_for_a_tap():
    js = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    form = js[js.index("  function showUrlForm(prefill) {"):js.index("  function sharedLinkFromAddress(")]
    assert "requestSubmit" not in form and ".submit()" not in form
    assert 'tx("Shared link: press Fetch recipe to import it.")' in form
    assert "requestSubmit" not in js[js.index("  function sharedLinkFromAddress("):js.index("  function showPdfForm(")]
