import email
import email.utils
import imaplib
import smtplib
import socket
import ssl
from email.header import decode_header
from email.mime.text import MIMEText
from .logging_setup import get_logger

log = get_logger("mail")


class EmailConnectionError(Exception):
    pass


def decode_subject(raw_subject) -> str:
    if not raw_subject:
        return ""
    try:
        parts = decode_header(raw_subject)
    except Exception:
        log.debug("decode_subject: caught error, continuing", exc_info=True)
        return str(raw_subject)
    decoded = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            try:
                decoded += part.decode(enc or "utf-8", errors="replace")
            except (LookupError, TypeError):
                log.warning("decode_subject: caught error, continuing", exc_info=True)
                decoded += part.decode("utf-8", errors="replace")
        else:
            decoded += part
    return decoded


# Implicit-TLS ports: the connection is encrypted from the first byte.
# Everything else starts in the clear and upgrades with STARTTLS. There is
# no plaintext path either way.
IMPLICIT_TLS_PORTS = {993, 465}


# Flag naming, for both functions below: use_ssl / use_tls choose HOW TLS
# is established, never WHETHER. IMAP use_ssl=True and SMTP use_tls=False
# both mean implicit TLS (encrypted from the first byte: 993 / 465). The
# other value means connect in the clear and upgrade with STARTTLS
# (143 / 587). The names read the wrong way round for SMTP; kept because
# they are stored columns.
#
# On 993 and 465 the port decides, whatever the stored flag says. A row
# saved before the frontend derived the flag from the port still says
# "STARTTLS" for port 465, and STARTTLS against an implicit-TLS listener
# doesn't fail -- it waits for a greeting that never comes, then times out.
# That was the reported failure, and it survived the frontend fix because
# the fix only took effect on the next save.
def imap_uses_implicit_tls(port: int, use_ssl: bool) -> bool:
    return port in IMPLICIT_TLS_PORTS or use_ssl


def smtp_uses_implicit_tls(port: int, use_tls: bool) -> bool:
    return port in IMPLICIT_TLS_PORTS or not use_tls


def connect_imap(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> imaplib.IMAP4:
    """Connects and logs in, selecting INBOX. Raises EmailConnectionError
    with a clean message on any failure rather than leaking raw
    imaplib/socket exceptions up to callers."""
    implicit = imap_uses_implicit_tls(port, use_ssl)
    try:
        if implicit:
            conn = imaplib.IMAP4_SSL(host, port, timeout=20)
        else:
            conn = imaplib.IMAP4(host, port, timeout=20)
            conn.starttls(ssl.create_default_context())
        conn.login(username, password)
        conn.select("INBOX")
        return conn
    except Exception as e:
        log.debug("connect_imap: caught error, continuing", exc_info=True)
        raise EmailConnectionError(
            f"Could not connect/login to IMAP ({host}:{port}, "
            f"{'implicit TLS' if implicit else 'STARTTLS'}): {e}"
            + _diagnose_suffix(host, port, implicit, e)
        )


def connect_smtp(host: str, port: int, username: str, password: str, use_tls: bool = True) -> smtplib.SMTP:
    """Connects and logs in. See the flag note above."""
    implicit = smtp_uses_implicit_tls(port, use_tls)
    try:
        if implicit:
            conn = smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context())
        else:
            conn = smtplib.SMTP(host, port, timeout=20)
            conn.starttls(context=ssl.create_default_context())
        conn.login(username, password)
        return conn
    except Exception as e:
        log.debug("connect_smtp: caught error, continuing", exc_info=True)
        raise EmailConnectionError(
            f"Could not connect/login to SMTP ({host}:{port}, "
            f"{'implicit TLS' if implicit else 'STARTTLS'}): {e}"
            + _diagnose_suffix(host, port, implicit, e)
        )


def probe_port(host: str, port: int, timeout: float = 8.0) -> str:
    """What is actually listening on host:port, independent of settings.

    Returns one of:
      "unreachable"   -- no TCP connection (firewall, wrong host/port, or the
                         network path from this container is blocked)
      "implicit_tls"  -- a TLS handshake succeeds straight away
      "plaintext"     -- the server speaks first in the clear (a STARTTLS port)
      "cert_error"    -- TLS is there but the certificate doesn't verify
      "silent"        -- TCP connects, but neither a TLS handshake nor a
                         plaintext greeting arrives
    Used only to explain a failure, never to pick the mode: a probe that
    could switch the app to a different connection type on its own would be
    a downgrade path waiting to happen.
    """
    try:
        raw = socket.create_connection((host, port), timeout=timeout)
    except OSError:
        log.debug("probe_port: caught error, continuing", exc_info=True)
        return "unreachable"
    try:
        raw.settimeout(timeout)
        ctx = ssl.create_default_context()
        try:
            with ctx.wrap_socket(raw, server_hostname=host):
                return "implicit_tls"
        except ssl.SSLCertVerificationError:
            return "cert_error"
        except (ssl.SSLError, OSError):
            log.debug("probe_port: caught error, continuing", exc_info=True)
            pass
    finally:
        try:
            raw.close()
        except OSError:
            log.debug("probe_port: caught error, continuing", exc_info=True)
            pass
    # Not TLS from the first byte. Does it greet in the clear?
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw2:
            raw2.settimeout(timeout)
            banner = raw2.recv(64)
            if banner[:3] in (b"220", b"* O"):  # SMTP "220 ...", IMAP "* OK ..."
                return "plaintext"
    except OSError:
        log.debug("probe_port: caught error, continuing", exc_info=True)
        pass
    return "silent"


def _diagnose_suffix(host: str, port: int, implicit: bool, err: Exception) -> str:
    """One plain-language sentence explaining a connection failure, based on
    probing the port. Only for network-level failures -- a wrong password is
    reported as-is, and probing wouldn't add anything."""
    if isinstance(err, (smtplib.SMTPAuthenticationError, imaplib.IMAP4.error)) and \
            not isinstance(err, imaplib.IMAP4.abort):
        return ""
    try:
        found = probe_port(host, port)
    except Exception:
        log.debug("_diagnose_suffix: caught error, continuing", exc_info=True)
        return ""
    if found == "unreachable":
        return (f" -- Diagnosis: can't open a connection to {host} on port {port} from the "
                "container at all. The app's settings aren't the problem; a firewall, your ISP, "
                "or the mail host is blocking that port from your network. Try the other port "
                "your host lists (587 for SMTP, 143 for IMAP), or check the host's docs for "
                "IP restrictions.")
    if found == "implicit_tls" and not implicit:
        return (f" -- Diagnosis: port {port} expects TLS immediately, but the app used STARTTLS.")
    if found == "plaintext" and implicit:
        return (f" -- Diagnosis: port {port} is a STARTTLS port (the server greets in plain "
                "text first), but the app expected TLS immediately.")
    if found == "cert_error":
        return (f" -- Diagnosis: {host}:{port} has TLS, but its certificate doesn't verify for "
                f"that hostname. Use the exact server name from the certificate (shared hosts "
                "often want their own name, e.g. the server's hostname rather than "
                "mail.yourdomain), or ask the host.")
    if found == "silent":
        return (f" -- Diagnosis: {host}:{port} accepts the connection but never answers. "
                "Usually a firewall or proxy in between, or the wrong port.")
    return ""


def send_email(smtp_conn: smtplib.SMTP, from_addr: str, to_addr: str, subject: str, body: str):
    # Charset stated explicitly. MIMEText(body) with no charset already
    # switches to UTF-8 when the body isn't ASCII (checked: accented and
    # CJK text round-trips), so this documents intent rather than fixing a
    # failure.
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    # Most servers add a Date on receipt; some strict ones reject mail
    # that arrives without one.
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid()
    smtp_conn.sendmail(from_addr, [to_addr], msg.as_string())


def search_unseen_by_subject(imap_conn: imaplib.IMAP4, subject_keyword: str) -> list[bytes]:
    """Returns message IDs for UNSEEN emails whose subject contains
    subject_keyword (case-insensitive substring match). Uses a broad
    UNSEEN search then filters subjects client-side -- IMAP SEARCH SUBJECT
    behavior (case sensitivity, substring vs. exact) varies enough across
    server implementations that client-side filtering on the decoded
    subject is more predictable than relying on it. Uses BODY.PEEK so
    fetching the header for inspection doesn't itself mark anything Seen
    -- only mark_seen() (called explicitly once a message has actually
    been handled) should do that."""
    status, data = imap_conn.search(None, "UNSEEN")
    if status != "OK" or not data or not data[0]:
        return []
    matched = []
    for msg_id in data[0].split():
        status, header_data = imap_conn.fetch(msg_id, "(BODY.PEEK[HEADER.FIELDS (SUBJECT)])")
        if status != "OK" or not header_data or not isinstance(header_data[0], tuple):
            continue
        raw_header = header_data[0][1]
        msg = email.message_from_bytes(raw_header)
        subject = decode_subject(msg.get("Subject", ""))
        if subject_keyword.lower() in subject.lower():
            matched.append(msg_id)
    return matched


# Whole-message ceiling, checked before downloading. Attachments are capped
# at 20 MB once decoded; base64 adds a third, plus headers and body. Without
# this the full message is downloaded and parsed in memory before any limit
# applies.
MAX_MESSAGE_BYTES = 30 * 1024 * 1024


class MessageTooLargeError(Exception):
    pass


def message_size(imap_conn: imaplib.IMAP4, msg_id: bytes):
    """RFC822.SIZE for one message, or None if the server doesn't say."""
    import re as _re
    try:
        status, data = imap_conn.fetch(msg_id, "(RFC822.SIZE)")
    except Exception:
        log.debug("message_size: caught error, continuing", exc_info=True)
        return None  # can't tell; the full fetch is still bounded by the server
    if status != "OK" or not data:
        return None
    first = data[0][0] if isinstance(data[0], tuple) else data[0]
    if isinstance(first, bytes):
        m = _re.search(rb"RFC822\.SIZE (\d+)", first)
        if m:
            return int(m.group(1))
    return None


def fetch_full_message(imap_conn: imaplib.IMAP4, msg_id: bytes) -> email.message.Message:
    size = message_size(imap_conn, msg_id)
    if size is not None and size > MAX_MESSAGE_BYTES:
        raise MessageTooLargeError(
            f"email is {size / 1048576:.0f} MB; the limit is {MAX_MESSAGE_BYTES // 1048576} MB"
        )
    status, data = imap_conn.fetch(msg_id, "(BODY.PEEK[])")
    if status != "OK" or not data or not isinstance(data[0], tuple):
        raise EmailConnectionError(f"Could not fetch message {msg_id!r}")
    return email.message_from_bytes(data[0][1])


def mark_seen(imap_conn: imaplib.IMAP4, msg_id: bytes):
    """Marks a message handled. Called once processing is complete --
    success or failure -- so a permanently-unparseable tagged email is
    never retried indefinitely on every future scan."""
    # Flag list must be parenthesized per RFC 3501's STORE grammar.
    # Gmail/Dovecot accept the bare form, but stricter servers (Cyrus,
    # some Postfix setups) reject it -- the kind of thing that works on
    # the developer's provider and silently fails on someone else's.
    imap_conn.store(msg_id, "+FLAGS", "(\\Seen)")
