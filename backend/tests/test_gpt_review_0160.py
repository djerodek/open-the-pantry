"""GPT review of 0159 and 0160: a password file with every key but a value
of the wrong kind wasn't seen as damaged (unlocking raised a TypeError); a
zip whose end record under-counts its entries got past the entry limit
while its directory stayed under the size bound; the From header was
fetched before the message-size check. (The redaction test's precedence:
test_claude_review_0158.py.)"""
import json
import os
import zipfile

import pytest

from . import _zip_directory

H = {"X-Requested-With": "OpenThePantry"}
GOOD = {"salt": "00" * 16, "hash": "11" * 32, "n": 2 ** 14, "r": 8, "p": 1}


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


@pytest.mark.parametrize("change", [
    {"hash": 123}, {"salt": "zz" * 16}, {"hash": "11" * 31}, {"n": "16384"}, {"r": True}, {"p": 1.0},
    {"n": 2 ** 21}, {"n": 3000}, {"r": 64}, {"n": 2 ** 20, "r": 8}, {"salt": None},
])
def test_wrong_kinds_of_values_are_damaged(change):
    from app import admin_lock
    assert admin_lock._valid(GOOD)
    assert not admin_lock._valid({**GOOD, **change}), change


def test_a_wrongly_typed_file_reads_as_damaged_not_a_crash(client):
    """End to end: the recovery message instead of a 500."""
    from app import admin_lock
    kept = admin_lock.PASSWORD_PATH + ".kept-by-test"
    os.replace(admin_lock.PASSWORD_PATH, kept)
    with open(admin_lock.PASSWORD_PATH, "w") as f:
        json.dump({**GOOD, "hash": 123}, f)
    try:
        assert admin_lock.damaged() and admin_lock._verify("anything") is False
        # A client without the suite's unlock cookie: a damaged file ends
        # any session it is shown, and the shared one must outlive this.
        from fastapi.testclient import TestClient
        from app.main import app
        bare = TestClient(app, headers={"X-Requested-With": "OpenThePantry"})
        status = bare.get("/api/admin/status")
        assert status.status_code == 200 and status.json()["damaged"] is True
        r = bare.post("/api/admin/unlock", headers=H, json={"password": "anything-long"})
        assert r.status_code == 409 and "damaged" in r.json()["detail"]
    finally:
        os.replace(kept, admin_lock.PASSWORD_PATH)


def test_a_lying_entry_count_under_the_size_bound_is_refused(tmp_path, monkeypatch):
    """100,000 real entries listed as 10: 4.7 MB of directory, under the
    5.12 MB bound for 20,000; refused by counting, before zipfile parses."""
    from app import file_validation
    path = tmp_path / "liar.zip"
    _zip_directory.make(str(path), 100_000, lie=10)
    assert os.path.getsize(path) < 20_000 * file_validation.ZIP_DIR_BYTES_PER_ENTRY

    def no_parse(*a, **k):
        raise AssertionError("zipfile parsed it")
    monkeypatch.setattr(zipfile, "ZipFile", no_parse)
    with pytest.raises(ValueError, match="more than 20000"):
        file_validation.check_zip_size(str(path), 20_000, ValueError)


def test_a_true_count_at_the_limit_still_opens(tmp_path):
    from app import file_validation
    path = tmp_path / "ok.zip"
    _zip_directory.make(str(path), 20_000)
    file_validation.check_zip_size(str(path), 20_000, ValueError)
    real = tmp_path / "real.zip"
    with zipfile.ZipFile(real, "w") as zf:
        for i in range(3):
            zf.writestr(f"f{i}.txt", "x" * i, compress_type=zipfile.ZIP_DEFLATED)
        zf.comment = b"PK\x05\x06 in a comment"
    file_validation.check_zip_size(str(real), 3, ValueError)


class _Imap:
    """Records what was fetched."""
    def __init__(self, size, from_header=b"From: friend@example.com\r\n\r\n"):
        self.size, self.from_header, self.asked = size, from_header, []

    def fetch(self, msg_id, what):
        self.asked.append(what)
        if what == "(RFC822.SIZE)":
            return "OK", [f"1 (RFC822.SIZE {self.size})".encode()]
        if "HEADER.FIELDS (FROM)" in what:
            return "OK", [(b"1 (BODY[HEADER.FIELDS (FROM)]<0> {%d}" % len(self.from_header), self.from_header)]
        raise AssertionError(f"fetched {what}")


def test_an_oversized_email_is_refused_before_its_header(client, monkeypatch):
    from app import email_client
    from app.database import SessionLocal
    from app.routes import email as email_routes
    imap = _Imap(40 * 1024 * 1024)
    with SessionLocal() as db:
        s = email_routes._get_email_settings(db)
        s.enabled, s.imap_host, s.username, s.password_encrypted = True, "imap.example.com", "u", "x"
        s.allowed_senders = "friend@example.com"
        db.commit()
    seen = []
    monkeypatch.setattr(email_routes.crypto, "decrypt_secret", lambda v: "pw")
    monkeypatch.setattr(email_client, "connect_imap", lambda *a, **k: imap)
    imap.logout = lambda: None
    monkeypatch.setattr(email_client, "search_unseen_by_subject", lambda i, kw: [b"1"])
    monkeypatch.setattr(email_client, "mark_seen", lambda i, m: seen.append(m))
    try:
        out = email_routes.run_email_scan()
        assert imap.asked == ["(RFC822.SIZE)"], imap.asked          # no header, no body
        assert seen == [b"1"] and "the limit is 30 MB" in " ".join(out["messages"])
    finally:
        from sqlalchemy import text
        with SessionLocal() as db:
            s = email_routes._get_email_settings(db)
            s.enabled, s.imap_host, s.username, s.password_encrypted, s.allowed_senders = False, None, None, None, ""
            db.execute(text("DELETE FROM email_notification_queue"))
            db.commit()


def test_the_from_header_is_fetched_bounded_and_shown_short():
    from app import email_client
    long_from = b"From: " + b"a" * 5000 + b"@example.com\r\n\r\n"
    imap = _Imap(1000, long_from)
    sender = email_client.fetch_sender(imap, b"1")
    assert "<0.2048>" in imap.asked[0]
    assert len(sender) <= email_client.SENDER_SHOWN


def test_the_count_message_reads_in_french():
    from app.i18n import translate
    assert translate("This archive has more than 20000 files; the limit is 20000.", "fr") == \
        "Cette archive contient plus de 20000 fichiers; la limite est 20000."
