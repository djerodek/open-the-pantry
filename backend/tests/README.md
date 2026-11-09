# Tests

Run from `backend/`: `python -m pytest -q`. The scaling tests run `app.js`
functions in Node and are skipped without it. The pages' own tests are in
`../tests_browser/` (headless Chromium, `python -m pytest tests_browser`),
with the accessibility scan in `test_accessibility.py`.

Each file holds the tests for one part of the app; new tests go in the
file for the part they test. A test for a fixed bug says in its docstring
what was broken and which patch or review found it, and it failed on the
code before the fix. (Until 0165, those tests were kept in one file per
review; they were moved into these files with their history in git.)

## By feature

| Feature | Files |
|---|---|
| Recipes: create, edit, delete, list, sort, API limits | `test_api_core.py`, `test_pace_filter.py`, `test_servings.py`, `test_showcase_image.py`, `test_recently_deleted.py` |
| Merge and duplicate a recipe | `test_merge.py`, `test_duplicate_recipe.py` |
| The database (indexes, busy as 503, older databases) | `test_database.py` |
| Search | `test_search_and_parsing.py`, `test_search_typos.py` |
| Import from a link | `test_url_import.py` (what's read, the download budget, URLs in the log), `test_url_errors.py`, `test_incomplete_sources.py`, `test_chrome_version.py` |
| Import from a PDF or photo | `test_pdf_ingest.py`, `test_component_recipes.py`, `test_deskew.py`, `test_import_layouts.py`, `test_incomplete_sources.py` |
| Ingredients (parsing, scaling, units) | `test_ingredient_parser.py`, `test_ui_consistency.py` (scaling), `test_weights.py`, `test_volumes.py` |
| Recipe language, French | `test_language.py`, `test_french.py`, `test_i18n_catalog.py` |
| Tags and the tagger | `test_tagger.py`, `test_auto_tag.py`, `test_tag_groups.py`, `test_batch_tags.py` |
| Learning | `test_learning.py`, `test_learning_explained.py` |
| Import corpus | `test_corpus.py` |
| Already in your library? | `test_duplicates.py`, `test_library_check.py` |
| Review queue (To review) | `test_review_queue.py` |
| Email ingest | `test_email_ingest.py`, `test_email_real_world.py` |
| Share and export (PDF, HTML, JSON, Email PDF) | `test_export.py` |
| Backup | `test_backup.py`, `test_backup_settings.py`, `test_scheduled_backup.py`, `test_interchange.py` (schema.org, Paprika) |
| Restore | `test_restore.py` |
| Settings password | `test_settings_password.py` |
| HTTPS | `test_https_setup.py`, `test_tls_key_containment.py` |
| Security (addresses, uploads, paths, hosts, outgoing TLS) | `test_security.py`, `test_security_headers.py`, `test_tls_distrust.py`, `test_dependency_cves.py` |
| Logs | `test_logs.py` |
| Startup and background jobs | `test_startup.py` |
| What to eat? | `test_picker.py`, `test_picker_amounts.py` |
| Things kept in two places | `test_ui_consistency.py`, `test_i18n_catalog.py` |
| Page behaviour without a browser | `test_frontend_source.py` |
| CI and the Docker image | `test_ci_and_image.py` |
| Times and dates | `test_time_utils.py` |

The pages themselves: `tests_browser/test_pages.py` (each test names the
patch whose bug it would have caught) and `test_accessibility.py`.

## Import corpus

`test_corpus.py` reads every case in `tests/corpus/` again with the import
code and fails when a field scores more than 0.02 below its baseline
(`baseline.json`). Those cases are written for this repository, so CI
can run them. Real recipes are other people's text: keep them in a private
corpus outside the repository (the data folder is git-ignored and isn't in
backups) and point `RECIPE_APP_CORPUS_DIR` at it to run it too. How cases
are made, scored and run is at the top of `app/corpus.py`; in short:

```bash
# Inside the container, for production's Tesseract:
docker exec open-the-pantry python -m app.corpus export /app/data/recipes.db /app/data/corpus
docker exec open-the-pantry python -m app.corpus report /app/data/corpus --save-baseline
# After an update, or before pushing a change:
docker exec open-the-pantry python -m app.corpus report /app/data/corpus
docker exec open-the-pantry python -m app.corpus report /app/data/corpus --show 00012-chicken-curry
# A file or link that imported badly, as a draft to correct:
docker exec open-the-pantry python -m app.corpus add /app/data/corpus --pdf /app/data/some.pdf
```

`export` turns your checked recipes into cases: the text the import read
(PDF, photo, email) or the page fetched again (a link), with what you saved
as the expected result. `report` exits 1 when a case got worse, and
`--save-baseline` accepts the scores as they are now. A checked case has to
have a baseline: until it does, the report lists it as not yet guarded and
`test_corpus.py` fails on it. The scores measure
the rules and link readers without Learning, which scores itself in
Settings.

## Patching after the route split (0140)

The API's routes are in `app/routes/` (`recipes`, `ingest`, `email`,
`settings`, `backup`, and `common` for what they share); `app/main.py`
keeps start-up and the middleware. Patch a name in the module that uses
it: `monkeypatch.setattr(routes.ingest, "ingest_url", …)`, not `main`. A
helper defined in `common` and imported by a route module is patched in
that route module. `conftest.app_routes(app)` lists every route, including
those of the included routers (`app.routes` shows each router as one
entry).

## Tests that read the source

A test should check what the code does. Reading a file's text is kept for
what a test can't run: the CI workflow, the Dockerfile and entrypoint
(`test_ci_and_image.py`), pinned library versions (`test_dependency_cves.py`),
rules over the whole app (every outgoing connection through the TLS guard,
every Tesseract run with a time limit, every image open naming its formats),
values kept in two places (`test_ui_consistency.py`, `test_i18n_catalog.py`),
and page behaviour no browser test covers yet (`test_frontend_source.py`).
When a browser test covers the same thing, the source check goes. When a
refactor breaks a source check, change it to the new structure; don't
delete it.
