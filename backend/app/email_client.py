import email
import imaplib
import smtplib
import ssl
from email.header import decode_header
from email.mime.text import MIMEText


class EmailConnectionError(Exception):
    pass


def decode_subject(raw_subject) -> str:
    if not raw_subject:
        return ""
    try:
        parts = decode_header(raw_subject)
    except Exception:
        return str(raw_subject)
    decoded = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            try:
                decoded += part.decode(enc or "utf-8", errors="replace")
            except (LookupError, TypeError):
                decoded += part.decode("utf-8", errors="replace")
        else:
            decoded += part
    return decoded


def connect_imap(host: str, port: int, username: str, password: str, use_ssl: bool = True) -> imaplib.IMAP4:
    """Connects and logs in, selecting INBOX. Always uses SSL/TLS -- either
    a direct SSL connection (typical port 993) or STARTTLS upgrade of a
    plaintext connection (typical port 143). Raises EmailConnectionError
    with a clean message on any failure rather than leaking raw
    imaplib/socket exceptions up to callers."""
    try:
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port, timeout=20)
        else:
            conn = imaplib.IMAP4(host, port, timeout=20)
            conn.starttls(ssl.create_default_context())
        conn.login(username, password)
        conn.select("INBOX")
        return conn
    except Exception as e:
        raise EmailConnectionError(f"Could not connect/login to IMAP ({host}:{port}): {e}")


def connect_smtp(host: str, port: int, username: str, password: str, use_tls: bool = True) -> smtplib.SMTP:
    """Always uses TLS -- either STARTTLS upgrade (typical port 587) or a
    direct SSL connection (typical port 465, use_tls=False selects this
    path since 'not STARTTLS' here means 'already encrypted')."""
    try:
        if use_tls:
            conn = smtplib.SMTP(host, port, timeout=20)
            conn.starttls(context=ssl.create_default_context())
        else:
            conn = smtplib.SMTP_SSL(host, port, timeout=20)
        conn.login(username, password)
        return conn
    except Exception as e:
        raise EmailConnectionError(f"Could not connect/login to SMTP ({host}:{port}): {e}")


def send_email(smtp_conn: smtplib.SMTP, from_addr: str, to_addr: str, subject: str, body: str):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
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


def fetch_full_message(imap_conn: imaplib.IMAP4, msg_id: bytes) -> email.message.Message:
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
