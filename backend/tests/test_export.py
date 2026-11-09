"""Share and export: PDF, HTML and JSON of a recipe, the PDF bundle, and Share -> Email PDF."""
import io
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


H = {"X-Requested-With": "OpenThePantry"}


def _export(client, rid, lang="en"):
    r = client.get(f"/api/recipes/{rid}/export.html", headers={**H, "X-App-Lang": lang})
    assert r.status_code == 200, r.text
    return r.text


FR = {"X-App-Lang": "fr"}


def _recipe(client, title, notes=None, **extra):
    body = {"title": title, "source_type": "manual",
            "ingredients": [{"raw_line": "2 cups flour"}], "steps": ["Mix."]}
    body.update(extra)
    r = client.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    if notes is not None:
        assert client.patch(f"/api/recipes/{rid}/notes", json={"notes": notes}).status_code == 200
    return rid


@pytest.fixture
def mail(client, monkeypatch):
    from cryptography.fernet import Fernet

    from app import crypto, main, routes
    monkeypatch.setenv(crypto.ENCRYPTION_KEY_ENV_VAR, Fernet.generate_key().decode())
    routes.email._recipe_email_times.clear()
    body = {"enabled": False, "imap_host": "mail.me.example", "imap_port": 993, "imap_use_ssl": True,
            "smtp_host": "mail.me.example", "smtp_port": 465, "smtp_use_tls": False,
            "username": "pantry@me.example", "password": "s3cret", "notify_email": "me@me.example",
            "subject_keyword": "[RECIPE]", "daily_scan_hour": 3, "cooldown_minutes": 0}
    assert client.put("/api/email-settings", json=body).status_code == 200
    yield body
    client.put("/api/email-settings/recipients", json={"recipients": []})
    client.put("/api/email-settings", json=body)  # back to English
    client.delete("/api/email-settings/password")
    routes.email._recipe_email_times.clear()


def test_the_export_source_line_and_language(client):
    body = {"ingredients": [{"raw_line": "1 tasse de farine"}],
            "steps": ["Mélanger la farine avec le beurre dans un bol."]}
    r = client.post("/api/recipes", headers=H, json={"title": "Crêpes", "source_type": "import",
                                                     "language": "fr", **body}).json()
    try:
        html = _export(client, r["id"])
        assert "Source: Imported" in html and "Source: import" not in html
        assert '<html lang="fr-CA">' in html                       # the recipe's language
        assert "Ingredients" in html                               # the interface's labels
        assert "Source : Importée" in _export(client, r["id"], "fr")
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{r['id']}", headers=H)


def test_long_unbroken_text_wraps_in_the_pdf():
    template = (ROOT / "backend/app/templates/recipe_export.html").read_text(encoding="utf-8")
    body = template[template.index("  body {"):template.index("}", template.index("  body {"))]
    assert "overflow-wrap: anywhere" in body


def test_export_labels_follow_the_app_language(client):
    rid = _recipe(client, "Export Langue", servings="4", notes="Moins de sel.")
    en = client.get(f"/api/recipes/{rid}/export.html?include_notes=true").text
    fr = client.get(f"/api/recipes/{rid}/export.html?include_notes=true", headers=FR).text
    assert "<h2>Ingredients</h2>" in en and "Servings:" in en
    assert "<h2>Ingrédients</h2>" in fr and "<h2>Préparation</h2>" in fr
    # lang= is the recipe's language from 0138 (this one is English); the
    # labels follow the app (Claude review of 0138).
    assert "Portions\u00a0: 4" in fr and 'lang="en"' in fr
    assert "saisie manuelle" in fr


def test_email_pdf_sends_from_the_ingest_account(client, mail):
    rid = _recipe(client, "Tourtière\nBcc: x@evil.example", notes="secret note")
    with patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email_with_attachment") as send:
        r = client.post(f"/api/recipes/{rid}/email",
                        json={"to": ["Ann@family.example", " ann@family.example ", "bob@family.example"],
                              "message": "Try this one."},
                        headers=FR)
    assert r.status_code == 200, r.text
    args = send.call_args.args
    assert args[1] == "pantry@me.example"
    assert args[2] == ["Ann@family.example", "bob@family.example"]
    assert args[3] == "Recette : Tourtière Bcc: x@evil.example"  # newline flattened
    assert args[4].startswith("Try this one.") and "Envoyé depuis Open the Pantry." in args[4]
    assert args[5][:4] == b"%PDF" and args[6].endswith(".pdf")
    assert b"secret note" not in args[5]
    assert r.json()["recent_recipients"][:2] == ["Ann@family.example", "bob@family.example"]
    assert client.get("/api/email-settings").json()["recent_recipients"][:2] == \
        ["Ann@family.example", "bob@family.example"]


def test_a_real_send_builds_a_pdf_attachment():
    from email import message_from_bytes

    from app import email_client
    smtp = MagicMock()
    email_client.send_email_with_attachment(smtp, "a@x.example", ["b@y.example", "c@y.example"],
                                            "Recipe: Pâté", "Hi", b"%PDF-1.4 x", "Pâté.pdf")
    msg = smtp.send_message.call_args.args[0]
    raw = msg.as_bytes()
    parsed = message_from_bytes(raw)
    assert parsed["To"] == "b@y.example, c@y.example"
    parts = [p for p in parsed.walk() if p.get_content_disposition() == "attachment"]
    assert parts[0].get_filename() == "Pâté.pdf" and parts[0].get_payload(decode=True) == b"%PDF-1.4 x"


@pytest.mark.parametrize("to, status, fragment", [
    ([], 400, "Ajoutez au moins un destinataire."),
    (["not-an-address"], 400, "« not-an-address » n'est pas une adresse courriel."),
    (["a@b.example\r\nBcc: z@evil.example"], 400, "n'est pas une adresse courriel"),
    ([f"p{i}@x.example" for i in range(6)], 400, "Au plus 5 destinataires"),
])
def test_email_pdf_validation(client, mail, to, status, fragment):
    rid = _recipe(client, "Validation Courriel")
    with patch("app.routes.email.email_client.connect_smtp") as smtp:
        r = client.post(f"/api/recipes/{rid}/email", json={"to": to}, headers=FR)
    assert r.status_code == status and fragment in r.json()["detail"]
    smtp.assert_not_called()


def test_email_pdf_reports_the_smtp_problem(client, mail):
    rid = _recipe(client, "Erreur SMTP")
    with patch("app.routes.email.email_client.connect_smtp",
               side_effect=Exception("Could not connect/login to SMTP (mail.me.example:465, implicit TLS): "
                                     "535 authentication failed")):
        r = client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]})
    assert r.status_code == 502
    assert r.json()["detail"].startswith("The email wasn't sent: Could not connect/login to SMTP")
    assert "535 authentication failed" in r.json()["detail"]


def test_email_pdf_is_rate_limited(client, mail):
    from app import main, routes
    rid = _recipe(client, "Limite Courriel")
    with patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email_with_attachment"):
        codes = [client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]}).status_code
                 for _ in range(routes.email.MAX_RECIPE_EMAILS_PER_HOUR + 1)]
    assert codes[:-1] == [200] * routes.email.MAX_RECIPE_EMAILS_PER_HOUR and codes[-1] == 429


def test_email_pdf_needs_sending_set_up(client, mail):
    rid = _recipe(client, "Pas configuré")
    client.delete("/api/email-settings/password")
    r = client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]})
    assert r.status_code == 400 and "isn't set up" in r.json()["detail"]


def test_recent_recipients_are_editable(client, mail):
    r = client.put("/api/email-settings/recipients",
                   json={"recipients": ["x@y.example", "X@y.example", "z@y.example"]})
    assert r.json()["recipients"] == ["x@y.example", "z@y.example"]
    assert client.get("/api/email-settings").json()["recent_recipients"] == ["x@y.example", "z@y.example"]
    bad = client.put("/api/email-settings/recipients", json={"recipients": ["nope"]})
    assert bad.status_code == 400


def test_failed_sends_do_not_use_up_the_hour(client, mail):
    from app import main, routes
    rid = _recipe(client, "Quota Après Panne")
    with patch("app.routes.email.email_client.connect_smtp", side_effect=Exception("server down")):
        for _ in range(routes.email.MAX_RECIPE_EMAILS_PER_HOUR):
            assert client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]}).status_code == 502
    with patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email_with_attachment"):
        assert client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]}).status_code == 200


def test_the_pdf_bundle_embeds_the_photo_through_a_data_url(client):
    pypdf = pytest.importorskip("pypdf")
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (200, 120, 40)).save(buf, "PNG")
    stored = client.post("/api/upload-image", headers=H,
                         files={"file": ("p.png", buf.getvalue(), "image/png")}).json()["stored_file"]
    r = client.post("/api/recipes", headers=H, json={"title": "PDF photo check", "source_type": "manual",
                                                     "image_path": stored, "ingredients": [{"raw_line": "1 egg"}],
                                                     "steps": ["Boil."]}).json()
    try:
        z = zipfile.ZipFile(io.BytesIO(client.get("/api/backup/pdfs.zip").content))
        name = next(n for n in z.namelist() if n.endswith(".pdf") and "PDF_photo_check" in n.replace(" ", "_"))
        page = pypdf.PdfReader(io.BytesIO(z.read(name))).pages[0]
        assert len(page.images) >= 1
        assert "PDF photo check" in page.extract_text()
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{r['id']}", headers=H)


def test_failed_email_sends_do_not_use_up_the_hour(client, mail):
    from app import main, routes
    rid = _recipe(client, "Quota Failure Test")
    with patch("app.routes.email.email_client.connect_smtp", side_effect=Exception("smtp down")):
        for _ in range(routes.email.MAX_RECIPE_EMAILS_PER_HOUR + 2):
            assert client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]}).status_code == 502
    with patch("app.routes.email.email_client.connect_smtp", return_value=MagicMock()), \
         patch("app.routes.email.email_client.send_email_with_attachment"):
        assert client.post(f"/api/recipes/{rid}/email", json={"to": ["a@b.example"]}).status_code == 200


def test_export_renders_the_line_as_typed(client):
    r = client.post("/api/recipes", json={
        "title": "Export Line Check",
        "source_type": "manual",
        "ingredients": [{"raw_line": "3 Tablespoons maple syrup"}],
        "steps": ["Mix it."],
    })
    assert r.status_code == 200
    recipe_id = r.json()["id"]

    html = client.get(f"/api/recipes/{recipe_id}/export.html").text
    assert "3 Tablespoons maple syrup" in html
    assert "3 tbsp maple syrup" not in html


def test_pdf_bundle_skips_a_traversal_image_path(client, data_dir, tmp_path):
    """build_pdf_bundle joined image_path with os.path.join; a hand-edited
    or restored database could point it outside uploads/."""
    import zipfile

    from app import backup, models
    from app.database import SessionLocal

    secret = tmp_path / "secret.png"
    from PIL import Image
    Image.new("RGB", (300, 300), "red").save(secret)
    db = SessionLocal()
    r = models.Recipe(title="Traversal Bundle", source_type="manual", image_path=str(secret))
    db.add(r); db.commit()
    try:
        out = tmp_path / "bundle.zip"
        backup.build_pdf_bundle([r], str(out))
        with zipfile.ZipFile(out) as z:
            pdf = z.read(next(n for n in z.namelist() if n.endswith(".pdf")))
        assert b"/Subtype /Image" not in pdf and b"/Subtype/Image" not in pdf
    finally:
        db.delete(r); db.commit(); db.close()


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


@pytest.mark.parametrize("title", ["Grandma’s Pie", "Stew — slow", "Pie \U0001F967", 'Say "cheese"'])
def test_export_works_for_titles_that_arent_latin1(client, title):
    """Curly apostrophes, dashes and emoji in titles made both exports 500."""
    rid = client.post("/api/recipes", json={"title": title, "source_type": "manual",
                      "ingredients": [], "steps": ["Bake"], "tags": []}).json()["id"]
    try:
        for ext in ("pdf", "html"):
            r = client.get(f"/api/recipes/{rid}/export.{ext}")
            assert r.status_code == 200, (ext, r.status_code)
            cd = r.headers["content-disposition"]
            cd.encode("latin-1")
            assert "filename*=UTF-8''" in cd
    finally:
        client.delete(f"/api/recipes/{rid}")
