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
