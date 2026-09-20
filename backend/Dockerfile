# NOTE: this is a floating tag -- it follows whatever Debian release the
# Python image currently tracks. That moved from Bookworm to Trixie, which
# is what broke the original build: the transitional package
# "libgdk-pixbuf2.0-0" was dropped in Trixie. The name below
# ("libgdk-pixbuf-2.0-0") is the real package in BOTH releases, so it works
# either way. If a future base-image move breaks apt again, pin the suite
# explicitly (e.g. python:3.12-slim-trixie) rather than chasing names.
FROM python:3.12-slim

# System dependencies:
# - tesseract-ocr: OCR for screenshots / scanned PDF pages
# - libpango/cairo/gdk-pixbuf/libffi: required by WeasyPrint for HTML->PDF export
# - gosu: drop from root to a non-root user at container start (see
#   docker-entrypoint.sh) -- needed because a Dockerfile-time chown can't
#   fix ownership of a bind-mounted host directory, only a runtime one can
# - curl: container healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    fonts-liberation \
    gosu \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY frontend ./static

# Persistent data lives under /app/data (sqlite db, uploaded images/pdfs)
# Mount a volume here in docker-compose.yml to persist across container updates.
RUN mkdir -p /app/data/uploads /app/data/tmp \
    && groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -s /usr/sbin/nologin -M appuser \
    && chown -R appuser:appuser /app

# Fontconfig (used by WeasyPrint for PDF export) wants somewhere to cache its
# font list. The app runs as non-root with no home directory, so it has
# nowhere to write and logs "Fontconfig error: No writable cache directories"
# at startup. Harmless -- it falls back to building the list in memory, once,
# in this long-lived process -- but it is noise in the logs that looks like a
# fault. Point the cache at a writable path instead.
ENV XDG_CACHE_HOME=/tmp/.cache

COPY backend/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8090/healthz || exit 1

# Starts as root (image default) so the entrypoint can fix bind-mount
# ownership, then drops to the non-root appuser before running uvicorn --
# the app process itself never runs as root.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
