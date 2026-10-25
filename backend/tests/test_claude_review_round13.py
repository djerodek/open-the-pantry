"""Claude review round 13 (full pass over 0124): the inbox login didn't
check the mail server's certificate; a long digit run in an ingredient
line took time growing with its square; an email in an unknown charset
failed whole; a failed automatic backup waited a day; a scheduled email
scan failure was logged at DEBUG only; the published image didn't wait
for CI (workflow files, checked here as text)."""
import ast
import email
import ssl
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import re

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def test_the_inbox_login_checks_the_certificate(monkeypatch):
    from app import email_client
    seen = {}

    class FakeIMAP:
        def __init__(self, host, port, **kw):
            seen.update(kw)

        def login(self, *a):
            pass

        def select(self, *a):
            pass

    monkeypatch.setattr(email_client.imaplib, "IMAP4_SSL", FakeIMAP)
    email_client.connect_imap("imap.example.com", 993, "u", "p")
    ctx = seen["ssl_context"]
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True


def test_a_long_digit_run_parses_at_once():
    from app.ingestion.ingredient_parser import parse_ingredient_block, parse_ingredient_line, MAX_LINE
    started = time.monotonic()
    out = parse_ingredient_block(["1" * 40_000, "2" * 1_000_000])
    assert time.monotonic() - started < 1          # was 3.5 s for the 40,000 alone
    assert [len(i["raw_line"]) for i in out] == [40_000, 1_000_000]    # kept whole: refused on save, not cut
    assert all(len(i["name"]) <= MAX_LINE for i in out)
    # The amounts it reads are unchanged.
    for line, qty in [("1 1/2 cups flour", "1 1/2"), ("3/4 cup sugar", "3/4"), ("1-2 tbsp oil", "1-2"),
                      ("1 to 1 1/2 cups milk", "1 to 1 1/2"), ("2–2½ cups stock", "2–2½"), ("1½ tsp salt", "1½"),
                      ("1,5 tasse de lait", "1,5"), ("2 eggs", "2"), ("1-1/2 cups rice", "1-1/2")]:
        assert parse_ingredient_line(line)["quantity"] == qty, line


def test_an_unknown_charset_is_read_as_utf8():
    from app.ingestion.email_processing import extract_email_parts
    for charset in ("unknown-8bit", "x-user-defined"):
        raw = (f"From: a@example.com\r\nSubject: [RECIPE]\r\nContent-Type: text/plain; charset={charset}\r\n"
               "Content-Transfer-Encoding: 8bit\r\n\r\nCrème brûlée https://example.com/r\r\n").encode("utf-8")
        parts = extract_email_parts(email.message_from_bytes(raw))
        assert "Crème brûlée" in parts["body_text"], charset


def test_a_failed_backup_is_tried_again():
    from app.scheduled_backup import due
    tz = timezone(timedelta(hours=-4))
    at3 = datetime(2026, 10, 18, 3, 5, tzinfo=tz)
    yesterday = at3 - timedelta(days=1)
    # Failed at 3:05: not again within the hour, then hourly until one works.
    assert not due(True, 3, yesterday, at3 + timedelta(minutes=30), failed_at=at3)
    assert due(True, 3, yesterday, at3 + timedelta(hours=1), failed_at=at3)
    assert due(True, 3, yesterday, at3.replace(hour=14), failed_at=at3.replace(hour=13))
    # Once one works, the next is tomorrow at 3.
    assert not due(True, 3, at3.replace(hour=14), at3.replace(hour=15))
    # "Back up now" at 1:00 doesn't stand in for the 3:00 one.
    assert due(True, 3, at3.replace(hour=1), at3)
    # Never worked: at once, failures still an hour apart.
    assert due(True, 3, None, at3)
    assert not due(True, 3, None, at3, failed_at=at3 - timedelta(minutes=10))


def test_a_failed_backup_keeps_the_last_good_one(client, monkeypatch, tmp_path):
    from app import main, scheduled_backup
    from app.database import SessionLocal
    monkeypatch.setattr(scheduled_backup, "BACKUP_DIR", str(tmp_path / "b"))
    main.run_scheduled_backup()
    with SessionLocal() as db:
        good = main._backup_schedule(db).last_success_at
    assert good is not None

    def fail(*a, **k):
        raise scheduled_backup.BackupError("disk full")
    monkeypatch.setattr(scheduled_backup, "make", fail)
    try:
        main.run_scheduled_backup()
    except scheduled_backup.BackupError:
        pass
    with SessionLocal() as db:
        row = main._backup_schedule(db)
        assert row.last_ok is False and row.last_success_at == good and row.last_run_at > good


def test_a_scheduled_scan_failure_is_logged_in_full():
    tree = ast.parse((ROOT / "backend/app/main.py").read_text(encoding="utf-8"))
    loop = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_daily_email_scan_loop")
    handlers = [h for h in ast.walk(loop) if isinstance(h, ast.ExceptHandler)]
    assert handlers and all("log.exception" in ast.unparse(h) for h in handlers)


def test_the_image_is_published_only_after_ci():
    """Read as text (no YAML library in the test requirements)."""
    workflows = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / ".github/workflows").glob("*.yml")}
    assert [name for name, text in workflows.items() if "push: true" in text] == ["ci.yml"]
    ci = workflows["ci.yml"]
    publish = ci[ci.index("\n  publish:"):]
    assert "needs: [test, browser, docker-build]" in publish
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/main'" in publish
    assert publish.index("push: true") > publish.index("needs:")
    # Every action pinned to a commit.
    for name, text in workflows.items():
        for ref in re.findall(r"uses:\s*\S+@(\S+)", text):
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (name, ref)
