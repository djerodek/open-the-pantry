"""Outgoing TLS: certificates checked as usual, and chains through known
compromised CA keys refused.

Python checks no revocation, so a CA whose keys leak stays trusted until
its root leaves the image's root list (Debian's ca-certificates for email,
certifi for links; 0127 makes every build take the newest). The eMudhra
compromise of October 2026 showed the gap: eight intermediate keys could be
computed from their public certificates, and each key also sits in a
cross-signed copy under another root (Sectigo's AAA Certificate Services).
Go and rustls answered by refusing any chain containing those keys; this
does the same, by the SHA-256 of each certificate's SubjectPublicKeyInfo,
after the usual verification (SSLSocket.get_verified_chain, Python 3.13).

Not covered: requests sent through an https:// proxy (HTTPS_PROXY), where
urllib3 wraps the connection itself; the usual verification still applies
there. The container sets no proxy (Claude review 14, GPT review of 0129).

Used for every outgoing TLS connection the app makes with a secret or
content at stake: the inbox and email sending (email_client), recipe links
(url_ingest.safe_get), cPanel's DNS API and Let's Encrypt (https_setup).
"""
from __future__ import annotations

import hashlib
import os
import ssl
import threading

import requests
from requests.adapters import HTTPAdapter

from .logging_setup import get_logger

log = get_logger("tls")

# SHA-256 of the DER SubjectPublicKeyInfo. The same eight in Go 1.26.10 /
# 1.27.3 (crypto/x509 distrustedSPKIHashes, CVE-2026-111943) and in
# rustls-webpki 0.103.16 (src/data/distrusted/*.spki.der, commit f847737);
# checked to agree on 2026-10-25. All secp384r1, each also cross-signed
# under AAA Certificate Services with the same key.
DISTRUSTED_SPKI_SHA256 = {
    "ebed6475c2c2d80fcb199f3fe8dd6e6b3ce60b77eb15d4e42c3dec0c6f5b5105": "eMudhra EM DV TLS CA G3B",
    "49dd353dc2b919a3770645117d0e60c16115e7d1bc259eefe734b00ea434fe5f": "eMudhra EM DV TLS CA G3C",
    "e0254c7fcdc265e2f0f8a428dabb75ab4ae039b4872d62f5ee57f0877d5a8a41": "eMudhra EM EV TLS CA G3B",
    "5c0660a2b164910d8ff589c18e14552e9e0ec237dcefe2cb736d41a4811f81a0": "eMudhra EM EV TLS CA G3C",
    "43bffa19d6ef651451f6841e019931e96f6f2eaeff28ab4b6f1ce4576de2717b": "eMudhra EM OV TLS CA G3B",
    "63ecefee431d49523bc1136c4e72d0206a477086caf606cd5cf025686c109cf8": "eMudhra EM OV TLS CA G3C",
    "ec4ffa1ad0ac88081314b7aaffef6a91f97cd57291bb5ba686c5415f5eb4f6b6": "eMudhra EM OV CS CA G3B",
    "92c063d67e8f14db71d288e11266ffdaf69cbf115f7bab14f3bc2f68d25e0e30": "eMudhra EM OV CS CA G3C",
}

CAN_CHECK_CHAIN = hasattr(ssl.SSLSocket, "get_verified_chain")      # Python 3.13+
if not CAN_CHECK_CHAIN:
    log.warning("Python 3.13 or later is needed to refuse certificates from known compromised "
                "CA keys; only the root list protects these connections")


class DistrustedCertificateError(ssl.SSLCertVerificationError):
    """The certificate verified, but its chain goes through a CA key known
    to be compromised. A subclass of the usual verification error, so the
    callers that report a certificate problem report this one too."""


def spki_sha256(cert_der: bytes) -> str:
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = x509.load_der_x509_certificate(cert_der).public_key()
    return hashlib.sha256(key.public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).hexdigest()


def check_chain(chain_der: list[bytes]):
    """Raises DistrustedCertificateError if any certificate in the verified
    chain has a distrusted key."""
    for der in chain_der:
        name = DISTRUSTED_SPKI_SHA256.get(spki_sha256(der))
        if name:
            raise DistrustedCertificateError(
                f"certificate verify failed: the chain goes through {name}, a CA key known to be "
                "compromised (October 2026)")


class _GuardedSocket(ssl.SSLSocket):
    def do_handshake(self, block=False):
        super().do_handshake(block)
        if CAN_CHECK_CHAIN and self.context.verify_mode != ssl.CERT_NONE:
            try:
                check_chain(self.get_verified_chain())
            except DistrustedCertificateError:
                log.warning("TLS: refused %s: %s", self.server_hostname, "distrusted CA key", exc_info=True)
                raise


class GuardedContext(ssl.SSLContext):
    """An SSLContext whose sockets refuse a chain through a distrusted key
    once the handshake has verified it."""
    sslsocket_class = _GuardedSocket


def client_context(load_default_certs: bool = True) -> GuardedContext:
    """ssl.create_default_context() for a client, as a GuardedContext: the
    same protocol, options and verification flags (Python 3.13's include
    VERIFY_X509_STRICT), and the system roots unless load_default_certs is
    false (requests brings certifi's)."""
    base = ssl.create_default_context()
    ctx = GuardedContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.options = base.options
    ctx.verify_flags = base.verify_flags
    ctx.minimum_version = base.minimum_version
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    if load_default_certs:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return ctx


_requests_contexts: dict = {}
_contexts_lock = threading.Lock()


def _context_for_requests(verify) -> GuardedContext:
    """One guarded context per trust bundle, shared by every adapter
    (contexts are safe across threads; loading the bundle each time would
    read it per request): certifi's for verify=True, requests' own default,
    or the file or folder given (verify=path, or REQUESTS_CA_BUNDLE /
    CURL_CA_BUNDLE, which requests reads into verify)."""
    import certifi
    path = certifi.where() if verify is True else str(verify)
    with _contexts_lock:
        ctx = _requests_contexts.get(path)
        if ctx is None:
            ctx = client_context(load_default_certs=False)
            if os.path.isdir(path):
                ctx.load_verify_locations(capath=path)
            else:
                ctx.load_verify_locations(cafile=path)
            _requests_contexts[path] = ctx
    return ctx


class GuardedAdapter(HTTPAdapter):
    """requests' adapter with a guarded context whenever certificates are
    verified (the hook requests documents for this,
    build_connection_pool_key_attributes)."""

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        if verify and host_params.get("scheme") == "https":
            pool_kwargs["ssl_context"] = _context_for_requests(verify)
        return host_params, pool_kwargs


def guard_session(session: requests.Session) -> requests.Session:
    """The session's https:// requests through GuardedAdapter."""
    session.mount("https://", GuardedAdapter())
    return session


def session() -> requests.Session:
    """A new guarded session (no cookies carried from one site to the
    next); close it after use, or use it in a with block."""
    return guard_session(requests.Session())


def get(url, **kwargs) -> requests.Response:
    """requests.get through a guarded session (as requests.get does, one
    session per call)."""
    with session() as s:
        return s.get(url, **kwargs)
