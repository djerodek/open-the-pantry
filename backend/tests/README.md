# Tests

Run from `backend/`: `python -m pytest -q`. The scaling tests run `app.js`
functions in Node and are skipped without it. The pages' own tests are in
`../tests_browser/` (headless Chromium, `python -m pytest tests_browser`).

Files are named two ways. **Feature files** hold the tests for one part of
the app. **Review files** (`test_<reviewer>_…`) hold the regression tests
for the findings of one outside code review; each file's opening note says
what was broken and why, and each test failed on the code before its fix.
New tests go in the feature file when there is one.

## By feature

| Feature | Feature files | Also in review files |
|---|---|---|
| Recipes: create, edit, delete, list, sort | `test_api_core.py`, `test_pace_filter.py`, `test_servings.py`, `test_showcase_image.py`, `test_recently_deleted.py` | `test_stability.py`, `test_friday_batch.py`, `test_claude_review_round8.py` |
| Search | `test_search_and_parsing.py` | |
| Import from a link | `test_incomplete_sources.py` | `test_security.py`, `test_security_round2.py` (address and size checks), `test_review_round2_followup.py` (slow downloads), `test_claude_review_round9.py` |
| Import from a PDF or photo (reading the text) | `test_pdf_ingest.py`, `test_component_recipes.py`, `test_deskew.py`, `test_incomplete_sources.py` | `test_review_round2_followup.py` (OCR limits), `test_sept27_fixes.py`, `test_deepseek_0073.py`, `test_deepseek_review_0096.py` |
| Ingredients (parsing, scaling) | `test_ingredient_parser.py`, `test_ui_consistency.py` (scaling) | `test_claude_review_round9.py` |
| French | `test_french.py`, `test_i18n_catalog.py` | `test_friday_batch.py` (server messages), `test_deepseek_0073.py` |
| Tags and the tagger | `test_tagger.py`, `test_auto_tag.py`, `test_tag_groups.py`, `test_batch_tags.py` | `test_deepseek_round.py` |
| Learning | `test_learning.py`, `test_learning_explained.py` | |
| Already in your library? | `test_duplicates.py` | `test_claude_review_round9.py`, `test_gpt_review_0093.py` |
| Duplicate a recipe | `test_duplicate_recipe.py` | |
| Review queue (To review) | `test_review_queue.py` | |
| Email ingest | `test_email_ingest.py`, `test_email_real_world.py` | `test_deepseek_round.py`, `test_security_round2.py` (saved password), `test_qwen_review.py` |
| Backup and export | `test_backup.py`, `test_interchange.py`, `test_scheduled_backup.py` | `test_qwen_review.py`, `test_deepseek_0083.py`, `test_gpt_review_0093.py`, `test_gpt_review_0104.py` (names in the same second, backup folder owner) |
| Restore | `test_restore.py` | `test_gpt_review_0093.py` |
| Settings password | `test_settings_password.py` | |
| HTTPS | `test_https_setup.py`, `test_tls_key_containment.py` | |
| Security (uploads, paths, cross-site, headers) | `test_security.py`, `test_security_round2.py`, `test_security_headers.py`, `test_dependency_cves_0104.py` (image decoders, PDF photos, library versions) | `test_claude_review_round8.py`, `test_gpt_review_0104.py` (soft masks, re-encoding memory) |
| Logs | `test_log_review.py` | `test_claude_review_round7.py` |
| What to eat? | `test_picker.py` | `test_gpt_review_0078.py`, `test_claude_review_round7.py`, `test_claude_review_round8.py` |
| Things kept in two places (French text, page vs server rules) | `test_ui_consistency.py`, `test_i18n_catalog.py` | `test_deepseek_review_0096.py` (part names) |
| Times and dates | `test_time_utils.py` | `test_stability.py`, `test_deepseek_0073.py`, `test_qwen_review.py` |

The "also in" column lists where to look, not every file that touches a
feature: a review file often covers several.
