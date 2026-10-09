# Open the Pantry

*English · [Français](README.fr.md)*

Self-hosted, searchable recipe manager. Add recipes by URL, PDF, screenshot/photo,
or manual entry; browse by meal type, cooking style, or main ingredient; filter
and search; rate and favorite; print or export a clean, ad-free share card.
Interface in English or French (Quebec), chosen per device. Single Docker
image, no LLM dependency required for any ingestion path.

---

> ## ⚠️ Run this on your own network only
>
> **This application has no authentication by default.** Anyone who can reach
> its address can read, modify, and delete every recipe — and use its API.
> There is no login screen, no user accounts, and no permission model. The
> one exception is the parts that use your email account or DNS login
> (Email ingest, HTTPS, Logs and Email PDF), which need a
> [settings password](#security-notes) created in the app.
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

**Then create the settings password right away:** Settings → Email ingest
(or HTTPS, or Logs) asks for it the first time. Until it exists, the first
person on your network to open one of those sections sets it. See
[Security notes](#security-notes).

### Moving or restoring your data

Everything the app stores lives in one folder: whatever is on the left side
of the `volumes:` line in `docker-compose.yml` (`./data` by default). It
holds `recipes.db`, `uploads/` (photos and PDFs), `encryption.key` if you
set up email ingest from Settings, and `admin-password.json` (the settings
password) once it has been created. Moving the app to another drive means
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
re-enter the email password. Nothing else depends on the key. The settings
password (`admin-password.json`) isn't in the zip either: copy it across
too, or create a new one the first time you open Email ingest, HTTPS or Logs.
If the old data folder had a password and its `admin-password.created`
comes along without `admin-password.json`, creating the new one counts as a
reset and clears the email password and HTTPS. HTTPS itself (`https/`: the
certificate and the saved cPanel login) isn't in the zip: copy that folder
too, or set HTTPS up again.

To build from source instead of pulling the published image:

```bash
docker compose -f docker-compose.build.yml up -d --build
```

### Optional: HTTPS on your network

Off by default. Set up from **Settings → HTTPS**, it gives the app an
`https://` address with a real Let's Encrypt certificate, on your LAN and
over your VPN, without exposing anything to the internet. Browsers keep
some features from plain `http://` pages; for this app, "keep screen
awake" needs it.

It works with a domain whose DNS is managed in **cPanel** (most shared web
hosts). Your website stays where it is: the app adds one record for a new
name, e.g. `pantry.example.com`, pointing at this server's LAN address, and
proves to Let's Encrypt that the name is yours with a temporary record it
removes again. It renews the certificate by itself.

What has to happen outside the app, once: **create a cPanel API token**
(in cPanel, Security → Manage API Tokens → Create). Give it an **expiry
date** (a year, say; renewal stops when it expires, and Settings → HTTPS
says so). If your cPanel offers it, untick full access and allow only the
DNS feature (Zone Editor), then check that **Set up HTTPS** still works.
The token is kept inside the app's data, and the app reads files and pages
from outside (PDFs, photos, email, web pages); limiting the token limits
what anyone who ever broke into the app could do with your hosting account.

Then in **Settings → HTTPS** (it asks for the settings password):

1. Enter the **cPanel username**, the **cPanel address** (where you log in,
   with its port, e.g. `https://cpanel.example.com:2083`), the **API
   token**, and the **name to use**, e.g. `pantry.example.com`, and press
   **Save login**. The token is stored encrypted and never shown again.
   The name is the only one the app will ever create or change with it.
2. Enter this server's LAN address (filled in when you're using one) and an
   email for Let's Encrypt, and press **Set up HTTPS**. It takes a minute
   or two and shows each step. When it's done it shows the link,
   `https://pantry.example.com:8443`. On an iPhone, open that and add it to
   the Home Screen again: settings such as language and theme are kept per
   address.

The default `docker-compose.yml` already publishes port 8443 (if yours is
older, copy its `ports:` lines from the current one). No restart is needed.

**Prefer `.env`?** `CPANEL_USERNAME`, `CPANEL_TOKEN`, `CPANEL_BASE_URL` and
`PANTRY_DOMAIN` in a `.env` file next to `docker-compose.yml` still work
and win over what's saved in the app, field by field; Settings → HTTPS
shows them as "Set in .env". Installs that already use `.env` need no
change. The `env_file:` form with `required: false` in the default compose
file needs Docker Compose 2.24 or later (`docker compose version`); with an
older one, upgrade it or replace those three lines with `env_file: .env` and
keep a `.env` file there, even an empty one.

Safeguards and notes:
- The app only ever creates the record for the saved name, and never
  changes a record it didn't create. A name already in use (`www`, the
  domain itself, anything with a record) is refused, so the page can't be
  used to repoint your website.
- The name can only point at a home-network or VPN address (`10.x`,
  `172.16`–`172.31`, `192.168.x`, or Tailscale's `100.64`–`100.127`), never
  at a server on the internet.
- At most 5 certificate requests in 7 days, Let's Encrypt's own limit for
  one name; past that the page says when it can try again.
- Changing the name and setting up again removes the old name's record,
  if it still holds what the app wrote there.
- Changing the cPanel username or address without entering the token again
  clears the saved token, so the token can't be sent to an address someone
  else typed in.
- The old `http://<address>:8090` keeps working.
- For `https://pantry.example.com` with no port, change the left side of
  `8443:8443` in `docker-compose.yml` to `443` (if the machine doesn't
  already use 443 -- a NAS's own web interface often does) and set the
  port in Settings → HTTPS to 443.
- Some routers block DNS answers that point at private addresses ("DNS
  rebind protection"). If the name doesn't resolve at home, allow your
  domain there.
- The name appears in public certificate logs, as every Let's Encrypt name
  does; the private address it points to is useless from outside.
- **Other DNS hosts** (Cloudflare, OVH, Gandi and about 150 others):
  `docker-compose.https.yml` does the same with Traefik. Not for cPanel:
  running both would have two clients asking Let's Encrypt for the same
  name. Settings go in `.env` (from `https.env.example`) and the DNS
  host's API token in `traefik.env` (from `traefik.env.example`), which
  only Traefik reads; see the comments in those files. That route has no
  Settings page.
  - Traefik gets a fixed address (`172.31.250.10`; change
    `PANTRY_NETWORK_SUBNET` and `PANTRY_TRAEFIK_IP` in `.env` if that range
    is in use), and the app trusts the visitor addresses Traefik passes on
    from there only. Each visitor then gets their own request limit and
    wrong-password count.
  - **Upgrading this route from before 0056:** move the token lines from
    `.env` to `traefik.env`, then `docker compose down` and
    `docker compose up -d` once (the network's address range changes).
  - Traefik reads the Docker socket to find the app. Mounting it `:ro`
    does not make the Docker API read-only: whoever takes over Traefik
    controls Docker, which is root on the host. A socket proxy that only
    allows listing containers (e.g. `tecnativa/docker-socket-proxy` with
    `CONTAINERS=1`) closes that, if it matters on your network.

## What it does

**Ingestion**
- **URL** — tries `recipe-scrapers` (100+ site-specific parsers) first, falls
  back to `schema.org` JSON-LD, then a heuristic HTML scrape. Individual or
  batch (up to 50 URLs at once, auto-saved without per-item review). Also
  downloads the source page's showcase image (`og:image`/JSON-LD image)
  when present, through the same validated pipeline as any other image
  upload — a broken/oversized/invalid image never fails the ingestion,
  it's just skipped.
- **When a link doesn't give a whole recipe** (the site blocks the app, the
  page has no recipe the app can read, or it comes back without
  ingredients or without steps), the import counts as failed and the app
  says how to make a PDF of the page instead (Safari: Share → Options →
  PDF), with an **Add from PDF** button right there. A half recipe can
  still be opened with "Continue anyway". Batch imports list it as failed,
  and an emailed link gets a `[FAILURE]` reply saying to email the PDF as
  an attachment instead. The browser has the whole page (past any bot
  check), so its PDF usually reads cleanly.
- **When a PDF or screenshot doesn't give a whole recipe** (ingredients
  but no steps, or the reverse), it fails the same way instead of opening
  the review screen with an empty field. The usual cause is a PDF of a
  blog post that stops before the recipe card at the bottom. The app says
  what's missing and offers the other ways in: screenshots of the recipe
  card (opens "Combine multiple"), a PDF (for a screenshot), or typing it
  in, plus "Continue anyway". A batch PDF import lists it as failed.
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
- **Ingredients are kept as written.** Each line is stored exactly as
  entered or extracted, and also parsed into quantity, unit and name
  ("1½ cups flour" -> 1½ / cup / flour), including lines typed on the
  review, edit and manual screens. The recipe shows the line as written.
- Recipe pages that group their steps into sections ("For the dough",
  "For the filling") keep every step, with the section names as headings.
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
- **French recipes are understood too.** French headings (Ingrédients,
  Préparation, Étapes, Mode de préparation...), step numbers ("Étape 2"),
  units ("2 c. à soupe", "250 ml", "1,5 tasse") and accented capitals are
  read the same way as their English equivalents. OCR reads English and
  French together (the image includes Tesseract's French data); English
  screenshots come out the same as before, and OCR takes roughly half as
  long again.
- In a recipe with no headings at all, a line without a quantity ("Salt
  and pepper to taste", "Sel et poivre au goût") stays with the
  ingredients rather than becoming step 1.
- Uploads (PDF/image) are capped at 20MB and are validated by magic bytes,
  not just file extension, before any parsing/OCR is attempted.

**Email ingest (optional, off by default)**
- Email a recipe to a configured inbox with a keyword in the subject
  (default `[RECIPE]`), and Open the Pantry picks it up on its next scan.
  Everything without that keyword is ignored entirely — the app never
  attempts to parse, or even fully read, untagged mail.
- Scans run once daily at a configurable hour (default 3:00 AM in the
  container's time zone -- set `TZ` in `docker-compose.yml` to yours, e.g.
  `America/Toronto`; the example ships with `UTC`; times shown in
  Settings, such as the last scan, are in your device's time zone either
  way), plus on demand: "Check email inbox" in the +
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
- Email has no review screen, so a photo or fully scanned PDF that text
  recognition can't read reliably (confidence under 50%) is refused
  rather than saved as a recipe of garbled words; the `[FAILURE]` email
  says so and suggests a sharper photo or the typed text. Attachment names
  are decoded, so Apple Mail's `Screenshot … PM.pdf` appears as that.
  A photo, PDF, link or email text that gives only part of a recipe
  (ingredients but no steps, or the reverse) fails rather than being saved
  half done, and the `[FAILURE]` email says which part was missing and
  what to send instead.
- If an email can't be turned into a recipe, the reason lists each thing
  tried and why it gave up. It is shown under "Scan inbox now" and sent in
  the `[FAILURE]` email. Emails over 30 MB are refused before download.
- The SMTP/IMAP connection type follows the port: 465 and 993 are
  encrypted from the start, other ports upgrade with STARTTLS. "Send test
  email" checks sending and reading separately, and when a connection
  fails it probes the port and says what it found there.
- If the overnight scan can't read the inbox, or its result email can't be
  sent, Settings → Email ingest says so at the top, with the error and how
  many results are waiting to be emailed. (A broken sending setup can't
  email you that it's broken.) The warning clears after the next scan or
  test that goes through. The same errors are in `data/logs/app.log`.
- A link in an email imports the recipe but not the page's photo (the
  in-app "Add from URL" does fetch it). Add one from the recipe's page.
- Result emails are written in the language of whoever last saved the
  email settings (English or French). The `[SUCCESS]`, `[FAILURE]` and
  `[PARTIAL]` markers stay the same in both, so mail filters keep working.
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
- Settings → Recipe list → "Show photos on recipe cards" switches
  thumbnails on or off for the whole list, remembered on each device.
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
  ingest time; always user-editable. The built-in tags know French words
  as well as English ones ("au four" suggests Oven, "poulet" Chicken,
  "souper" Dinner), with or without accents. Meal words follow Quebec
  usage: déjeuner is breakfast, dîner is lunch, souper is supper. Stocks, broths and sauces ("chicken
  stock", "fish sauce") don't count as the main ingredient, though they do
  rule out Vegetarian. Suggestions apply when a recipe is added; tags on
  recipes you already have aren't changed unless you use Settings → Tags →
  "Add suggested tags to all recipes". That lists each recipe's missing
  suggestions with a checkbox per recipe and per tag (all ticked to start);
  only what's left ticked is added. It never adds a tag a recipe already
  has, or Vegetarian next to a meat or fish tag, and removes nothing.
  Unticked or removed suggestions are offered again on the next run,
  unless the recipe is set to "Skip in future scans" (a button on each row
  of the list). A skipped recipe's edit screen can include it again, and
  its "Suggest tags" button adds suggestions for that one recipe to the
  tag field any time -- skipped or not -- for you to trim before saving.
- Settings → Tags → "Manage tag groups and keywords": add groups (e.g.
  Cuisine) and tags in any group, each with the words that make the
  tagger suggest it ("Thai: lemongrass, galangal, fish sauce"). Built-in
  tags can take extra words too (Beef: bavette). A main-ingredient tag can
  be marked as meaning the dish isn't vegetarian. New groups get their own
  button next to Meal Type / Cooking Style / Main Ingredient once they have
  tags. Groups and tags added there can be deleted, which removes them from
  recipes; built-in ones can't. A tag left in a group that no longer
  exists (a database edited outside the app, say) is moved to Custom at
  startup. Cocktail-specific cooking-style tags
  (shaken/stirred/built/blended) render as a subtab under Cooking Style.
- Full-text search (SQLite FTS5) across titles, ingredients, steps, notes,
  source text *and* tag names, with each result labeled by whether it
  matched the recipe or a tag. Words match from their start ("chick" finds
  chicken) in any order, and every word must match. The ingredient and step
  text is kept in step by database triggers, so edits are searchable
  immediately.
- Sort by newest/oldest, title, rating, cook time or difficulty, each
  either way; recipes with no value for the chosen field always go last.
  The choice is remembered. Sorting applies to search results too.
- "Clear filters" appears (with a count) whenever tags, a pace, a cook-time
  range or a search are active: in the toolbar, and at the top of the sidebar,
  kept apart from the tags themselves.
- A small notes icon on a card (photo or list view) marks recipes that
  have notes.
- Cards without a photo show just the title, no empty placeholder. Source
  and OCR-quality badges are on the recipe's own page, not on cards. A recipe
  read from a web address (typed in, or a link in an emailed recipe) also has a
  **View original** link next to the source badge. It opens the page in a new
  tab or window. In an installed app, the phone decides whether that is your
  default browser or a viewer inside the app; a web page can't choose. A
  printout or export shows the address itself. For an emailed link, the
  address kept is the page the link led to, not the newsletter's tracking
  link.
  Recipes saved before this version from an emailed link have no address
  stored, so they have no link.
- The ☰ button opens "Browse by tag": the tag groups, where tapping tags
  filters the list. On a phone it's a drawer that closes when you tap
  outside it.
- Stacked tag filters (AND logic — narrows to recipes matching every
  selected tag) plus a nested cook-time filter (coarse hour buckets that
  drill down to 20-minute increments), available options generated only
  from cook times actually logged, no dead filter options.
- Pace filter (Quick / Moderate / Long, from the pace rating on each
  recipe). Picking more than one shows recipes with any of them; it
  stacks with tags, cook time and search.
- Filters apply as you tap them. Done (always at the bottom of the panel),
  the × or Escape closes the panel and keeps them.

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
- Exports use the interface language for their labels (Ingrédients,
  Préparation, Portions...); the recipe itself is exported as written.

**Email a recipe (Share → Email PDF)**
- Sends the same PDF as Download PDF, as an attachment, through the email
  account set up under Settings → Email ingest. It appears in the Share
  menu once that account has an SMTP server, a username and a saved
  password; the daily inbox scan doesn't have to be on.
- From is the email-ingest account (shown, not editable). To takes up to
  five addresses separated by commas, with an optional message, and a
  checkbox to include your notes when the recipe has any. The subject is
  "Recipe: <title>" ("Recette : <title>" in French).
- "Choose from contacts" opens the phone's contact picker where the
  browser has one (Chrome on Android). Everywhere else, addresses you've
  sent to are offered as one-tap suggestions; Settings → Email ingest →
  Recent recipients lists them, and you can remove or add addresses there.
- If sending fails, the message says why, including the same connection
  diagnosis as "Send test email".
- At most 10 recipe emails per hour across the app (see Security notes).

**Interface**
- **English or French.** Settings → Language. Defaults to the device's
  language (French if it's set to any French, otherwise English) and is
  remembered per device, so two phones in the same house can differ.
  Everything is translated: menus, messages from the server, dates, the
  exported PDF and HTML, the backup's restore instructions, and the result
  emails (see Email ingest). French is Quebec French. Built-in tags and
  groups are shown in French (Four, Souper, Mode de cuisson) but stored
  under their English names, so switching language changes nothing in
  the library; tags you create are shown as you typed them.
- PWA: installable, offline app-shell caching via a service worker.
- **Updates show up on the next open.** The app's own files are served
  with `Cache-Control: no-cache`, so the browser checks for a newer copy
  every time instead of keeping an old one for hours (iOS did). Pulling
  down at the top of the recipe list reloads the recipes, and if the
  server has a newer version of the app (`GET /api/version`), reloads the
  page into it.
- Light / dark / system theme (Settings menu). Dark mode is true black
  (`#000000`), not a dark-gray substitute — surface separation comes from
  hairline borders, not a lighter fill.
- Adjustable in-app text size (Settings), layered on top of the OS
  text-size setting. Page zoom is turned off -- pinch, double-tap, and the
  automatic zoom iOS does when you tap a small text field -- because it
  kept pushing buttons off screen; text fields are at least 16px so iOS
  has no reason to zoom.
- **Keep screen awake while reading a recipe** — useful when your hands are
  busy and the phone is propped on the counter. Optional: switch on Settings
  → Screen → "Keep screen awake when viewing a recipe" once, and every
  recipe you open keeps the screen on until you close it. Or leave it off
  and use the "Keep screen on" switch on the recipes where you want it.
  It comes back on if you switch apps and return, and the switch shows the
  real state: if the phone refuses (Low Power Mode, for example), it shows
  off. Uses the browser's Screen Wake Lock API: Chrome/Edge, Firefox 126+,
  Safari 16.4+, and iPhone home-screen apps from iOS 18.4. Browsers only
  offer it on a secure page, so it needs the app opened over `https://`
  (see *Optional: HTTPS on your network*) or as `localhost`; over plain
  `http://` the setting and the per-recipe switch don't appear. (A silent-video workaround for `http://` was tried and didn't keep
  an iPhone awake.)
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
- **Settings password for the parts that use credentials.** Settings →
  Email ingest, Settings → HTTPS, Settings → Logs and Share → Email PDF use
  your email account or the cPanel DNS token (the log holds email subjects
  and senders), so they need a password; the rest of the app has none.
  The inbox scan in the + menu stays open (it only reads the recipe inbox).
  - The password is created in the app the first time one of those is
    opened (at least 8 characters). Until then, anyone on the network who
    opens one first would be the one to create it, so open one of them
    soon after installing.
  - Entering it unlocks that browser for 15 minutes. **Lock now** and
    **Change password** are in Settings → Settings password; changing it
    locks every other device. A restart locks everything.
  - It's stored as a salted scrypt hash in `admin-password.json` in the
    data folder, never in plain text. It isn't in the backup zip, so after
    restoring you create a new one.
  - **Forgotten password:** delete `admin-password.json` from the data
    folder (no restart needed); the next visit asks for a new one. Creating
    it **clears the saved email password and cPanel token and turns HTTPS
    off**, deleting the certificate, so whoever sets the new password also
    enters those again. The email server, username and recipient list, the
    cPanel username and address, the HTTPS name and address, the recipes
    and everything else are kept. A token set in `.env` is out of the
    app's reach and stays. The first password
    on an install upgraded from before 0052 clears nothing: the app keeps
    `admin-password.created` to tell a reset from a first setup.
  - Passwords are checked one at a time. After 5 wrong ones in a row from
    one address, or 20 from all addresses together, each further try has
    to wait (30 seconds, doubling up to 15 minutes). The second limit means
    someone guessing can make you wait too; a correct password or a
    restart clears it.
  - **Once the app's own HTTPS is on,** the password and these sections
    work only at the `https://` address; over `http://…:8090`, where the
    password could be read on the network, the app links to the secure
    address instead. Requests from the server itself are exempt.
  - The full backup (Settings → Backup) stays open, like the recipes. It
    holds the email server, username, notification address, allowed
    senders and recent recipients, but no password (only its encrypted
    form, without the key).
  - The unlock is an HttpOnly, SameSite=Strict cookie: other websites
    can't use it, and the page's own scripts can't read it.
- **Optional API key**: set `RECIPE_APP_API_KEY` to require a matching
  `X-API-Key` header on every request except `/healthz`. This is
  API-level enforcement only — there's no frontend login screen in this
  version, so using it means either an API client that sends the header
  itself, or a reverse proxy configured to inject it. A reverse-proxy auth
  layer (basic auth, Tailscale, etc.) remains the more complete option for
  exposing this beyond your LAN.
- **Other websites can't use the app through your browser.** "Only my
  network can reach it" doesn't cover web pages that people on the network
  visit: such a page can send requests to the NAS from their browser. Two
  checks close that:
  - **Host names.** Requests are accepted for IP addresses, `localhost`, and
    local-style names (no dot, or ending `.local`, `.lan`, `.home.arpa`,
    `.internal`). Any other name -- e.g. `pantry.example.com` behind a
    reverse proxy, or a Tailscale MagicDNS name like `nas.tail1234.ts.net`
    -- must be listed in `RECIPE_APP_ALLOWED_HOSTS` (comma-separated) in
    the compose file, or requests for it get a 400 that says so. This
    blocks DNS rebinding, where a site points its own domain at your NAS's
    address.
  - **Writes need an `X-Requested-With` header.** Every request that
    changes data must carry it; the app's pages add it automatically. A
    page on another site can't add a custom header without the browser
    asking the app first, and the app never agrees. Scripts that call the
    API directly need to send it too (any value).
- **Share → Email PDF sends from your email account**, so it needs the
  settings password. It's also capped: at most five recipients per email
  and 10 emails per hour. It's only offered once the email-ingest account
  has a saved password.
- **Settings → HTTPS keeps a cPanel API token.** It's entered on that page
  (behind the settings password) or in `.env`, stored encrypted with the
  same key as the email password in `data/https/cpanel-login.json`, never
  shown again, never in a backup and never served. Changing the cPanel
  username or address without entering it again clears it. A settings
  password reset clears it too (one set in `.env` stays: the app can't
  change that file). Even with the password, the page works only for the
  saved name: set it up again (at most 5 certificate requests a week),
  point it at another address on your network, or turn HTTPS off. It can't
  create any other name, point the name at the internet, or change or take
  over a record it didn't create (so not your website's), and the
  certificate's key stays on the server. But the token is inside the
  app's container, so anyone who got code running there would have the
  token itself, with everything it allows: give it an expiry date and,
  where cPanel allows it, DNS access only (see *Optional: HTTPS on your
  network*).
- **Changing the email server or username clears the saved password**
  unless a new one is entered in the same save. Otherwise anyone who can
  open the email settings could point them at their own server and press
  "Send test email" to receive the password.
- **Email from strangers can be ignored.** Settings → Email ingest → "Only
  accept email from" takes addresses and domains, with or without the
  leading `@` (`example.com` and `@example.com` are the same entry). Other
  senders' tagged emails are marked read and listed as IGNORED in the scan
  results. Empty means anyone, as before. The From header can be forged,
  so this keeps out people who stumble on the address and keyword -- a
  dedicated, unguessable address still matters.
- **The log is readable in Settings → Logs** with the settings password
  (and from the host: `docker logs`, `data/logs/app.log`). It holds email subjects and senders and
  the URLs of recipes added, never passwords, keys, email bodies or page
  contents. The app's offline cache never keeps a copy of it.
- **Page downloads are bounded:** 10 MB per page (20 MB for photos), 30
  seconds in total including redirects even against a server that trickles
  data slowly rather than going silent outright, and only public internet
  addresses (anything not globally routable is refused, including the
  `100.64.0.0/10` range Tailscale uses). Scanned PDF pages and uploaded
  photos are rendered/upscaled for OCR within a 35-megapixel budget, so an
  extremely tall or narrow page or image can't exhaust memory.
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
  - The email settings are behind the settings password, but the rest of
    the app has **no authentication** (see above), and anyone who can reach
    the app can try passwords, slowly. Pick a password that isn't used
    anywhere else. If people you don't trust can reach the app, put it
    behind a VPN or an authenticating reverse proxy anyway.
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
- **No CSRF tokens; cross-site requests are blocked another way.** CSRF
  tokens stop a malicious site from riding a victim's logged-in session,
  and this app has no session or cookie to ride. The risk that does apply
  -- a page on another site sending requests to the NAS from a browser on
  your network -- is handled by the host-name check and the required
  `X-Requested-With` header described above, not by tokens.
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

- **Settings → HTTPS supports cPanel only.** Other DNS hosts can use the
  Traefik file instead (see *Optional: HTTPS on your network*).
- **The language setting translates the app, not your recipes.** A recipe
  added in English stays in English in the French interface, and the
  reverse. Tag and group names you create are shown as typed.
- **"Choose from contacts" is Android-only for now.** The Contact Picker
  API exists in Chrome on Android; Safari on iOS doesn't offer it to web
  apps, so on an iPhone you type the address once and pick it from the
  recent list after that.

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
  the app with HTTP 403 and a "Just a moment..." page; the app says the
  site refused and what to do instead, and the log records it in one line
  (`otp.url`). The app doesn't try to get around that. Save the page as a
  PDF (iPhone Safari: Share → Options → PDF; elsewhere Print → Save as
  PDF), take screenshots, or paste the text, and add or email that
  instead.
- **Full-page screenshots saved as PDF can be cut off.** A PDF page can't
  be taller than 200 inches, and iOS stops a long page there, so the end
  of a long blog post's recipe card may simply not be in the file. Use
  Print → PDF, which splits the page, or the site's own print button. A
  PDF cut off before the steps is refused with that advice (see
  Ingestion).
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

**Settings → Logs** (needs the settings password) shows the log in the app: the newest 500 entries,
everything or only warnings and errors (a traceback stays with its entry),
plus **Download** (the whole log, all rotated files, as one `.txt`) and
**Clear log** (empties it, after a confirmation). Kept to about 4 MB: the
oldest entries drop off by themselves, so clearing is never required.

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

**Resource limits.** The app bounds its own work (at most three OCR/PDF
jobs at once, 35 MP per page, batch size caps), but Docker doesn't cap the
container. Measured worst case is about 3.4 GB (three large scans at once);
idle is about 160 MB. The compose files have commented-out `mem_limit`,
`cpus` and `pids_limit` lines if you want a hard ceiling.

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
  tags are published by pushing a `vX.Y.Z` git tag
  (`git tag v1.0.0 && git push origin v1.0.0` gives `:1.0.0` and `:1.0`).
  The base image (`python:3.12-slim`) is deliberately not pinned: each
  build picks up Debian security updates, and the CI dependency audit fails
  the build on a known vulnerability.
- A `HEALTHCHECK` is included (hitting a dedicated `/healthz` endpoint, not
  a real-data one), so `docker ps` / orchestration tooling can tell a
  hung-but-still-listening process apart from a genuinely healthy one.
- **When releasing a new version**: bump `CACHE_NAME` in
  `frontend/service-worker.js`. The fetch strategy is network-first (falling
  back to cache only when offline), and the app's own files are fetched
  with revalidation, so this mostly matters for pruning old cached entries
  promptly rather than correctness — but it's still good practice to bump
  on every release.

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
parsing, tag suggestion), French coverage (every interface string has a
French version), and Settings → HTTPS end to end against a fake cPanel
API and a test certificate authority.

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
5. The same workflow copies `DOCKERHUB.md` into the Docker Hub repository's
   Overview on each push to `main`. That needs a token with the **Read,
   Write, Delete** scope (Docker Hub refuses description updates from a
   Read & Write token). With a narrower token the step fails on its own and
   the image is still published; paste `DOCKERHUB.md` into Repository →
   Overview by hand instead.

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
    i18n.py                                 server messages, exports and
                                             result emails in French
    https_setup.py                          Settings -> HTTPS: cPanel DNS
                                             records, Let's Encrypt, the
                                             HTTPS listener and renewal
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
      completeness.py                         a whole recipe needs ingredients
                                               and steps (PDF/photo/email)
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
    test_*.py              one file per area (test_api_core, test_security,
                           test_pdf_ingest, test_email_ingest,
                           test_https_setup, ...) plus one per review round,
                           each test tied to the problem it guards against
frontend/
  index.html                 topbar, sidebar, filter panel, modals
  manifest.json
  service-worker.js
  css/styles.css                design tokens incl. light/dark theme vars
  js/i18n.js                      English/French: language choice and the
                                  French text for every interface string
  js/app.js                       all client logic (single file, no build step)
LICENSE                       MIT
DOCKERHUB.md                   Docker Hub overview (synced by docker-publish.yml)
docker-compose.yml            pulls published image
docker-compose.build.yml       builds from source
docker-compose.https.yml       optional HTTPS via Traefik, for DNS hosts other than cPanel
https.env.example              settings template for the above (copied to .env)
traefik.env.example            DNS API token template for the above (copied to traefik.env, Traefik only)
.github/workflows/
  docker-publish.yml             builds + publishes to Docker Hub on release
  ci.yml                           tests + dependency audit + build check on every push/PR
```
