"""Optional HTTPS, set up from Settings -> HTTPS.

What it does, once you press "Set up HTTPS":
  1. Points a name in your domain (e.g. pantry.example.com) at this
     server's LAN address, by adding an A record through your DNS host's
     API (cPanel's UAPI).
  2. Gets a Let's Encrypt certificate for that name with the DNS-01
     challenge: a temporary TXT record, added and removed through the same
     API. Nothing has to be reachable from the internet.
  3. Serves the app over HTTPS on a second port (8443 in the container),
     next to the usual plain-HTTP 8090, and renews the certificate before
     it expires.

The DNS host login (cPanel username, address, API token) and the one name
to set up are entered in Settings -> HTTPS, which needs the settings
password (admin_lock.py), and saved in data/https/cpanel-login.json with
the token encrypted like the email password. The data/https folder is never
in a backup or served (test_tls_key_containment.py). Values in the
environment (CPANEL_USERNAME, CPANEL_TOKEN, CPANEL_BASE_URL, PANTRY_DOMAIN,
from .env) take precedence over the saved ones, field by field.

Limits that keep this page from reaching the rest of the domain:
  - The only name it will ever create or change is the saved one
    (PANTRY_DOMAIN). Changing it is a settings change, behind the password.
  - It never touches the domain's own name (the zone apex), and never
    changes an existing record for that name unless it created it.
  - The name can only point at a private network address (home LAN or
    Tailscale), never at a server on the internet.
  - At most MAX_REQUESTS_PER_WEEK certificate requests in 7 days, so the
    page can't be used to use up Let's Encrypt's limits for the domain.
"""
import asyncio
import base64
import contextlib
import ipaddress
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import requests

from .database import DATA_DIR, SessionLocal
from . import models
from . import crypto
from .logging_setup import get_logger

log = get_logger("https")

HTTPS_DIR = os.path.join(DATA_DIR, "https")
ACCOUNT_KEY_PATH = os.path.join(HTTPS_DIR, "account.key")
ACCOUNT_INFO_PATH = os.path.join(HTTPS_DIR, "account.json")
CERT_PATH = os.path.join(HTTPS_DIR, "fullchain.pem")
KEY_PATH = os.path.join(HTTPS_DIR, "privkey.pem")

# The port inside the container. The compose file maps it to whatever the
# devices use (8443 by default); that public number is a setting, only
# used to build the link.
LISTEN_PORT = int(os.environ.get("RECIPE_APP_HTTPS_PORT", "8443"))

LETS_ENCRYPT = "https://acme-v02.api.letsencrypt.org/directory"
ACME_DIRECTORY = os.environ.get("RECIPE_APP_ACME_DIRECTORY", LETS_ENCRYPT)
# Only for testing against a private ACME server (Pebble).
ACME_CA_BUNDLE = os.environ.get("RECIPE_APP_ACME_CA_BUNDLE") or True

RENEW_WITHIN = timedelta(days=30)
DNS_WAIT_SECONDS = 180
TXT_TTL = 300
A_TTL = 3600

CREDENTIAL_VARS = ("CPANEL_USERNAME", "CPANEL_TOKEN", "CPANEL_BASE_URL")
# The one name this page may set up. Kept with the login, outside the app,
# so someone using the page can't have the token create other names.
DOMAIN_VAR = "PANTRY_DOMAIN"
REQUIRED_VARS = CREDENTIAL_VARS + (DOMAIN_VAR,)

# Let's Encrypt issues at most 5 certificates for the same name per week.
# Requests are counted here so the page stops before that, with a date.
MAX_REQUESTS_PER_WEEK = 5
REQUESTS_PATH = os.path.join(HTTPS_DIR, "requests.json")

# Where a home network or VPN puts its machines: the private ranges, plus
# 100.64.0.0/10 (Tailscale and other carrier-grade NAT). Listed rather than
# ipaddress's is_private, which also counts documentation ranges and the
# like.
NETWORK_RANGES = tuple(ipaddress.IPv4Network(n) for n in
                       ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"))


class HttpsError(Exception):
    """A failure with a message meant for the Settings page."""


# ---------------------------------------------------------------------------
# Settings and progress
# ---------------------------------------------------------------------------

_job_lock = threading.Lock()
_progress = {"running": False, "step": ""}
# Host name the app answers to over HTTPS; read by the host check in main.py.
_active_domain: str | None = None


def active_domain() -> str | None:
    return _active_domain


def _set_step(text: str):
    _progress["step"] = text
    log.info("HTTPS: %s", text)


# ---------------------------------------------------------------------------
# The cPanel login and the name: saved by Settings -> HTTPS, or from .env
# ---------------------------------------------------------------------------
LOGIN_PATH = os.path.join(HTTPS_DIR, "cpanel-login.json")
_FIELDS = {"username": "CPANEL_USERNAME", "base_url": "CPANEL_BASE_URL", "domain": DOMAIN_VAR}


def _stored_login() -> dict:
    try:
        with open(LOGIN_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_login(d: dict):
    os.makedirs(HTTPS_DIR, exist_ok=True)
    _write_private(LOGIN_PATH, json.dumps(d).encode("utf-8"))


def _value(field: str) -> tuple[str, bool]:
    """A login field and whether it comes from .env (which wins)."""
    env = os.environ.get(_FIELDS[field], "").strip()
    return (env, True) if env else (str(_stored_login().get(field) or "").strip(), False)


def _normal_domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    return value if _DOMAIN_RE.match(value) and len(value) <= 253 else ""


def allowed_domain() -> str:
    """The one name this page may set up, normalised; empty if unset or
    not a valid name."""
    return _normal_domain(_value("domain")[0])


def _token() -> tuple[str, bool]:
    env = os.environ.get("CPANEL_TOKEN", "").strip()
    if env:
        return env, True
    enc = _stored_login().get("token_encrypted")
    if not enc:
        return "", False
    try:
        return crypto.decrypt_secret(enc), False
    except Exception as e:
        log.warning("The saved cPanel token can't be read: %s", e)
        raise HttpsError("The saved cPanel token can't be read (the encryption key changed or is missing). "
                         "Enter it again in Settings → HTTPS.")


def credentials_status() -> dict:
    username, user_env = _value("username")
    base, base_env = _value("base_url")
    domain_env = _value("domain")[1]
    token_env = bool(os.environ.get("CPANEL_TOKEN", "").strip())
    token_set = token_env or bool(_stored_login().get("token_encrypted"))
    missing = [var for var, ok in (("CPANEL_USERNAME", username), ("CPANEL_TOKEN", token_set),
                                   ("CPANEL_BASE_URL", base), (DOMAIN_VAR, allowed_domain())) if not ok]
    host = re.sub(r"^https?://", "", base).split("/")[0] if base else ""
    from_env = [f for f, on in (("username", user_env), ("base_url", base_env), ("token", token_env),
                                ("domain", domain_env)) if on]
    return {"found": not missing, "missing": missing, "provider": "cPanel", "host": host,
            "domain": allowed_domain(), "username": username, "base_url": base, "token_set": token_set,
            "from_env": from_env}


def save_login(username: str, base_url: str, domain: str, token: str | None):
    """Settings -> HTTPS: saves the cPanel login and the name. Fields set in
    .env are left to .env. Changing the username or address without entering
    the token again clears the saved token: otherwise the page could send
    the token to whatever address someone typed in."""
    if _progress["running"]:
        raise HttpsError("HTTPS setup is running. Try again when it has finished.")
    old = _stored_login()
    new = dict(old)
    username, base_url, token = username.strip(), base_url.strip().rstrip("/"), (token or "").strip()
    if not _value("username")[1]:
        if not username:
            raise HttpsError("Enter the cPanel username.")
        new["username"] = username
    if not _value("base_url")[1]:
        if not re.match(r"^https://[^\s/:]+(:\d{1,5})?$", base_url):
            raise HttpsError("Enter the cPanel address as https://host:port, e.g. https://cpanel.example.com:2083.")
        new["base_url"] = base_url
    if not _value("domain")[1]:
        d = _normal_domain(domain)
        if not d:
            raise HttpsError("Enter the name to use, e.g. pantry.example.com.")
        new["domain"] = d
    if (new.get("username") != old.get("username") or new.get("base_url") != old.get("base_url")) and not token:
        new.pop("token_encrypted", None)
    if token:
        if not crypto.encryption_configured():
            if crypto.key_source() is None:
                crypto.generate_key_file()      # same key file the email password uses
            else:
                raise HttpsError("The token wasn't saved: the encryption key is damaged. "
                                 "Settings → Email ingest explains how to fix it.")
        new["token_encrypted"] = crypto.encrypt_secret(token)
    _write_login(new)
    log.info("HTTPS: cPanel login saved (user %s, %s, name %s, token %s)", new.get("username"),
             new.get("base_url"), new.get("domain"), "set" if new.get("token_encrypted") else "not set")


def forget_token():
    """Settings password reset: the saved token goes; the rest stays."""
    d = _stored_login()
    if d.pop("token_encrypted", None) is not None:
        _write_login(d)


def clear_login():
    with contextlib.suppress(FileNotFoundError):
        os.remove(LOGIN_PATH)


def _settings(db) -> models.HttpsSettings:
    s = db.get(models.HttpsSettings, 1)
    if s is None:
        s = models.HttpsSettings(id=1)
        db.add(s)
        db.commit()
        db.refresh(s)
    return s


def status() -> dict:
    db = SessionLocal()
    try:
        s = _settings(db)
        return {
            "credentials": credentials_status(),
            "enabled": bool(s.enabled),
            "domain": s.domain or "",
            "lan_address": s.lan_address or "",
            "email": s.email or "",
            "port": s.public_port or 8443,
            "state": "working" if _progress["running"] else (s.state or "off"),
            "step": _progress["step"] if _progress["running"] else "",
            "last_error": s.last_error,
            "cert_expires_at": _utc(s.cert_expires_at).isoformat() if s.cert_expires_at else None,
            "listening": _server is not None,
            "url": _url(s) if s.enabled and s.cert_expires_at else None,
        }
    finally:
        db.close()


def _utc(dt: datetime) -> datetime:
    # SQLite hands datetimes back without a zone; they're stored in UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _url(s) -> str:
    port = s.public_port or 8443
    return f"https://{s.domain}" + ("" if port == 443 else f":{port}")


_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_DOMAIN_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})+$")
_EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def _not_pinned_error(domain: str, pinned: str) -> "HttpsError":
    if not pinned:
        return HttpsError("Enter the name to use (e.g. pantry.example.com) with the cPanel login above, "
                          "then save.")
    return HttpsError(f"This page can only set up {pinned}, the saved name. To use another name, "
                      "change it with the cPanel login above.")


def is_network_address(ip: ipaddress.IPv4Address) -> bool:
    """A home-network or VPN address: private ranges and Tailscale's."""
    return any(ip in net for net in NETWORK_RANGES)


def validate(domain: str, lan_address: str, email: str, port: int,
             pinned: str | None = None) -> tuple[str, str, str, int]:
    """pinned: the name allowed by PANTRY_DOMAIN (None reads it now). An
    empty domain means that name."""
    pinned = allowed_domain() if pinned is None else pinned
    domain = (domain or "").strip().lower().rstrip(".") or pinned
    lan_address = (lan_address or "").strip()
    email = (email or "").strip()
    if not _DOMAIN_RE.match(domain) or len(domain) > 253:
        raise HttpsError("Enter a name like pantry.example.com.")
    if domain != pinned:
        raise _not_pinned_error(domain, pinned)
    try:
        ip = ipaddress.IPv4Address(lan_address)
    except ValueError:
        raise HttpsError("Enter this server's IPv4 address on your network, e.g. 192.168.1.20.")
    if not is_network_address(ip):
        raise HttpsError("Enter this server's address on your home network or VPN, e.g. 192.168.1.20. "
                         "Internet addresses aren't accepted.")
    if not _EMAIL_RE.match(email):
        raise HttpsError("Enter an email address for Let's Encrypt.")
    if not 1 <= int(port) <= 65535:
        raise HttpsError("The port must be between 1 and 65535.")
    return domain, str(ip), email, int(port)


# ---------------------------------------------------------------------------
# cPanel DNS (UAPI DNS::parse_zone / DNS::mass_edit_zone, as lego uses)
# ---------------------------------------------------------------------------

class CpanelDns:
    def __init__(self, base_url: str, username: str, token: str, timeout: float = 20):
        self.base = base_url.rstrip("/")
        self.auth = f"cpanel {username}:{token}"
        self.timeout = timeout

    def _call(self, func: str, **params):
        url = f"{self.base}/execute/DNS/{func}"
        try:
            r = requests.get(url, params=params, timeout=self.timeout,
                             headers={"Authorization": self.auth, "Accept": "application/json"})
        except requests.RequestException as e:
            raise HttpsError(f"Couldn't reach cPanel at {self.base}: {e}")
        if r.status_code in (401, 403):
            raise HttpsError("cPanel refused the login. Check the cPanel username and API token in Settings → HTTPS.")
        if r.status_code != 200:
            raise HttpsError(f"cPanel answered with HTTP {r.status_code}.")
        try:
            body = r.json()
        except ValueError:
            raise HttpsError("cPanel's answer wasn't JSON. Check the cPanel address (e.g. https://host:2083).")
        if not body.get("status"):
            errors = "; ".join(body.get("errors") or []) or "unknown error"
            raise HttpsError(f"cPanel refused the request: {errors}")
        return body.get("data")

    def records(self, zone: str) -> list[dict]:
        out = []
        for rec in self._call("parse_zone", zone=zone) or []:
            if rec.get("type") != "record":
                continue
            out.append({
                "line_index": rec.get("line_index"),
                "record_type": rec.get("record_type"),
                "name": base64.b64decode(rec.get("dname_b64") or "").decode("utf-8", "replace"),
                "data": [base64.b64decode(d).decode("utf-8", "replace") for d in rec.get("data_b64") or []],
                "ttl": rec.get("ttl"),
            })
        return out

    def find_zone(self, fqdn: str) -> str:
        """The account's zone holding fqdn: the longest suffix cPanel accepts.
        Never the name itself (the app doesn't manage a zone apex)."""
        labels = fqdn.split(".")
        for i in range(1, len(labels) - 1):
            zone = ".".join(labels[i:])
            try:
                self._call("parse_zone", zone=zone)
                return zone
            except HttpsError as e:
                if "login" in str(e) or "reach" in str(e) or "JSON" in str(e):
                    raise
        try:
            self._call("parse_zone", zone=fqdn)
        except HttpsError:
            raise HttpsError(f"This cPanel account doesn't have a DNS zone containing {fqdn}.")
        raise HttpsError(f"Use a name under {fqdn}, like pantry.{fqdn}, not the domain itself.")

    @staticmethod
    def _serial(zone: str, recs: list[dict]) -> int:
        for r in recs:
            if r["record_type"] == "SOA" and r["name"].rstrip(".") == zone and len(r["data"]) > 2:
                return int(r["data"][2])
        raise HttpsError("Couldn't read the zone's serial number from cPanel.")

    def _edit(self, zone: str, recs: list[dict], action: str, value):
        payload = value if isinstance(value, str) else json.dumps(value)
        self._call("mass_edit_zone", zone=zone, serial=str(self._serial(zone, recs)), **{action: payload})

    def set_a(self, fqdn: str, ip: str, previous_ip: str | None) -> str:
        """Point fqdn at ip. Returns the zone. Refuses to change a record the
        app didn't create (its value isn't previous_ip)."""
        zone = self.find_zone(fqdn)
        recs = self.records(zone)
        name = fqdn + "."
        same = [r for r in recs if r["name"] == name]
        others = [r for r in same if r["record_type"] in ("CNAME", "AAAA")]
        if others:
            raise HttpsError(f"{fqdn} already has a {others[0]['record_type']} record in cPanel. "
                             "Choose a name that isn't in use, or remove that record first.")
        a = [r for r in same if r["record_type"] == "A"]
        if len(a) > 1:
            raise HttpsError(f"{fqdn} has more than one A record in cPanel. Leave one, or choose another name.")
        if a:
            current = a[0]["data"][0] if a[0]["data"] else ""
            if current == ip:
                return zone
            if current != previous_ip:
                raise HttpsError(f"{fqdn} already points to {current}. Choose a name that isn't in use, "
                                 "or change that record in cPanel yourself.")
            self._edit(zone, recs, "edit", {"line_index": a[0]["line_index"], "dname": name, "ttl": A_TTL,
                                             "record_type": "A", "data": [ip]})
            return zone
        self._edit(zone, recs, "add", {"dname": name, "ttl": A_TTL, "record_type": "A", "data": [ip]})
        return zone

    def remove_a(self, fqdn: str, ip: str) -> bool:
        """Removes fqdn's A record, only if it's the single record for that
        name and still holds ip (the value this app wrote). Returns whether
        it removed anything."""
        zone = self.find_zone(fqdn)
        recs = self.records(zone)
        name = fqdn + "."
        same = [r for r in recs if r["name"] == name]
        if len(same) != 1 or same[0]["record_type"] != "A" or same[0]["data"] != [ip]:
            return False
        self._edit(zone, recs, "remove", str(same[0]["line_index"]))
        return True

    def add_txt(self, zone: str, fqdn: str, value: str):
        recs = self.records(zone)
        name = fqdn + "."
        for r in recs:
            if r["record_type"] == "TXT" and r["name"] == name:
                if value in r["data"]:
                    return
                self._edit(zone, recs, "edit", {"line_index": r["line_index"], "dname": name, "ttl": TXT_TTL,
                                                 "record_type": "TXT", "data": r["data"] + [value]})
                return
        self._edit(zone, recs, "add", {"dname": name, "ttl": TXT_TTL, "record_type": "TXT", "data": [value]})

    def remove_txt(self, zone: str, fqdn: str, value: str):
        recs = self.records(zone)
        name = fqdn + "."
        for r in recs:
            if r["record_type"] == "TXT" and r["name"] == name and value in r["data"]:
                rest = [d for d in r["data"] if d != value]
                if rest:
                    self._edit(zone, recs, "edit", {"line_index": r["line_index"], "dname": name, "ttl": TXT_TTL,
                                                     "record_type": "TXT", "data": rest})
                else:
                    self._edit(zone, recs, "remove", str(r["line_index"]))
                return


_MISSING_NAMES = {"CPANEL_USERNAME": "cPanel username", "CPANEL_TOKEN": "API token",
                  "CPANEL_BASE_URL": "cPanel address", DOMAIN_VAR: "name"}


def missing_settings_error(missing: list[str]) -> "HttpsError":
    return HttpsError("Settings → HTTPS still needs: " + ", ".join(_MISSING_NAMES.get(m, m) for m in missing) +
                      ". Fill them in with the cPanel login and save.")


def _dns_client() -> CpanelDns:
    cred = credentials_status()
    if not cred["found"]:
        raise missing_settings_error(cred["missing"])
    return CpanelDns(_value("base_url")[0], _value("username")[0], _token()[0])


# ---------------------------------------------------------------------------
# Waiting for the TXT record
# ---------------------------------------------------------------------------

def _authoritative_servers(zone: str) -> list[tuple[str, int]]:
    """The zone's own name servers, so the check doesn't depend on a cache.
    RECIPE_APP_DNS_CHECK_SERVERS (host:port, comma-separated) overrides it;
    the tests point it at Pebble's DNS server."""
    override = os.environ.get("RECIPE_APP_DNS_CHECK_SERVERS", "").strip()
    if override:
        out = []
        for item in override.split(","):
            host, _, port = item.strip().rpartition(":")
            out.append((host or item.strip(), int(port) if port.isdigit() else 53))
        return out
    import dns.resolver
    servers = []
    for ns in dns.resolver.resolve(zone, "NS"):
        try:
            for a in dns.resolver.resolve(str(ns.target), "A"):
                servers.append((a.address, 53))
        except Exception:
            log.debug("NS %s has no A record", ns.target, exc_info=True)
    return servers or [("1.1.1.1", 53), ("8.8.8.8", 53)]


def _wait_for_txt(zone: str, fqdn: str, value: str, timeout: float = DNS_WAIT_SECONDS):
    import dns.resolver
    try:
        servers = _authoritative_servers(zone)
    except Exception:
        log.warning("Couldn't look up the name servers for %s; using public resolvers", zone, exc_info=True)
        servers = [("1.1.1.1", 53), ("8.8.8.8", 53)]
    deadline = time.monotonic() + timeout
    while True:
        seen_everywhere = True
        for host, port in servers:
            r = dns.resolver.Resolver(configure=False)
            r.nameservers = [host]
            r.port = port
            r.lifetime = 5
            try:
                answer = r.resolve(fqdn, "TXT", raise_on_no_answer=False)
                texts = {b"".join(rr.strings).decode() for rr in answer} if answer.rrset else set()
            except Exception:
                log.debug("TXT lookup for %s at %s:%s failed", fqdn, host, port, exc_info=True)
                texts = set()
            if value not in texts:
                seen_everywhere = False
                break
        if seen_everywhere:
            return
        if time.monotonic() > deadline:
            raise HttpsError("The DNS record didn't appear on your domain's name servers within "
                             f"{int(timeout // 60)} minutes. Try again in a few minutes.")
        time.sleep(5)


# ---------------------------------------------------------------------------
# Let's Encrypt (ACME, via the acme library certbot is built on)
# ---------------------------------------------------------------------------

def _write_private(path: str, data: bytes):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _account_key():
    import josepy as jose
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    if os.path.exists(ACCOUNT_KEY_PATH):
        with open(ACCOUNT_KEY_PATH, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _write_private(ACCOUNT_KEY_PATH, key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return jose.JWKRSA(key=key)


def _acme_client(email: str):
    from acme import client, errors, messages
    key = _account_key()
    net = client.ClientNetwork(key, user_agent="open-the-pantry", verify_ssl=ACME_CA_BUNDLE)
    directory = client.ClientV2.get_directory(ACME_DIRECTORY, net)
    acme = client.ClientV2(directory, net=net)
    try:
        acme.new_account(messages.NewRegistration.from_data(email=email, terms_of_service_agreed=True))
    except errors.ConflictError as e:
        # The key already has an account: use it.
        net.account = messages.RegistrationResource(uri=e.location, body=messages.Registration())
    return acme, key


def _recent_requests(now: datetime) -> list[datetime]:
    try:
        with open(REQUESTS_PATH, encoding="utf-8") as f:
            stamps = [datetime.fromisoformat(t) for t in json.load(f)]
    except (OSError, ValueError, TypeError):
        return []
    return sorted(t for t in stamps if now - t < timedelta(days=7))


def _check_request_budget():
    now = datetime.now(timezone.utc)
    recent = _recent_requests(now)
    if len(recent) >= MAX_REQUESTS_PER_WEEK:
        again = recent[len(recent) - MAX_REQUESTS_PER_WEEK] + timedelta(days=7)
        raise HttpsError(f"{len(recent)} certificates were requested in the last 7 days; Let's Encrypt allows "
                         f"{MAX_REQUESTS_PER_WEEK} a week for the same name. Try again after "
                         f"{again.strftime('%Y-%m-%d %H:%M')} UTC.")


def _count_request():
    now = datetime.now(timezone.utc)
    stamps = _recent_requests(now) + [now]
    os.makedirs(HTTPS_DIR, exist_ok=True)
    with open(REQUESTS_PATH, "w", encoding="utf-8") as f:
        json.dump([t.isoformat() for t in stamps], f)


def _issue(dns: CpanelDns, zone: str, domain: str, email: str) -> datetime:
    from acme import challenges, crypto_util, messages
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    _set_step("Asking Let's Encrypt for a certificate…")
    acme, account_key = _acme_client(email)
    cert_key = ec.generate_private_key(ec.SECP256R1())
    key_pem = cert_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
    _count_request()
    order = acme.new_order(crypto_util.make_csr(key_pem, [domain]))

    added = []
    try:
        pending = []
        for authz in order.authorizations:
            # Let's Encrypt reuses a recent successful validation for the
            # same account; there's nothing to answer then.
            if authz.body.status == messages.STATUS_VALID:
                continue
            chall = next((c for c in authz.body.challenges if isinstance(c.chall, challenges.DNS01)), None)
            if chall is None:
                raise HttpsError("Let's Encrypt didn't offer a DNS challenge.")
            name = chall.chall.validation_domain_name(authz.body.identifier.value)
            value = chall.chall.validation(account_key)
            _set_step("Adding the temporary DNS record…")
            dns.add_txt(zone, name, value)
            added.append((name, value))
            pending.append((chall, name, value))
        for chall, name, value in pending:
            _set_step("Waiting for the DNS record to appear…")
            _wait_for_txt(zone, name, value)
            acme.answer_challenge(chall, chall.chall.response(account_key))
        _set_step("Let's Encrypt is checking the record…")
        try:
            finished = acme.poll_and_finalize(order, deadline=datetime.now() + timedelta(seconds=180))
        except Exception as e:
            log.warning("Let's Encrypt order failed", exc_info=True)
            raise HttpsError(f"Let's Encrypt didn't issue the certificate: {e}")
    finally:
        for name, value in added:
            try:
                dns.remove_txt(zone, name, value)
            except Exception:
                log.warning("Couldn't remove the temporary record %s", name, exc_info=True)

    chain = finished.fullchain_pem.encode() if isinstance(finished.fullchain_pem, str) else finished.fullchain_pem
    expires = x509.load_pem_x509_certificate(chain).not_valid_after_utc
    _write_private(KEY_PATH, key_pem)
    _write_private(CERT_PATH, chain)
    return expires


def run_setup(main_loop=None, app=None, request: dict | None = None):
    """The whole job, in a worker thread. request holds the values from the
    form (domain, lan_address, email, port); None renews with the saved
    ones. The saved settings change only when it succeeds, so a failed
    attempt with a new name leaves a working setup as it was. Progress goes
    to _progress; errors to last_error."""
    global _active_domain
    db = SessionLocal()
    try:
        s = _settings(db)
        if request is None:
            request = {"domain": s.domain, "lan_address": s.lan_address, "email": s.email, "port": s.public_port}
        domain, lan, email = request["domain"], request["lan_address"], request["email"]
        # Only the record this app wrote for this same name may be changed.
        previous_a = s.a_record_value if domain == s.domain else None
        old_name, old_a = s.domain, s.a_record_value
        try:
            os.makedirs(HTTPS_DIR, exist_ok=True)
            # Checked here too, not only in the API: a renewal uses the saved
            # name, which may predate PANTRY_DOMAIN or differ from it now.
            pinned = allowed_domain()
            if domain != pinned:
                raise _not_pinned_error(domain, pinned)
            _check_request_budget()
            _set_step("Checking the cPanel login…")
            dns = _dns_client()
            _set_step(f"Pointing {domain} at {lan}…")
            zone = dns.set_a(domain, lan, previous_a)
            expires = _issue(dns, zone, domain, email)
            if old_name and old_name != domain and old_a:
                # The previous name's record, if it's still the one this app
                # wrote. Best effort: a leftover record is harmless.
                try:
                    if dns.remove_a(old_name, old_a):
                        log.info("HTTPS: removed the old record for %s", old_name)
                except Exception:
                    log.warning("HTTPS: couldn't remove the old record for %s", old_name, exc_info=True)
            s.domain, s.lan_address, s.email, s.public_port = domain, lan, email, request["port"]
            s.a_record_value = lan
            s.cert_expires_at = expires
            s.state = "ready"
            s.last_error = None
            s.enabled = True
            db.commit()
            _active_domain = domain
            _set_step("Starting HTTPS…")
            if main_loop is not None and app is not None:
                asyncio.run_coroutine_threadsafe(start_server(app), main_loop).result(timeout=30)
            log.info("HTTPS: certificate for %s valid until %s", domain, expires.isoformat())
        except Exception as e:
            if isinstance(e, HttpsError):
                message = str(e)
                log.warning("HTTPS setup failed: %s", e)
            else:
                message = f"Unexpected error: {e}"
                log.exception("HTTPS setup failed")
            db.rollback()
            s = _settings(db)
            s.last_error = message
            if not s.enabled:
                s.state = "error"
            db.commit()
    finally:
        db.close()
        _progress["running"] = False
        _progress["step"] = ""
        _job_lock.release()


def start_job(main_loop, app, request: dict | None = None) -> bool:
    """Starts run_setup in a thread unless one is already running."""
    if not _job_lock.acquire(blocking=False):
        return False
    _progress["running"] = True
    _progress["step"] = "Starting…"
    threading.Thread(target=run_setup, args=(main_loop, app, request), name="https-setup", daemon=True).start()
    return True


def turn_off():
    global _active_domain
    db = SessionLocal()
    try:
        s = _settings(db)
        s.enabled = False
        s.state = "off"
        s.last_error = None
        db.commit()
    finally:
        db.close()
    _active_domain = None


@contextlib.contextmanager
def reserved():
    """Holds the setup-job lock for a whole operation: no setup or renewal
    can start until it ends (start_job is refused), and it is refused itself
    if one is running. The settings-password reset runs inside this, so it
    can't be cut in half by a setup that starts between its check and its
    clearing."""
    if not _job_lock.acquire(blocking=False):
        raise HttpsError("HTTPS setup is running. Try again when it has finished.")
    try:
        yield
    finally:
        _job_lock.release()


def forget_certificate():
    """After the settings password is reset (admin_lock.py): HTTPS off, and
    the certificate, its key and the Let's Encrypt account key deleted, so
    HTTPS has to be set up again. Kept: the name, address and email (they
    prefill the form), the A record value the app last wrote (so it can
    still update its own record), and the weekly request count (Let's
    Encrypt's limit doesn't reset). Call stop_server() first, and only
    inside reserved()."""
    if not _job_lock.locked():
        raise RuntimeError("forget_certificate() must run inside reserved()")
    turn_off()
    for path in (KEY_PATH, CERT_PATH, ACCOUNT_KEY_PATH):
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
    db = SessionLocal()
    try:
        _settings(db).cert_expires_at = None
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# The HTTPS listener: a second uvicorn server for the same app
# ---------------------------------------------------------------------------

_server = None
_server_task: asyncio.Task | None = None


def _make_server(app):
    import uvicorn

    class Server(uvicorn.Server):
        # The main server owns the process's signal handling.
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    config = uvicorn.Config(app, host="0.0.0.0", port=LISTEN_PORT, ssl_certfile=CERT_PATH,
                            ssl_keyfile=KEY_PATH, lifespan="off", log_level="warning",
                            server_header=False)
    return Server(config)


async def _serve(server):
    global _server, _server_task
    try:
        await server.serve()
    except SystemExit:
        # uvicorn exits the process when it can't bind; here that must
        # only end this listener, never the app.
        log.error("HTTPS: couldn't listen on port %s (in use?)", LISTEN_PORT)
        _record_listen_error()
    except Exception:
        log.exception("HTTPS listener stopped")
    finally:
        if _server is server:
            _server = None
            _server_task = None


def _record_listen_error():
    db = SessionLocal()
    try:
        s = _settings(db)
        s.state = "error"
        s.last_error = f"The certificate is ready, but the app couldn't listen on port {LISTEN_PORT}."
        db.commit()
    finally:
        db.close()


def _clear_listen_error():
    db = SessionLocal()
    try:
        s = _settings(db)
        if s.last_error and "couldn't listen on port" in s.last_error:
            s.last_error = None
            s.state = "ready"
            db.commit()
    finally:
        db.close()


async def start_server(app):
    """Starts the HTTPS listener, or loads a renewed certificate into the
    running one (new connections get it; nothing restarts)."""
    global _server, _server_task
    if not (os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH)):
        return
    if _server is not None and _server.config.loaded and _server.config.ssl is not None:
        _server.config.ssl.load_cert_chain(CERT_PATH, KEY_PATH)
        log.info("HTTPS: loaded the renewed certificate")
        return
    server = _make_server(app)
    _server = server
    _server_task = asyncio.create_task(_serve(server))
    # Give it a moment to bind, so a port problem shows up in the status.
    for _ in range(50):
        if server.started or _server is not server:
            break
        await asyncio.sleep(0.1)
    if server.started:
        _clear_listen_error()


async def stop_server():
    global _server, _server_task
    server, task = _server, _server_task
    _server, _server_task = None, None
    if server is not None:
        server.should_exit = True
    if task is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=10)


async def startup(app):
    """At app start: serve HTTPS if it was set up, and remember the name."""
    global _active_domain
    db = SessionLocal()
    try:
        s = _settings(db)
        enabled, domain = s.enabled, s.domain
    finally:
        db.close()
    if enabled and domain:
        _active_domain = domain
        await start_server(app)


def cert_needs_renewal() -> bool:
    db = SessionLocal()
    try:
        s = _settings(db)
        if not s.enabled or not s.cert_expires_at:
            return False
        return _utc(s.cert_expires_at) - datetime.now(timezone.utc) < RENEW_WITHIN
    finally:
        db.close()


async def renewal_loop(app):
    """Checks twice a day; renews in the last 30 days of the certificate."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            if cert_needs_renewal():
                cred = credentials_status()
                if cred["found"]:
                    log.info("HTTPS: renewing the certificate")
                    start_job(loop, app)
                else:
                    log.warning("HTTPS: the certificate needs renewing, but .env is missing %s",
                                ", ".join(cred["missing"]))
        except Exception:
            log.exception("HTTPS renewal check failed")
        await asyncio.sleep(12 * 3600)
