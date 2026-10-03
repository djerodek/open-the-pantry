"""Settings -> HTTPS (app/https_setup.py).

cPanel is replaced by an in-memory fake that answers the same UAPI calls
with the response shapes lego's fixtures use. Let's Encrypt itself isn't
contacted here: the issuing step is stubbed, and the full flow was run by
hand against Pebble (Let's Encrypt's test server) with the same fake.

app.* is imported inside tests only; see the note in test_backup.py.
"""
import base64
import json
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timedelta, timezone

import pytest


class FakeCpanel:
    ZONE = "example.test"

    def __init__(self, user="marc", token="tok"):
        self.auth = f"cpanel {user}:{token}"
        self.serial = 2026100301
        self.next_line = 10
        self.records = [
            {"line_index": 1, "record_type": "SOA", "name": "example.test.",
             "data": ["ns1.example.test.", "admin.example.test.", str(self.serial), "3600"], "ttl": 86400},
            {"line_index": 2, "record_type": "A", "name": "example.test.", "data": ["203.0.113.10"], "ttl": 14400},
            {"line_index": 3, "record_type": "A", "name": "www.example.test.", "data": ["203.0.113.10"], "ttl": 14400},
            {"line_index": 4, "record_type": "CNAME", "name": "mail.example.test.", "data": ["example.test."], "ttl": 14400},
        ]
        self.calls = []

    def find(self, name, rtype):
        return [r for r in self.records if r["name"] == name and r["record_type"] == rtype]

    def get(self, url, params=None, headers=None, timeout=None):
        u = urlparse(url)
        self.calls.append((u.path.rsplit("/", 1)[-1], dict(params or {})))
        if headers.get("Authorization") != self.auth:
            return _Resp(403, {"status": 0, "errors": ["Access denied"]})
        params = params or {}
        if params.get("zone") != self.ZONE:
            return _Resp(200, {"status": 0, "data": None,
                               "errors": [f"You do not control a DNS zone named {params.get('zone')}."]})
        if u.path.endswith("/parse_zone"):
            data = [{"line_index": r["line_index"], "type": "record", "record_type": r["record_type"],
                     "dname_b64": base64.b64encode(r["name"].encode()).decode(),
                     "data_b64": [base64.b64encode(d.encode()).decode() for d in r["data"]], "ttl": r["ttl"]}
                    for r in self.records]
            return _Resp(200, {"status": 1, "data": data})
        assert u.path.endswith("/mass_edit_zone")
        assert int(params["serial"]) == self.serial, "stale serial"
        if "add" in params:
            rec = json.loads(params["add"])
            self.records.append({"line_index": self.next_line, "record_type": rec["record_type"],
                                 "name": rec["dname"], "data": rec["data"], "ttl": rec["ttl"]})
            self.next_line += 1
        elif "edit" in params:
            rec = json.loads(params["edit"])
            r = next(r for r in self.records if r["line_index"] == rec["line_index"])
            r.update(data=rec["data"], ttl=rec["ttl"])
        elif "remove" in params:
            self.records = [r for r in self.records if r["line_index"] != int(params["remove"])]
        self.serial += 1
        self.records[0]["data"][2] = str(self.serial)
        return _Resp(200, {"status": 1, "data": {"new_serial": str(self.serial)}})


class _Resp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        return self._body


@pytest.fixture(autouse=True)
def _isolated_data_dir(data_dir):
    """app.database reads RECIPE_APP_DATA_DIR at import; importing
    app.https_setup before conftest set it would point at /app/data."""


@pytest.fixture
def cpanel(monkeypatch):
    from app import https_setup
    fake = FakeCpanel()
    monkeypatch.setattr(https_setup.requests, "get", fake.get)
    return fake, https_setup.CpanelDns("https://cp.example.test:2083", "marc", "tok")


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("CPANEL_USERNAME", "marc")
    monkeypatch.setenv("CPANEL_TOKEN", "tok")
    monkeypatch.setenv("CPANEL_BASE_URL", "https://cp.example.test:2083")


@pytest.fixture
def fresh_settings(client):
    from app.database import SessionLocal
    from app import models, https_setup
    yield
    db = SessionLocal()
    db.query(models.HttpsSettings).delete()
    db.commit()
    db.close()
    https_setup._active_domain = None


# ---------------------------------------------------------------------------
# DNS records
# ---------------------------------------------------------------------------

def test_new_name_gets_an_a_record(cpanel):
    fake, dns = cpanel
    assert dns.set_a("pantry.example.test", "192.168.60.20", None) == "example.test"
    assert fake.find("pantry.example.test.", "A")[0]["data"] == ["192.168.60.20"]


def test_existing_record_is_never_taken_over(cpanel):
    """Someone on the network can't use this page to repoint the website."""
    from app.https_setup import HttpsError
    fake, dns = cpanel
    with pytest.raises(HttpsError, match="already points to 203.0.113.10"):
        dns.set_a("www.example.test", "192.168.60.20", None)
    assert fake.find("www.example.test.", "A")[0]["data"] == ["203.0.113.10"]
    with pytest.raises(HttpsError, match="already has a CNAME"):
        dns.set_a("mail.example.test", "192.168.60.20", None)


def test_the_domain_itself_is_refused(cpanel):
    from app.https_setup import HttpsError
    fake, dns = cpanel
    with pytest.raises(HttpsError, match="not the domain itself"):
        dns.set_a("example.test", "192.168.60.20", None)
    assert fake.find("example.test.", "A")[0]["data"] == ["203.0.113.10"]


def test_its_own_record_can_be_moved(cpanel):
    fake, dns = cpanel
    dns.set_a("pantry.example.test", "192.168.60.20", None)
    dns.set_a("pantry.example.test", "192.168.60.30", "192.168.60.20")
    assert [r["data"] for r in fake.find("pantry.example.test.", "A")] == [["192.168.60.30"]]


def test_txt_record_added_and_removed(cpanel):
    fake, dns = cpanel
    dns.add_txt("example.test", "_acme-challenge.pantry.example.test", "abc")
    dns.add_txt("example.test", "_acme-challenge.pantry.example.test", "def")
    assert fake.find("_acme-challenge.pantry.example.test.", "TXT")[0]["data"] == ["abc", "def"]
    dns.remove_txt("example.test", "_acme-challenge.pantry.example.test", "abc")
    assert fake.find("_acme-challenge.pantry.example.test.", "TXT")[0]["data"] == ["def"]
    dns.remove_txt("example.test", "_acme-challenge.pantry.example.test", "def")
    assert fake.find("_acme-challenge.pantry.example.test.", "TXT") == []


def test_bad_login_and_unknown_zone_say_so(monkeypatch, cpanel):
    from app import https_setup
    fake, _ = cpanel
    bad = https_setup.CpanelDns("https://cp.example.test:2083", "marc", "wrong")
    with pytest.raises(https_setup.HttpsError, match="refused the login"):
        bad.find_zone("pantry.example.test")
    good = https_setup.CpanelDns("https://cp.example.test:2083", "marc", "tok")
    with pytest.raises(https_setup.HttpsError, match="doesn't have a DNS zone containing"):
        good.find_zone("pantry.other.test")


# ---------------------------------------------------------------------------
# Form values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("domain, lan, email, ok", [
    ("Pantry.Example.com.", "192.168.60.20", "me@example.com", True),
    ("pantry", "192.168.60.20", "me@example.com", False),
    ("pantry.example.com", "127.0.0.1", "me@example.com", False),
    ("pantry.example.com", "nas.local", "me@example.com", False),
    ("pantry.example.com", "192.168.60.20", "not-an-email", False),
    ("-bad.example.com", "192.168.60.20", "me@example.com", False),
])
def test_validation(domain, lan, email, ok):
    from app import https_setup
    if ok:
        assert https_setup.validate(domain, lan, email, 8443)[0] == "pantry.example.com"
    else:
        with pytest.raises(https_setup.HttpsError):
            https_setup.validate(domain, lan, email, 8443)


# ---------------------------------------------------------------------------
# The job and the API
# ---------------------------------------------------------------------------

def _run(https_setup, request=None):
    assert https_setup._job_lock.acquire(blocking=False)
    https_setup._progress["running"] = True
    https_setup.run_setup(None, None, request)


def test_setup_saves_only_on_success(monkeypatch, cpanel, creds, fresh_settings):
    from app import https_setup
    fake, _ = cpanel
    expires = datetime.now(timezone.utc) + timedelta(days=90)
    monkeypatch.setattr(https_setup, "_issue", lambda dns, zone, domain, email: expires)

    good = {"domain": "pantry.example.test", "lan_address": "192.168.60.20", "email": "me@example.test", "port": 8443}
    _run(https_setup, good)
    st = https_setup.status()
    assert st["state"] == "ready" and st["enabled"] and st["domain"] == "pantry.example.test"
    assert st["url"] == "https://pantry.example.test:8443" and https_setup.active_domain() == "pantry.example.test"

    # A failed attempt with another name leaves the working setup alone.
    _run(https_setup, {**good, "domain": "www.example.test"})
    st = https_setup.status()
    assert st["domain"] == "pantry.example.test" and st["state"] == "ready"
    assert "already points to" in st["last_error"]
    assert https_setup.active_domain() == "pantry.example.test"


def test_setup_without_a_login_explains_env(client, monkeypatch, fresh_settings):
    for v in ("CPANEL_USERNAME", "CPANEL_TOKEN", "CPANEL_BASE_URL"):
        monkeypatch.delenv(v, raising=False)
    st = client.get("/api/https").json()
    assert st["credentials"]["found"] is False and len(st["credentials"]["missing"]) == 3
    r = client.post("/api/https/setup", json={"domain": "pantry.example.test", "lan_address": "192.168.60.20",
                                              "email": "me@example.test", "port": 8443})
    assert r.status_code == 400 and ".env" in r.json()["detail"]
    fr = client.post("/api/https/setup", headers={"X-App-Lang": "fr"},
                     json={"domain": "pantry", "lan_address": "192.168.60.20", "email": "me@example.test"})
    assert fr.json()["detail"] == "Entrez un nom comme pantry.example.com."


def test_the_configured_name_is_an_allowed_host(client, monkeypatch, fresh_settings):
    from app import https_setup
    assert client.get("/api/version", headers={"Host": "pantry.example.test"}).status_code == 400
    monkeypatch.setattr(https_setup, "_active_domain", "pantry.example.test")
    assert client.get("/api/version", headers={"Host": "pantry.example.test:8443"}).status_code == 200


def test_renewal_is_due_in_the_last_30_days(fresh_settings):
    from app import https_setup, models
    from app.database import SessionLocal
    db = SessionLocal()
    s = https_setup._settings(db)
    s.enabled = True
    s.cert_expires_at = datetime.now(timezone.utc) + timedelta(days=60)
    db.commit()
    assert https_setup.cert_needs_renewal() is False
    s.cert_expires_at = datetime.now(timezone.utc) + timedelta(days=20)
    db.commit()
    db.close()
    assert https_setup.cert_needs_renewal() is True


def test_a_busy_port_does_not_stop_the_app(monkeypatch, tmp_path, fresh_settings):
    """uvicorn exits the process when it can't bind; for the HTTPS listener
    that must only end the listener."""
    import asyncio
    import socket
    from app import https_setup
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pantry.example.test")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "fullchain.pem", tmp_path / "privkey.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen()
    monkeypatch.setattr(https_setup, "CERT_PATH", str(cert_path))
    monkeypatch.setattr(https_setup, "KEY_PATH", str(key_path))
    monkeypatch.setattr(https_setup, "LISTEN_PORT", blocker.getsockname()[1])

    async def go():
        from fastapi import FastAPI
        await https_setup.start_server(FastAPI())
        await asyncio.sleep(0.3)
        return https_setup._server

    try:
        assert asyncio.run(go()) is None      # stopped, and we're still here
        assert "couldn't listen on port" in https_setup.status()["last_error"]
    finally:
        blocker.close()
