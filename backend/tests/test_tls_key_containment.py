"""The HTTPS private keys never leave the server.

Settings -> HTTPS has no login (by design), so anyone on the network can
point PANTRY_DOMAIN at another LAN address. That is only a nuisance (a
certificate error) because the certificate's private key stays in
data/https/ on the server: the other machine has the name but no valid
certificate. These tests keep it that way. If a backup or a web path ever
exposes the key, repointing the name becomes impersonation.
"""
import io
import os
import zipfile

import pytest

MARKER = b"TEST-PRIVATE-KEY-MATERIAL-7f3c9a"


@pytest.fixture
def planted_keys(client):
    """Puts recognisable key files where the app keeps the real ones, and
    restores whatever was there afterwards."""
    from app import https_setup

    os.makedirs(https_setup.HTTPS_DIR, exist_ok=True)
    paths = [https_setup.KEY_PATH, https_setup.ACCOUNT_KEY_PATH, https_setup.LOGIN_PATH]
    saved = {p: open(p, "rb").read() if os.path.exists(p) else None for p in paths}
    for p in paths:
        with open(p, "wb") as f:
            f.write(b"-----BEGIN PRIVATE KEY-----\n" + MARKER + b"\n-----END PRIVATE KEY-----\n")
    try:
        yield paths
    finally:
        for p, data in saved.items():
            if data is None:
                os.remove(p)
            else:
                with open(p, "wb") as f:
                    f.write(data)


def _zip_leaks(content: bytes) -> list[str]:
    leaks = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        for name in z.namelist():
            if "privkey" in name or "account.key" in name or "cpanel-login" in name or name.startswith("https/"):
                leaks.append(name)
            elif MARKER in z.read(name):
                leaks.append(name)
    return leaks


@pytest.mark.parametrize("path", ["/api/backup/database.zip", "/api/backup/pdfs.zip"])
def test_backups_do_not_contain_the_tls_keys(client, sample_recipe_payload, planted_keys, path):
    client.post("/api/recipes", json={**sample_recipe_payload, "title": "TLS key containment"})
    r = client.get(path)
    assert r.status_code == 200
    assert _zip_leaks(r.content) == []


@pytest.mark.parametrize("path", [
    "/https/privkey.pem",
    "/data/https/privkey.pem",
    "/https/account.key",
    "/https/cpanel-login.json",
    "/uploads/../https/cpanel-login.json",
    "/uploads/../https/privkey.pem",
    "/uploads/%2e%2e/https/privkey.pem",
    "/uploads/..%2fhttps%2fprivkey.pem",
    "/uploads/%2e%2e%2fhttps%2fprivkey.pem",
    "/tmp-preview/../https/privkey.pem",
    "/tmp-preview/%2e%2e/https/privkey.pem",
    "/tmp-preview/..%2fhttps%2fprivkey.pem",
    "/api/https/privkey.pem",
    "/api/https/key",
])
def test_no_web_path_serves_the_tls_keys(client, planted_keys, path):
    r = client.get(path)
    assert MARKER not in r.content, f"{path} returned the private key (HTTP {r.status_code})"
