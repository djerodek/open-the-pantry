# Open the Pantry

Self-hosted, searchable recipe manager. Add recipes by URL, PDF, screenshot/photo,
or manual entry; browse by meal type, cooking style, or main ingredient; filter
and search; rate and favorite; print or export a clean, ad-free share card.
Single Docker image, no LLM dependency required for any ingestion path.

---

> ## ⚠️ Run this on your own network only
>
> **This application has no authentication by default.** Anyone who can reach
> its address can read, modify, and delete every recipe — and use its API.
> There is no login screen, no user accounts, and no permission model.
>
> **Do not expose it directly to the internet.** Run it on your LAN, or reach
> it remotely through a VPN (WireGuard, Tailscale, OpenVPN) or behind a reverse
> proxy that enforces authentication itself. If it must be reachable beyond a
> trusted network, setting `RECIPE_APP_API_KEY` is the minimum, not the whole
> answer — see [Security notes](#security-notes).
>
> This is not a theoretical concern. A vulnerability allowing any unauthenticated
> caller to read arbitrary files from the container *and delete the application's
> own database* was found and fixed late in development — after several review
> passes had already reported the code as clean. It is fixed and regression-tested,
> but it's a fair indication that others may exist and simply haven't been found.
> **The network boundary is the real protection here.** Treat it that way.
>
> If you enable the optional email-ingest feature, the app also stores email
> credentials (encrypted at rest). At that point access control stops being
> optional in practice — use a dedicated email account with an app-specific
> password, never your primary one.

> ## 📋 Project status: unmaintained
>
> Built with AI coding tools for personal home use. Published in case it's
> useful to someone else.
>
> Provided as-is, with no support, no issue triage, and no commitment to fix
> bugs or ship security updates. Development continues only as long as it
> serves my own needs. Forks are welcome and encouraged — if you need changes,
> that's the path.

---

## Run it

Requires Docker and Docker Compose.

```bash
curl -O https://raw.githubusercontent.com/djerodek/open-the-pantry/main/docker-compose.yml
docker compose up -d
```

Open `http://localhost:8090`. Data (SQLite DB + uploaded images/PDFs) persists
in `./data` next to the compose file.

### Moving or restoring your data

Everything the app stores lives in one folder: whatever is on the left side
of the `volumes:` line in `docker-compose.yml` (`./data` by default). It
holds `recipes.db`, `uploads/` (photos and PDFs), and `encryption.key` if you
set up email ingest from Settings. Moving the app to another drive means
moving that folder and pointing the compose file at it.

**Moving to another drive or path** (e.g. from `./data` to a NAS pool):

1. Stop the app: `docker compose down`. Do not copy while it is running --
   SQLite may be holding recent writes in `recipes.db-wal`, and a copy taken
   mid-write can be inconsistent.
2. Copy the whole folder, keeping everything in it:
   `cp -a ./data /path/on/nas/open-the-pantry-data`
3. Edit the volume line in `docker-compose.yml`. Change only the left side;
   `/app/data` on the right is the path inside the container and must stay:

   ```yaml
   volumes:
     - /path/on/nas/open-the-pantry-data:/app/data
   ```

4. Start it: `docker compose up -d`. The container fixes file ownership on
   start, so no `chown` is needed.
5. Check your recipes are there before deleting the old folder.

**Restoring from a backup zip** (Settings → Backup & export → Full backup):

1. `docker compose down`
2. Unzip into the folder the volume line points at, so that `recipes.db` and
   `uploads/` sit directly inside it (not in a subfolder):
   `unzip -o open-the-pantry-backup-*.zip -d /path/to/data`
3. `docker compose up -d`

Restoring replaces the current library. The backup zip deliberately does
**not** include `encryption.key`, so a leaked backup exposes no email
password. After restoring onto a fresh install, either copy `encryption.key`
across from the old data folder or set up encryption again in Settings and
re-enter the email password. Nothing else depends on the key.

To build from source instead of pulling the published image:

```bash
docker compose -f docker-compose.build.yml up -d --build
```

## What it does

**Ingestion**
- **URL** — tries `recipe-scrapers` (100+ site-specific parsers) first, falls
  back to `schema.org` JSON-LD, then a heuristic HTML scrape. Individual or
  batch (up to 50 URLs at once, auto-saved without per-item review). Also
  downloads the source page's showcase image (`og:image`/JSON-LD image)
  when present, through the same validated pipeline as any other image
  upload — a broken/oversized/invalid image never fails the ingestion,
  it's just skipped.
- **PDF** — checks each page for an existing text layer first; only pages
  without one (scanned images) go through Tesseract OCR, with an
  orientation pass (pages that are sideways or upside down are turned
  upright) and a deskew pass for pages fed in at a slight angle. Mixed documents are
  handled per-page. Individual or batch (up to 20 files and 100 MB in
  total, auto-saved). Also
  extracts the largest embedded image across the document as a showcase
  photo, if one is present and large enough to plausibly be a real photo
  rather than a logo/icon.
- **Screenshot/photo** — OCR with preprocessing (grayscale, orientation,
  deskewing, upscaling, thresholding). A photo taken sideways or upside
  down is detected and turned upright before OCR, and the saved photo is
  turned too so it displays the right way up; deskewing then corrects a
  page photographed at a slight angle. Works well on rendered/
  screenshot text; degrades on stylized fonts or low-contrast
  text-over-image graphics. Not intended for handwriting — use manual
  entry for handwritten cards instead. A "Combine multiple" mode handles
  recipes that span several screenshots (up to 10): each is OCR'd
  independently in the order selected, then the extracted text is
  concatenated and segmented as one recipe rather than producing separate
  entries — unlike URL/PDF batch mode, which intentionally does produce
  separate entries. The first image becomes the showcase photo by default
  (changeable afterward); a bad file anywhere in the set aborts the whole
  combine rather than silently producing a partial recipe.
- **Manual entry** — free-text fields plus optional photo attachment, for
  handwritten cards or anything OCR won't parse reliably.
- **Any saved recipe can be edited afterward** via "Edit recipe" on the
  detail page — title, ingredients, steps, tags, servings, and times. The
  original extracted text (whatever the scraper or OCR actually produced)
  is shown read-only alongside the editable fields, so a partially
  successful scrape can be corrected against the source rather than from
  memory. This matters most for the paths that auto-save without review
  (batch URL, batch PDF, email) and for low-confidence OCR you only notice
  was wrong later.
- Single-item ingestion (URL/PDF/screenshot) always routes through a
  manual-review screen — raw extracted text alongside editable fields, plus
  a preview of any auto-captured showcase image with a "Don't use this
  image" option — before saving. Batch ingestion saves directly and reports
  per-item success/failure, since reviewing many items one at a time isn't
  really a batch operation.
- **How extracted text becomes a recipe** (PDF, photo and email text all
  use the same rules): the title is the line above the author byline, or
  a short line the page repeats, never the browser's print date or a
  breadcrumb. Ingredient lines that wrapped are rejoined, sub-headings
  like "PEPPERED BACON CURE" become "For the peppered bacon cure:", and
  recipe-card buttons ("1X 2X 3X", "US Customary / Metric") are dropped.
  Steps are rebuilt whole from their printed lines, split at step numbers,
  and end at "Notes" or "Nutrition". Plain-text email formatting (Gmail's
  `*bold*` and `-` bullets) is understood, and numbered steps are found
  even without an "Instructions" heading.
- Uploads (PDF/image) are capped at 20MB and are validated by magic bytes,
  not just file extension, before any parsing/OCR is attempted.

**Email ingest (optional, off by default)**
- Email a recipe to a configured inbox with a keyword in the subject
  (default `[RECIPE]`), and Open the Pantry picks it up on its next scan.
  Everything without that keyword is ignored entirely — the app never
  attempts to parse, or even fully read, untagged mail.
- Scans run once daily at a configurable hour (default 3:00 AM in the
  container's time zone -- set `TZ` in `docker-compose.yml`, as the example
  does, or the container runs on UTC), plus on demand: "Check email inbox" in the +
  menu (shown once email ingest is enabled), or "Scan inbox now" in
  Settings for when you don't want to wait.
- Each tagged email is tried in order: PDF attachment → photo → links in
  the body (up to three, in order, handed to the existing URL ingestion
  pipeline) → the body text itself. Every path reuses the same ingestion
  and validation code as its manual equivalent, so an emailed PDF gets
  exactly the same treatment as one uploaded through the UI.
- Photos and PDFs sent from Apple Mail count even though Apple marks them
  "inline". An inline image has to be at least 640 px on its long side to
  be treated as a recipe photo, which keeps signature logos out of OCR.
  Mail-app footers ("Sent from my iPhone", "Get Outlook for iOS" and its
  link) and anything after a `-- ` signature line are ignored.
- If an email can't be turned into a recipe, the reason lists each thing
  tried and why it gave up. It is shown under "Scan inbox now" and sent in
  the `[FAILURE]` email. Emails over 30 MB are refused before download.
- The SMTP/IMAP connection type follows the port: 465 and 993 are
  encrypted from the start, other ports upgrade with STARTTLS. "Send test
  email" checks sending and reading separately, and when a connection
  fails it probes the port and says what it found there.
- Results are reported by email: `[SUCCESS]`, `[FAILURE]`, or `[PARTIAL]`
  when a scan had both. Results within a configurable cooldown window
  (default 30 min) are batched into one message rather than sent
  individually. If sending fails, results stay queued and go out with the
  next scan rather than being lost.
- A "Send test email" button does a full round trip — sends a `[TEST]`
  message via SMTP and confirms IMAP access — and reports which specific
  step failed if something's misconfigured.
- Emailed recipes are auto-saved without a review screen (there's nobody
  at the keyboard at 3 AM) and tagged with an `Email` source badge, so
  they're easy to find and check afterward.
- **Use a dedicated email address that handles nothing else.** This is the
  recommended setup, not just a precaution: create an account or alias used
  solely for sending recipes to this app, with an app-specific password.
  The app needs credentials for whatever inbox you point it at, so pointing
  it at an account holding your real correspondence means storing
  credentials to all of it. A dedicated address limits the blast radius to
  an inbox containing nothing but recipes.
- The email password is stored encrypted, so a key has to exist first.
  Settings → Email ingest has a **Set up encryption** button that creates
  one (`encryption.key` in the data folder); no compose editing needed.
  Setting `RECIPE_APP_ENCRYPTION_KEY` yourself still works and takes
  priority — see Security notes for the difference.

**Showcase image**
- Every recipe can have one representative photo: shown below the title on
  the detail page, and as a thumbnail on each card in the list/grid view.
- Auto-captured from URL and PDF ingestion (see above), or uploaded/
  replaced/removed manually at any time from the recipe detail page —
  works the same whether the recipe originally had an image or not.
- A toolbar toggle shows or hides thumbnails across the whole list, saved
  as a preference.
- Never included in the shared PDF (see Share/print below); the exported
  HTML file and the in-app print view still include it.

**Backup & export** (Settings → Backup & export)
- Choose a format, then Download. The file is fetched in the background
  and saved from the page, so an installed iPhone app doesn't get stuck on
  a zip preview with no way back. A failed build says why instead of
  silently doing nothing.
- **Full backup (`.zip`)** — the restorable one. Contains a consistent
  snapshot of the SQLite database plus every uploaded photo and PDF, with
  a `manifest.json` and a `RESTORE.txt` explaining how to put it back.
  Restoring is: stop the app, unzip into the data folder, start it again
  (see *Moving or restoring your data* above; the same steps are in the
  app under the Download button). The app is stopped for that on purpose — replacing a SQLite file underneath a
  running process is how you corrupt it.
- The snapshot is taken through SQLite's **online backup API**, not by
  copying `recipes.db`. This matters more than it sounds: the app runs in
  WAL mode, so at any moment an unknown amount of committed data is sitting
  in `recipes.db-wal` rather than the main file. A plain file copy silently
  produces a backup missing your most recent recipes — measurably so; there
  is a regression test asserting the snapshot contains WAL-committed rows.
  The backup is also checkpointed out of WAL mode before being zipped, so
  what lands in the archive is one self-contained file.
- The photos in the archive are exactly the ones that database snapshot
  refers to, and deleting a recipe waits until they've been copied, so a
  backup never points at a photo it doesn't contain. Photos nothing refers
  to are left out; a referenced photo that's missing from disk is listed in
  `manifest.json` and logged rather than silently skipped.
- **Recipes as PDFs (`.zip`)** — one PDF per recipe, plus an `index.csv`. This is
  the archive that outlives the app: PDFs open on anything, with no Docker
  and no SQLite. It **cannot** be restored from — it's a reading copy, not a
  backup. Notes are included by default (it's your own archive, unlike the
  share export) and there's a checkbox to leave them out. A recipe that
  fails to render is recorded in the index and skipped rather than killing
  the whole archive.
- Both are built to a temp file and streamed by the server, never
  assembled in its memory — a library with a few hundred photos is larger
  than it would be sensible to hold in RAM on a NAS. (The browser does hold
  the finished file in memory before saving it; that's the price of not
  navigating away on iOS, and fine up to a few hundred MB.) Temp archives are deleted once sent, and swept if a
  download dies mid-flight.
- There is deliberately **no in-app restore button**. The app has no
  authentication, and an unauthenticated endpoint that overwrites the entire
  database is a materially worse thing to expose than one that reads it.
  Restoring is a documented two-command manual step instead.

**Organizing & finding**
- Three structured tag categories (meal type, cooking style, main
  ingredient) plus free-form custom tags. Keyword-based auto-suggestion at
  ingest time; always user-editable. Cocktail-specific cooking-style tags
  (shaken/stirred/built/blended) render as a subtab under Cooking Style.
- Full-text search (SQLite FTS5) across recipe text *and* tag names, with
  each result labeled by which one matched.
- Sort by newest/oldest, title, rating, cook time or difficulty, each
  either way; recipes with no value for the chosen field always go last.
  The choice is remembered. Sorting applies to search results too.
- "Clear filters" appears (with a count) whenever tags, a cook-time range
  or a search are active: in the toolbar, and at the top of the sidebar,
  kept apart from the tags themselves.
- Cards without a photo show just the title, no empty placeholder. Source
  and OCR-quality badges are on the recipe's own page, not on cards.
- On a phone the category sidebar is a drawer that closes when you tap
  outside it.
- Stacked tag filters (AND logic — narrows to recipes matching every
  selected tag) plus a nested cook-time filter (coarse hour buckets that
  drill down to 20-minute increments), available options generated only
  from cook times actually logged, no dead filter options.

**Rating & tracking**
- Favorite toggle, plus three independent ratings (tastiness 1–5, pace
  quick/moderate/long, difficulty easy/medium/hard) settable straight from
  each recipe card, no need to open the full recipe. On a card these open a
  sheet (a card clips its own overflow, so an inline popover would be cut
  off and would cover the title); the detail page uses an inline popover,
  where there's room. The sheet also shows the current value and offers to
  clear it.
- Optional real-world cook time (`dd:hh:mm`), logged separately from
  whatever prep/cook/total time a source states — this is what drives the
  time filter. Up to 60 days (long cures and ferments); hours under 24 and
  minutes under 60. The filter offers 20-minute steps up to a day, then
  whole days.
- Free-form notes per recipe (substitutions, timing tweaks, how it turned
  out) via a button on the recipe detail page — visually distinct once
  notes exist versus empty. Managed exclusively through `PATCH /notes`;
  deliberately not part of the full-replace `PUT /api/recipes/{id}` body,
  so a client that omits them can't silently wipe them.

**Bulk management**
- Select mode (the button at the right end of the toolbar) for
  multi-select batch delete, alongside normal single-recipe delete
  ("Delete recipe", next to "Edit recipe" at the bottom of a recipe).
- Deleting asks first. If it fails, the app says why (server error, can't
  reach the server, already deleted) and gives up after 10 seconds rather
  than leaving the dialog stuck. A batch delete reports how many were
  deleted, missing or failed, and keeps the failures selected for a retry.
- Deletion is total: the DB row and any file on disk (image/PDF) are both
  removed. Draft files from an abandoned add-recipe session (uploaded but
  never saved) are discarded when the dialog closes, with a startup sweep
  as a backstop for anything missed.

**Share/print**
- One shared HTML template renders as the in-app print view, a downloadable
  PDF (WeasyPrint), and a self-contained HTML file (images inlined as
  base64) for sharing outside your network.
- If a recipe has notes, downloading the PDF prompts whether to include
  them; when included, they're appended as a clearly labeled "Notes"
  section at the end. Notes are personal annotations, not part of the
  recipe itself, so they're opt-in per export rather than always tagging
  along — print and HTML export never include them.
- The PDF export never includes the showcase image (not a toggle — always
  excluded). The exported HTML file and the in-app print view still
  include it.

**Interface**
- PWA: installable, offline app-shell caching via a service worker.
- Light / dark / system theme (Settings menu). Dark mode is true black
  (`#000000`), not a dark-gray substitute — surface separation comes from
  hairline borders, not a lighter fill.
- Adjustable in-app text size, layered on top of normal browser zoom / OS
  text-size settings (never disabled).
- **Keep screen awake while reading a recipe** — useful when your hands are
  busy and the phone is propped on the counter. Set it as a default for
  every recipe in Settings, and/or toggle it per recipe from the detail
  view. Released automatically when you close the recipe, and re-acquired
  if you switch away and come back (browsers drop the lock when a tab is
  backgrounded and don't restore it on their own). The control is hidden
  entirely on browsers without Wake Lock support (currently Firefox;
  works in Chrome/Edge/Android and Safari 16.4+), and reflects the real
  lock state rather than what was requested — if the browser denies it
  (low battery, OS policy), the toggle shows off rather than lying.
  Browsers only allow it on a secure page: `https://`, or `localhost`.
  Opened as `http://<NAS address>:8090` the browser doesn't offer it, so
  the setting and the per-recipe toggle don't appear at all. Serving the
  app over HTTPS (e.g. through a reverse proxy with a certificate) makes
  them appear.
- Semantic HTML, ARIA labeling on icon-only controls and dialogs, visible
  focus states, focus trapping/return on modals.

## Security notes

- **No built-in authentication by default.** Every API endpoint is
  reachable by anyone who can reach the container's network address unless
  you opt into the API key below — there's no session, no login UI. This
  matches the app's intended use (a personal tool on your own LAN or behind
  your own VPN) — see the warning at the top of this file. Access control
  is your responsibility and is not optional if anything you don't control
  can reach it.
- **Optional API key**: set `RECIPE_APP_API_KEY` to require a matching
  `X-API-Key` header on every request except `/healthz`. This is
  API-level enforcement only — there's no frontend login screen in this
  version, so using it means either an API client that sends the header
  itself, or a reverse proxy configured to inject it. A reverse-proxy auth
  layer (basic auth, Tailscale, etc.) remains the more complete option for
  exposing this beyond your LAN.
- **Email credentials (if you use email ingest)** are encrypted at rest
  with Fernet (AES-128-CBC + HMAC). The key comes from one of two places:

  1. **The Set up encryption button** in Settings → Email ingest (easiest).
     It writes a random key to `encryption.key` in the data folder, readable
     only by the app user. The trade-off: the key sits next to the
     database, so anyone who can copy the whole data folder gets both. The
     backup zip leaves the key out, so a backup on its own still exposes
     nothing.
  2. **`RECIPE_APP_ENCRYPTION_KEY`** in the compose file, if you want the
     key kept out of the data folder. Generate one with the image's own
     Python, since the NAS usually doesn't have the library installed:

     ```bash
     docker exec open-the-pantry python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
     ```

     If this variable is set, it is used and any `encryption.key` is
     ignored. A value that is set but invalid is reported as an error and
     does **not** fall back to the key file. Silently switching keys would
     make saved credentials stop working with no visible cause.

  Either way, losing the key means re-entering the email password; nothing
  else is affected. The app fails closed: without a valid key it refuses
  to save a credential rather than falling back to plaintext, and the
  password field stays disabled until a key exists. The password is never
  returned by the API in any form, encrypted or otherwise — only a boolean
  indicating whether one is set.
- **Two warnings specific to email ingest**, both worth reading before
  enabling it:
  - The app has **no authentication by default** (see above). Storing
    email credentials in an app that anyone on your network can reach is
    a meaningfully bigger risk than storing recipes. If you enable email
    ingest, enabling `RECIPE_APP_API_KEY` or putting the app behind
    reverse-proxy auth stops being optional in practice.
  - Use a **dedicated email address that handles nothing else** — an
    account or alias created solely for this purpose, with an
    app-specific password. Not your primary personal account, and not a
    shared family one. The app only needs to read one inbox and send
    notifications; giving it credentials to an account holding anything
    else is unnecessary exposure. Scanning is strictly limited to
    messages whose subject contains your configured keyword — other mail
    is never parsed — but the credential itself would still grant full
    access to that mailbox if compromised, so the right move is to make
    sure that mailbox contains nothing worth taking.
- **Rate limiting** is on by default (120 API requests/minute per IP,
  configurable via `RECIPE_APP_RATE_LIMIT`) as basic abuse protection.
  Photos and the app's own files aren't counted -- they used to be, and
  scrolling a large library hit the limit. Behind a reverse proxy every
  client shares the proxy's address. It's
  in-memory and per-process — meaningful for this app's single-instance
  design, not a substitute for real infrastructure-level rate limiting if
  you're expecting real traffic.
- **Single-instance enforcement**: the app takes an exclusive lock on its
  data directory at startup and refuses to start a second instance against
  the same `./data` — SQLite doesn't support concurrent writers safely, and
  this turns a silent corruption risk (e.g. an accidental multi-replica
  deployment) into a clear startup failure instead.
- **No CSRF middleware, deliberately.** CSRF protection exists to stop a
  malicious site from riding a *victim's existing authenticated session* to
  make requests on their behalf. This app has no session or cookie-based
  auth to ride — every request is already anonymous/unauthenticated by
  design (or authenticated via a static header, which CSRF doesn't apply to
  either). Adding CSRF tokens here would protect nothing real.
- **Upload handling**: size-capped (20MB) and magic-byte validated before
  any parsing/OCR touches the file; uploads are streamed to disk rather
  than buffered fully in memory; saved filenames use an extension derived
  from the validated file content, never the client-supplied filename.
  Uploaded images are additionally re-encoded through Pillow after
  validation, discarding anything in the file that isn't actual pixel data
  (EXIF payloads, trailing bytes, other polyglot content) — passing the
  magic-byte check only proves a file *starts with* a valid signature, not
  that nothing else is appended. EXIF orientation is baked into the pixel
  data before that metadata is stripped, so a portrait photo doesn't come
  out sideways. PDFs are capped at 60 pages to bound worst-case OCR time,
  and concurrent OCR/PDF jobs are capped at 3 to avoid CPU thrashing
  between simultaneous heavy requests. Showcase images auto-captured from
  URL ingestion (downloaded) or PDF ingestion (extracted from the
  document) go through this same size-cap/magic-byte/re-encode pipeline —
  they're exactly as untrusted as a direct upload, regardless of source.
- **Search input** is treated as literal text, not query syntax — user
  search terms can't be used to alter which columns/fields get searched.
- **Stored filename fields are allow-listed.** Any API field naming a
  stored image (`image_path` on recipe create/update) must match the
  exact shape this app generates (`<prefix>-<uuid32>.<ext>`); absolute
  paths, traversal, and directory components are rejected at the schema
  layer and again in the file-handling helpers. This matters because
  `os.path.join()` silently discards its base directory when given an
  absolute path — without the check, an image field would be an
  arbitrary-file read (export embeds the target as a data URI) and an
  arbitrary-file delete (recipe deletion unlinks it), including the
  app's own database. Found in review and fixed; covered by regression
  tests in `test_security.py`.
- **A `/tmp-preview` route serves draft files** (the add-recipe review
  screen's image preview before a recipe is saved) alongside the normal
  `/uploads` route for saved recipes. Filenames are unguessable UUIDs
  either way, and draft content has already passed the same validation as
  anything in `/uploads` by the time it's servable — not a materially
  different trust boundary, just a second directory. Note this also means
  a PDF mid-ingestion is technically fetchable at this path (magic-byte
  validated, served as `application/pdf`) even though the UI never links
  to it — only the extracted image is ever referenced from the frontend.
- **URL ingestion is SSRF-guarded**: a submitted URL's resolved address is
  checked against private/loopback/link-local/reserved IP ranges before any
  request is made, and redirects are followed manually with the same check
  re-applied at every hop (rather than left to `requests`' default
  auto-follow, which would silently bypass the initial check via a 302 to
  an internal address). This is a DNS-resolve-time check, not a
  connection-time one, so it doesn't defend against DNS rebinding.
  Every page fetch goes through that guarded client. `recipe_scrapers`
  used to fetch pages itself via `scrape_me(url)`, outside the guard; it
  now only parses HTML the app has already fetched (`scrape_html`), and a
  test asserts it never makes its own request. DNS rebinding remains an
  accepted gap for a LAN tool.
- **Recipe content is HTML-escaped on export.** Titles/ingredients/steps can
  originate from arbitrary scraped web pages or OCR output — the HTML/PDF
  export template escapes all of it (Jinja2 autoescape), so injected markup
  in a source page can't execute when you open the downloaded file.

- **SQLite runs in WAL mode**, so reads (list/search/get) aren't blocked
  while a write is in progress. Two auxiliary files (`recipes.db-wal`,
  `recipes.db-shm`) live alongside `recipes.db` in `./data` as a result —
  they're part of the same bind mount, so normal volume backups already
  include them, but a raw `cp recipes.db` while the app is running can miss
  data that hasn't been checkpointed back to the main file yet; stop the
  container (or use `sqlite3 recipes.db "PRAGMA wal_checkpoint(FULL);"`)
  before copying just that one file. WAL relies on shared-memory file
  locking that isn't reliable on some network filesystems (notably NFS) —
  fine for local disk, most NAS setups (SMB/local block mounts), and
  typical Docker bind mounts, which covers this app's documented
  deployment cases.

## Known limitations

- **Email ingest needs a certificate the system trusts.** Connections
  verify the mail server's certificate and hostname. A self-signed
  certificate, or a host name that doesn't match the certificate (common
  with shared hosting, where `mail.yourdomain` points at the provider's
  server), fails with `CERTIFICATE_VERIFY_FAILED`. Use the host name your
  provider's certificate is issued for.
- **Email scans fetch each unread message's header separately.** Fine for
  a dedicated inbox; slow on a shared inbox with hundreds of unread
  messages. This is the reason the README recommends a dedicated address.

- **Some recipe sites refuse automated requests.** Sites behind
  Cloudflare's bot check (and paywalled ones such as NYT Cooking) answer
  the app with HTTP 403 and a "Just a moment..." page; the log says so
  (`otp.url`). The app doesn't try to get around that. Save the page as a
  PDF (Share → Print, pinch out, share the PDF), take a screenshot, or
  paste the text, and add or email that instead.
- **Full-page screenshots saved as PDF can be cut off.** A PDF page can't
  be taller than 200 inches, and iOS stops a long page there, so the end
  of a long blog post's recipe card may simply not be in the file. Use
  Print → PDF, which splits the page, or the site's own print button.
- Heuristic parsing (non-JSON-LD URLs, PDF/OCR segmentation) is regex/rule
  based, not ML-based — expect to correct fields on messy or non-standard
  layouts via the review screen shown after single-item ingestion. Batch
  ingestion skips that review, so check results afterward.
- Screenshot OCR confidence is surfaced as a badge (`OCR quality: low` etc.)
  based on Tesseract's per-word confidence; treat low-confidence extractions
  as a starting draft, not a finished entry.
- No handwriting recognition. This is a known Tesseract limitation, not a
  bug — see the Manual/handwritten entry path instead.
- PDF showcase-image extraction picks the largest embedded image above a
  minimum size threshold — a heuristic, not layout understanding. A PDF
  with multiple similarly-sized photos may not pick the one you'd expect;
  use the review screen's "Don't use this image" option, or replace it
  from the recipe detail page afterward.

## Logs

The app logs every step of email and link ingestion, every error it
catches anywhere in the backend (with the traceback), every request that
fails, and uncaught errors from the browser. It writes to two places:

```bash
docker logs open-the-pantry                 # since the container started
tail -f /path/to/data/logs/app.log          # survives container restarts
```

`app.log` is in the data folder next to `recipes.db`, rotated at 1 MB with
three old files kept. It's not included in the backup zip.

Useful filters:

```bash
docker logs open-the-pantry 2>&1 | grep otp.scan    # what each scan found and did
docker logs open-the-pantry 2>&1 | grep otp.email   # how each email was read
docker logs open-the-pantry 2>&1 | grep otp.url     # page fetches: status, redirects, blocks
docker logs open-the-pantry 2>&1 | grep otp.ocr     # photo orientation, OCR confidence
docker logs open-the-pantry 2>&1 | grep otp.client  # errors from the app in your browser
docker logs open-the-pantry 2>&1 | grep -A20 WARNING   # failures, with tracebacks
```

Set `RECIPE_APP_LOG_LEVEL: DEBUG` in the compose `environment:` for more
detail, or `WARNING` for less. At the default level, errors the app
recovers from routinely (cleanup of a file that's already gone, an optional
field a recipe page doesn't have) are not shown; DEBUG shows them too. Passwords, the encryption key, email bodies
and page HTML are never logged. Subjects, senders, attachment names and
sizes, and URLs are.

## Deployment notes

- The published image is a normal Docker image — it runs anywhere Docker
  runs: a home NAS, a VPS, a Raspberry Pi, behind a reverse proxy (Apache/
  Nginx/Caddy) with a domain, or over a VPN back to a home network. It does
  **not** run on shared PHP hosting without Docker/root access — that would
  require a separate rewrite, not a configuration change.
- Multi-arch image (`linux/amd64`, `linux/arm64`) via the included GitHub
  Actions workflow, so it runs on typical x86 servers and ARM boards (e.g.
  Raspberry Pi) alike.
- Database migrations run automatically at startup (`app/init_db.py`) and
  are additive — upgrading the image on an existing `./data` volume adds any
  new columns/indexes without touching existing recipes.
- The app process itself runs as a non-root user inside the container
  (fixed UID 1000 by default, or `PUID`/`PGID` env vars to match an
  existing host user). The container still starts as root briefly to fix
  ownership of `./data` on first run — needed because that's a bind-mounted
  host directory, so nothing baked into the image can set its permissions
  ahead of time — then drops privileges before running the app. No manual
  `chown` on the host is required for the default case.
- `docker-compose.yml` as given tracks `:main`, which follows the default
  branch and is the tag that is always published. `:latest` follows the same
  builds and is interchangeable. Either is convenient, but means whatever the
  tag currently points to is what you get on the next `docker compose pull`.
  For reproducible upgrades (and to control exactly when a new version
  applies), pin a version tag instead, e.g.
  `image: djerodek/open-the-pantry:1.2.0`, and bump it deliberately. Version
  tags are published by pushing a `vX.Y.Z` git tag.
- A `HEALTHCHECK` is included (hitting a dedicated `/healthz` endpoint, not
  a real-data one), so `docker ps` / orchestration tooling can tell a
  hung-but-still-listening process apart from a genuinely healthy one.
- **When releasing a new version**: bump `CACHE_NAME` in
  `frontend/service-worker.js`. The fetch strategy is network-first (falling
  back to cache only when offline), so this mostly matters for pruning old
  cached entries promptly rather than correctness — but it's still good
  practice to bump on every release.

## Testing

```bash
cd backend
pip install -r requirements-test.txt
python -m pytest
```

`pytest.ini` sets `pythonpath = .`, so test modules import the app package
directly without per-file `sys.path` manipulation.

Requires the same system packages as the Docker image (`tesseract-ocr` plus
WeasyPrint's font/rendering libraries — see the Dockerfile) since the PDF
and OCR tests exercise the real libraries, not mocks. `requirements-test.txt`
is separate from `requirements.txt` so the deployed image doesn't carry
test-only dependencies.

The suite runs against an isolated temp data directory per session (not
your real `./data`), and covers: core recipe CRUD, stacked tag filtering
(AND logic), search with match-source labeling, the nested time filter,
rating constraints, upload validation (size/magic-byte/extension-spoofing/
polyglot-payload stripping), XSS escaping on export, the SSRF guard
(including the redirect case), draft-file lifecycle, and the ingestion
heuristics (PDF text-layer detection, section segmentation, ingredient
parsing, tag suggestion).

Email-ingest tests mock the IMAP/SMTP layer (`unittest.mock`) rather than
standing up a mail server — they cover credential encryption round-trips
and fail-closed behavior, the password never being returned by the API,
each extraction path (PDF attachment, image attachment, link in body,
body text), malicious-attachment rejection, a full scan round-trip,
marking unparseable mail as seen so it isn't retried forever, and
notifications staying queued when sending fails. Real deliverability
against an actual provider is the one thing this can't verify — use the
"Send test email" button for that.

CI (`.github/workflows/ci.yml`) runs the suite plus a dependency audit and
a Docker build check on every push/PR.

## Publishing your own build to Docker Hub

1. Create a Docker Hub repo, e.g. `djerodek/open-the-pantry`.
2. Generate a Docker Hub access token (Account Settings → Security).
3. In your GitHub repo, add secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`.
4. Push to `main` (publishes `latest`) or push a tag like `v1.0.0` (publishes
   that version tag too) — `.github/workflows/docker-publish.yml` handles the
   rest.

## Project layout

```
backend/
  app/
    main.py               FastAPI app, all API routes
    models.py               SQLAlchemy models
    database.py               engine/session, SQLite + uploads/tmp paths
    init_db.py                  schema migration (isolated raw-sqlite3
                                 connection -- see comments in the file for
                                 why), FTS5 + trigger setup, tag seed data
    schemas.py                    Pydantic request/response models
    export.py                       shared HTML/PDF export rendering
    time_utils.py                     dd:hh:mm <-> minutes, time-bucket
                                       generation for the nested time filter
    file_validation.py                  shared magic-byte checks, extension
                                         forcing, image re-encoding (used by
                                         both uploads and email attachments)
    crypto.py                             Fernet encryption for stored
                                           credentials; fails closed
    email_client.py                         IMAP/SMTP primitives (stdlib only),
                                             TLS mode by port, port probe for
                                             connection diagnostics
    backup.py                               full backup (SQLite online backup
                                             API) and the PDF bundle
    logging_setup.py                        stdout + rotating data/logs/app.log
    ingestion/
      url_ingest.py                    recipe-scrapers -> JSON-LD ->
                                        heuristic HTML
      pdf_ingest.py                      per-page text-layer check, OCR
                                          fallback, heuristic segmentation
      image_ingest.py                     screenshot/photo OCR
      deskew.py                             shared OCR orientation + deskew
                                             (PDF + photo paths)
      ingredient_parser.py                  regex quantity/unit/name parsing
      tagger.py                               keyword-based auto-tagging
      email_processing.py                       tagged-email extraction:
                                                 PDF -> photo -> links -> body
    templates/
      recipe_export.html                      shared print/PDF/HTML template
  Dockerfile
  docker-entrypoint.sh   drops from root to non-root user at container start
  requirements.txt
  requirements-test.txt  pytest + httpx + piexif, not included in the deployed image
  pytest.ini             pythonpath = . (no per-file sys.path boilerplate)
  tests/
    __init__.py            makes tests a package, so `from .conftest import ...` resolves
    conftest.py            shared fixtures (isolated data dir, TestClient)
    test_time_utils.py
    test_ingredient_parser.py
    test_tagger.py
    test_pdf_ingest.py
    test_deskew.py
    test_showcase_image.py
    test_email_ingest.py
    test_api_core.py
    test_security.py
frontend/
  index.html                 topbar, sidebar, filter panel, modals
  manifest.json
  service-worker.js
  css/styles.css                design tokens incl. light/dark theme vars
  js/app.js                       all client logic (single file, no build step)
LICENSE                       MIT
DOCKERHUB.md                   paste-ready Docker Hub overview text
docker-compose.yml            pulls published image
docker-compose.build.yml       builds from source
.github/workflows/
  docker-publish.yml             builds + publishes to Docker Hub on release
  ci.yml                           tests + dependency audit + build check on every push/PR
```
