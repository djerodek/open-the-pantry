"""Claude review of 0148 (full pass): titles with "by" cut ("Death by
Chocolate Cake" -> "Death"); /docs and /openapi.json open on the main
port; titles written into index.csv could run as spreadsheet formulas;
RESTORE.txt still told people to copy encryption.key; .env and certs sent
to the Docker build; CI token with default rights and an unpinned
pip-audit. Restore findings: test_backup_settings_0143.py."""
import csv
import io
import re
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
H = {"X-Requested-With": "OpenThePantry"}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.mark.parametrize("title,kept", [
    ("Death by Chocolate Cake", "Death by Chocolate Cake"), ("Stand by Me Brownies", "Stand by Me Brownies"),
    ("Pulled Pork Sandwich by Samuel Fortier, Anna Lee", "Pulled Pork Sandwich"),
    ("Lemon Tart by Anna Smith", "Lemon Tart"),
])
def test_a_byline_goes_a_dish_name_stays(title, kept):
    from app.ingestion.pdf_ingest import _tidy_title
    assert _tidy_title(title) == kept


def test_no_route_map_on_the_main_port(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_titles_dont_become_formulas_in_index_csv(client):
    r = client.post("/api/recipes", headers=H, json={"title": "=HYPERLINK(\"http://x.example\",\"click\")",
                                                     "source_type": "url", "ingredients": [{"raw_line": "1 egg"}],
                                                     "steps": ["Boil."]}).json()
    try:
        z = zipfile.ZipFile(io.BytesIO(client.get("/api/backup/pdfs.zip").content))
        rows = list(csv.DictReader(io.StringIO(z.read("index.csv").decode("utf-8"))))
        titles = [row["title"] for row in rows if row["id"] == str(r["id"])]
        assert titles == ["'=HYPERLINK(\"http://x.example\",\"click\")"]
        assert all(not row["title"].startswith(("=", "+", "-", "@")) for row in rows)
    finally:
        client.delete(f"/api/recipes/{r['id']}", headers=H)
        client.delete(f"/api/recently-deleted/{r['id']}", headers=H)


def test_restore_instructions_dont_ask_for_encryption_key():
    from app.backup import RESTORE_INSTRUCTIONS, RESTORE_INSTRUCTIONS_FR
    assert "copy encryption.key" not in RESTORE_INSTRUCTIONS
    assert "copiez encryption.key" not in RESTORE_INSTRUCTIONS_FR


def test_secrets_stay_out_of_the_docker_build():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", "*.env", "letsencrypt/", "data/"} <= set(ignore)


def test_ci_token_reads_only_and_the_audit_is_pinned():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^permissions:\n  contents: read$", ci, re.M)
    assert re.search(r"pip install pip-audit==\d+\.\d+\.\d+", ci)
