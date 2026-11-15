# Contributing

*English · [Français](CONTRIBUTING.fr.md)*

The project is unmaintained (see the [README](README.md)), but forks are
welcome. This is how it's built and tested.

## Tests

```bash
cd backend
pip install -r requirements-test.txt
python -m pytest                       # server tests
pip install -r requirements-browser.txt
python -m playwright install --with-deps chromium
python -m pytest tests_browser         # the pages, in headless Chromium
```

The tests need the same system packages as the Docker image
(`tesseract-ocr` and WeasyPrint's libraries; see the `Dockerfile`), and each
run uses a temporary data folder.

- [`backend/tests/README.md`](backend/tests/README.md) lists which test files cover which feature, and
  describes the import corpus (`python -m app.corpus`), which re-reads real
  inputs after a change and reports any that come out worse.
- Every browser test also fails on a JavaScript error or a
  Content-Security-Policy violation. `test_accessibility.py` runs axe-core
  on every screen (English/light, French/dark); the zoom rule is off, since
  zoom is off on purpose.

## CI

CI (`.github/workflows/ci.yml`) runs on every push, every Sunday, and on
demand. It runs both test suites, an undefined-name check of the page
scripts (ESLint), a dependency audit, and a Docker build that is started
and checked. The image is published only once those pass,
so the weekly run refreshes it with the newest base image, trusted roots
and Chrome version (the browser name link imports send). GitHub pauses
scheduled runs after 60 days without a commit.

## Source layout

```
backend/app/        FastAPI app (main.py: start-up, middleware), models, backup, HTTPS, picker
  routes/           the API by area: recipes, ingest, email, settings, backup; common.py shared
  ingestion/        link, PDF (pdf_read), OCR (ocr), layout (segment) and email reading; tagging
  ml/               Settings -> Learning (numpy only)
  templates/        the print/PDF/HTML template
backend/tests/      server tests; corpus/: import corpus cases
backend/tests_browser/  page tests (Playwright)
frontend/           the app: index.html, js/app.js (a JavaScript module, no build step),
                    js/settings.js (the Settings panels), js/add_recipe.js (the Add dialog),
                    js/i18n.js, css/;
                    js/amounts.js + js/weights.js: scaling, Weights, Volumes (also used by What to eat?)
frontend-picker/    the What to eat? page (port 8091)
docker-compose*.yml published image, build from source, HTTPS via Traefik
```

## Logs while debugging

Each entry is tagged by area: `otp.scan`, `otp.email`, `otp.url`, `otp.ocr`
and `otp.client` (errors from the pages), e.g.
`docker logs open-the-pantry 2>&1 | grep otp.scan`. `RECIPE_APP_LOG_LEVEL`
is `DEBUG` for more detail, `WARNING` for less. The log file rotates at
1 MB, with 3 old files kept.
