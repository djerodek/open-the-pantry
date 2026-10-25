# Tests

Run from `backend/`: `python -m pytest -q`. The scaling tests run `app.js`
functions in Node and are skipped without it. The pages' own tests are in
`../tests_browser/` (headless Chromium, `python -m pytest tests_browser`),
with the accessibility scan in `test_accessibility.py`.

Files are named two ways. **Feature files** hold the tests for one part of
the app. **Review files** (`test_<reviewer>_…`) hold the regression tests
for the findings of one outside code review; each file's opening note says
what was broken and why, and each test failed on the code before its fix.
New tests go in the feature file when there is one.

## By feature

| Feature | Feature files | Also in review files |
|---|---|---|
| Recipes: create, edit, delete, list, sort | `test_api_core.py`, `test_pace_filter.py`, `test_servings.py`, `test_showcase_image.py`, `test_recently_deleted.py`, `test_merge.py` | `test_stability.py`, `test_friday_batch.py`, `test_claude_review_round8.py`, `test_gpt_review_0119.py` (merge limits), `test_gpt_review_0123.py` (no photo left by a refused merge), `test_gpt_review_0129.py` (merge photo copy failing) |
| Search | `test_search_and_parsing.py`, `test_search_typos.py` | `test_claude_review_round11.py` (control characters, Recently deleted out of the index) |
| Import from a link | `test_incomplete_sources.py` | `test_security.py`, `test_security_round2.py` (address and size checks), `test_review_round2_followup.py` (slow downloads), `test_claude_review_round9.py` |
| Import from a PDF or photo (reading the text) | `test_pdf_ingest.py`, `test_component_recipes.py`, `test_deskew.py`, `test_incomplete_sources.py` | `test_review_round2_followup.py` (OCR limits), `test_sept27_fixes.py`, `test_deepseek_0073.py`, `test_deepseek_review_0096.py`, `test_import_layouts_0132.py` (rules from the owner's corpus), `test_gpt_review_0132.py` (bare copyright line) |
| Ingredients (parsing, scaling) | `test_ingredient_parser.py`, `test_ui_consistency.py` (scaling) | `test_claude_review_round9.py`, `test_claude_review_round13.py` (long digit runs) |
| French | `test_french.py`, `test_i18n_catalog.py` | `test_friday_batch.py` (server messages), `test_deepseek_0073.py` |
| Tags and the tagger | `test_tagger.py`, `test_auto_tag.py`, `test_tag_groups.py`, `test_batch_tags.py` | `test_deepseek_round.py` |
| Learning | `test_learning.py`, `test_learning_explained.py` | |
| Import corpus (real inputs read again) | `test_corpus.py` | |
| Already in your library? | `test_duplicates.py`, `test_library_check.py` | `test_claude_review_round9.py`, `test_gpt_review_0093.py` |
| Duplicate a recipe | `test_duplicate_recipe.py` | `test_gpt_review_0132.py` (photo copy failing) |
| Review queue (To review) | `test_review_queue.py` | |
| Email ingest | `test_email_ingest.py`, `test_email_real_world.py` | `test_deepseek_round.py`, `test_security_round2.py` (saved password), `test_qwen_review.py`, `test_claude_review_round13.py` (inbox certificate, unknown charsets, scan failures logged), `test_claude_review_round14.py` (codec names that aren't text encodings) |
| Backup and export | `test_backup.py`, `test_interchange.py`, `test_scheduled_backup.py` | `test_qwen_review.py`, `test_deepseek_0083.py`, `test_gpt_review_0093.py`, `test_gpt_review_0104.py` (names in the same second, backup folder owner), `test_claude_review_round13.py` (a failed automatic backup tried again), `test_gpt_review_0129.py` (a failure after the day's success retried) |
| Restore | `test_restore.py` | `test_gpt_review_0093.py` |
| Settings password | `test_settings_password.py` | |
| HTTPS | `test_https_setup.py`, `test_tls_key_containment.py` | |
| Security (uploads, paths, cross-site, headers, outgoing TLS) | `test_tls_distrust.py` (compromised CA keys), `test_security.py`, `test_security_round2.py`, `test_security_headers.py`, `test_dependency_cves_0104.py` (image decoders, PDF photos, library versions) | `test_claude_review_round8.py`, `test_gpt_review_0104.py` (soft masks, re-encoding memory), `test_deepseek_review_0114.py` (body and field limits), `test_gpt_review_0119.py` (upload sizes as they arrive), `test_claude_review_round13.py` (trusted roots in the image, publishing after CI) |
| Logs | `test_log_review.py` | `test_claude_review_round7.py` |
| What to eat? | `test_picker.py` | `test_gpt_review_0078.py`, `test_claude_review_round7.py`, `test_claude_review_round8.py`, `test_claude_review_round14.py` (language guesses kept), `test_gpt_review_0129.py` (same-length edits) |
| Things kept in two places (French text, page vs server rules) | `test_ui_consistency.py`, `test_i18n_catalog.py` | `test_deepseek_review_0096.py` (part names) |
| Times and dates | `test_time_utils.py` | `test_stability.py`, `test_deepseek_0073.py`, `test_qwen_review.py` |

The "also in" column lists where to look, not every file that touches a
feature: a review file often covers several.

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
