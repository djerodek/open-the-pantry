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
    libgdk-pixbuf2.0-0 \
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
