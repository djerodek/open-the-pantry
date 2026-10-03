"""Application logging.

Before this module the app logged nothing: every failure in ingestion was
caught and turned into a one-line message, and the underlying exception was
discarded. That's fine for the person looking at the UI, and useless for
finding out why a particular page or email didn't work.

Everything goes to two places:
  * stdout, so `docker logs open-the-pantry` shows it;
  * <data dir>/logs/app.log, rotated at 1 MB, 3 old files kept -- survives
    the container being recreated, and can be read from the NAS directly.

Level: RECIPE_APP_LOG_LEVEL (default INFO). DEBUG adds the page-level detail
of URL ingestion (JSON-LD blocks found, etc.).

Never logged: passwords, the encryption key, email bodies, page HTML.
Subjects, part types/sizes, URLs and exception tracebacks are logged -- that
is the minimum needed to diagnose a failed ingest.
"""
import logging
import logging.handlers
import os
import re

LOGGER_NAME = "otp"


def setup_logging(data_dir: str) -> logging.Logger:
    log = logging.getLogger(LOGGER_NAME)
    if log.handlers:  # already configured (tests import the app repeatedly)
        return log

    level = os.environ.get("RECIPE_APP_LOG_LEVEL", "INFO").upper()
    log.setLevel(getattr(logging, level, logging.INFO))
    log.propagate = False  # don't double-print through uvicorn's root handler

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    log.addHandler(stream)

    try:
        log_dir = os.path.join(data_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(log_dir, "app.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as e:
        log.warning("File logging disabled, can't write to %s/logs: %s", data_dir, e)

    return log


def get_logger(name: str) -> logging.Logger:
    """Child of the app logger, e.g. get_logger("email") -> "otp.email"."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


# ---------------------------------------------------------------------------
# Settings -> Logs: view, download, clear
# ---------------------------------------------------------------------------

_RECORD_START = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d (\w+)\s")
_PROBLEM_LEVELS = {"WARNING", "ERROR", "CRITICAL"}


def _file_handler():
    for h in logging.getLogger(LOGGER_NAME).handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler):
            return h
    return None


def log_files(data_dir: str) -> list[str]:
    """app.log and its rotated copies that exist, oldest first."""
    h = _file_handler()
    base = h.baseFilename if h else os.path.join(data_dir, "logs", "app.log")
    count = h.backupCount if h else 3
    files = [f"{base}.{i}" for i in range(count, 0, -1)] + [base]
    return [f for f in files if os.path.isfile(f)]


def read_records(data_dir: str, limit: int = 300, problems_only: bool = False) -> dict:
    """The newest `limit` records, oldest first. A record is a log line plus
    any lines that follow it without a timestamp (a traceback)."""
    from collections import deque
    files = log_files(data_dir)
    records = deque(maxlen=limit)
    current, current_level = None, None

    def flush():
        if current is not None and (not problems_only or current_level in _PROBLEM_LEVELS):
            records.append(current)

    for path in files:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                m = _RECORD_START.match(line)
                if m:
                    flush()
                    current, current_level = line, m.group(1)
                elif current is not None:
                    current += "\n" + line
                else:
                    current, current_level = line, None
    flush()
    return {"records": list(records), "files": len(files),
            "bytes": sum(os.path.getsize(f) for f in files)}


def clear_logs(data_dir: str) -> None:
    """Empties app.log in place and deletes the rotated copies. The handler
    keeps writing to the same (now empty) file."""
    h = _file_handler()
    files = log_files(data_dir)
    if h:
        h.acquire()
    try:
        for path in files:
            if h and path == h.baseFilename:
                if h.stream:
                    h.stream.flush()
                    h.stream.seek(0)
                    h.stream.truncate()
                else:
                    open(path, "w").close()
            elif path.endswith(".log"):
                open(path, "w").close()
            else:
                os.remove(path)
    finally:
        if h:
            h.release()
