"""Outgoing TLS refuses chains through known compromised CA keys (app/tls.py;
the eMudhra intermediates of October 2026), after the usual verification.

A local CA, intermediate and server certificate stand in for the real
chain: the intermediate's key is added to the distrusted list for the test,
and the same connection is made with and without it there."""
import datetime
import http.server
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject_key, subject, issuer_key, issuer, ca, host=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    b = (x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(_name(issuer))
         .public_key(subject_key.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
         .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(subject_key.public_key()), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), critical=False))
    if ca:
        b = b.add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                          content_commitment=False, key_encipherment=False, data_encipherment=False,
                                          key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
    else:
        b = (b.add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
             .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False))
    return b.sign(issuer_key, hashes.SHA256())


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    d = tmp_path_factory.mktemp("pki")
    root_key, int_key, leaf_key = (ec.generate_private_key(ec.SECP384R1()) for _ in range(3))
    root = _cert(root_key, "Test Root", root_key, "Test Root", True)
    inter = _cert(int_key, "Test Intermediate", root_key, "Test Root", True)
    leaf = _cert(leaf_key, "localhost", int_key, "Test Intermediate", False, host="localhost")
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM)   # noqa: E731
    (d / "root.pem").write_bytes(pem(root))
    (d / "chain.pem").write_bytes(pem(leaf) + pem(inter))
    (d / "key.pem").write_bytes(leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
    from app import tls
    return {"dir": d, "intermediate_spki": tls.spki_sha256(inter.public_bytes(serialization.Encoding.DER))}


@pytest.fixture(scope="module")
def server(pki):
    """An HTTPS server on localhost presenting leaf + intermediate."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki["dir"] / "chain.pem", pki["dir"] / "key.pem")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()


def _connect(ctx, port):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname="localhost") as s:
            return s.version()


def test_python_can_check_the_chain():
    """Python 3.13 (0128): without get_verified_chain only the root list
    protects these connections."""
    from app import tls
    assert tls.CAN_CHECK_CHAIN


def test_the_published_list():
    from app import tls
    assert len(tls.DISTRUSTED_SPKI_SHA256) == 8
    assert all(len(h) == 64 and int(h, 16) >= 0 for h in tls.DISTRUSTED_SPKI_SHA256)


def test_a_chain_through_a_distrusted_key_is_refused(pki, server, monkeypatch):
    from app import tls

    def ctx():
        c = tls.client_context(load_default_certs=False)
        c.load_verify_locations(pki["dir"] / "root.pem")
        return c
    assert _connect(ctx(), server)                                   # verifies, not distrusted: connects
    monkeypatch.setitem(tls.DISTRUSTED_SPKI_SHA256, pki["intermediate_spki"], "Test Intermediate")
    with pytest.raises(tls.DistrustedCertificateError, match="Test Intermediate"):
        _connect(ctx(), server)
    # The usual verification error class, so callers' "certificate problem"
    # handling covers it (email_client.probe_port -> "cert_error").
    assert issubclass(tls.DistrustedCertificateError, ssl.SSLCertVerificationError)


def test_requests_through_the_guarded_adapter(pki, server, monkeypatch):
    from app import tls
    url = f"https://localhost:{server}/"
    root = str(pki["dir"] / "root.pem")
    assert tls.get(url, timeout=5, verify=root).text == "ok"
    # Each bundle gets its own guarded context (verify=path, as from
    # REQUESTS_CA_BUNDLE); verify=True takes certifi's.
    assert isinstance(tls._context_for_requests(root), tls.GuardedContext)
    monkeypatch.setitem(tls.DISTRUSTED_SPKI_SHA256, pki["intermediate_spki"], "Test Intermediate")
    import requests
    with pytest.raises(requests.exceptions.SSLError, match="Test Intermediate"):
        tls.get(url, timeout=5, verify=root)
    # Not verifying at all is left alone (nothing in the app does).
    assert tls.get(url, timeout=5, verify=False).text == "ok"


def test_every_outgoing_connection_uses_the_guard():
    """No plain ssl.create_default_context() or requests.get() left in the
    app outside tls.py."""
    from pathlib import Path
    app = Path(__file__).resolve().parents[1] / "app"
    for f in app.rglob("*.py"):
        if f.name == "tls.py":
            continue
        text = f.read_text(encoding="utf-8")
        assert "ssl.create_default_context(" not in text, f
        assert "requests.get(" not in text.replace("requests.get()'s", ""), f
        assert "requests.post(" not in text, f
    assert "tls.guard_session(net.session)" in (app / "https_setup.py").read_text(encoding="utf-8")
