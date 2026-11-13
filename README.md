# Open the Pantry

*English · [Français](README.fr.md)*

A self-hosted recipe manager. You can add recipes from a link, a PDF, a photo or screenshot, an email, or by typing them in. Browse them by tag, search, rate and share them. The interface is in English or French (Quebec). It runs as a single Docker image and needs no AI service.

> ## ⚠️ Run this on your own network only
>
> **There is no login.** Anyone who can reach the app can read, change and
> delete every recipe. The only exceptions are the parts that use your
> email account or DNS login, and restoring a backup. Those need a
> [settings password](#settings-password) created in the app.
>
> **Don't expose it to the internet.** Use it on your LAN, or from outside
> through a VPN (WireGuard, Tailscale…) or a reverse proxy that does its own
> authentication. A serious file-access bug was found and fixed late in
> development, after several reviews had called the code clean. Others may
> exist. The network boundary is the real protection.

> **Unmaintained.** Built with AI coding tools for personal use and
> published as-is: no support, no commitment to fixes. Forks are welcome.

## How it differs

Compared with Mealie and Tandoor, as their documentation stood in October
2026:

- **Photos, screenshots and PDFs are read without an AI model**: OCR and
  rules on your server, with no API key and no GPU. Mealie's photo import
  needs an image-capable AI provider; Tandoor reads photos and PDFs through
  an AI model you connect. A model may read a messy photo better; the
  [import corpus](backend/tests/README.md#import-corpus) measures how this
  app does on your own recipes.
- **It learns from your corrections** (Settings → Learning): how your
  imports are laid out and how you tag, used only where that beats the
  built-in rules on your recipes.
- **English and Quebec French**, in the interface and in what it reads.
- **Recipes by email**, sent to an inbox the app checks.
- **What to eat?**, a read-only page that picks a random recipe from your
  choices.
- **Your own measures**: tastiness, pace, difficulty, and the real cook
  time you logged, all usable as filters.
- **One container**, SQLite, no accounts.

**Not for you if** you want meal planning, shopping lists, nutrition, or
several users with their own accounts. Mealie and Tandoor have those.

## Run it

You need Docker with Compose 2.24 or later (`docker compose version`).

```bash
curl -O https://raw.githubusercontent.com/djerodek/open-the-pantry/main/docker-compose.yml
docker compose up -d
```

Open `http://<server>:8090`. Then **create the settings password right
away**: open Settings → Email ingest, HTTPS or Logs, and it asks for one.
Until a password exists, the first person on your network to open one of
those sections sets it.

- **Build from source** instead: `docker compose -f docker-compose.build.yml up -d --build`.
- **Updates**: `docker compose pull && docker compose up -d`. The database
  is upgraded at start-up, without touching your recipes.
  - `:main` follows every build, and is rebuilt every Sunday. To follow it,
    run a weekly cron job after that (e.g. Sunday 05:00):
    `cd /path/to/open-the-pantry && docker compose pull -q && docker compose up -d && docker image prune -f`.
    With HTTPS, give both compose files to each command.
  - To decide when to upgrade, pin a digest instead:
    `image: djerodek/open-the-pantry@sha256:<digest>`
    (`docker image inspect --format '{{index .RepoDigests 0}}' djerodek/open-the-pantry:main`).
- **Images**: `linux/amd64` and `linux/arm64`. The ports are published on
  every address of the machine; on one with a public address, put them
  behind a firewall or VPN (Docker's published ports bypass ufw). The app
  runs as UID 1000 (or `PUID`/`PGID`) and fixes the data folder's
  ownership at start.
- **Memory**: about 160 MB idle, up to about 3.4 GB with three large scans
  at once (commented-out `mem_limit`/`cpus` lines are in the compose file).
- **Time zone**: `TZ` (ships as `UTC`) sets the hour of the email scan and
  the automatic backups.

### Your data

Everything lives in one folder, the left side of the `volumes:` line
(`./data` by default):
- `recipes.db`: the database;
- `uploads/`: photos and PDFs;
- `encryption.key`: the key for the email password and cPanel token;
- `admin-password.json`: the settings password;
- `https/`: the certificate and saved cPanel login;
- `logs/`: the log files;
- `ml/`: learned models, rebuilt from your recipes;
- `backups/`: automatic backups, unless you chose another folder.

**To move it to another disk:**
1. Run `docker compose down`. Don't copy while the app runs: the database
   may be mid-write.
2. Copy the folder: `cp -a ./data /new/path`.
3. Change only the left side of the volume line: `- /new/path:/app/data`.
4. Run `docker compose up -d`. Check your recipes are there before you
   delete the old folder.

**To restore a full backup:** use Settings → Backup & export → Restore or
import (see [Backup and restore](#backup-and-restore)). If the app won't
start, do it by hand:
1. Run `docker compose down`.
2. Delete the database's journal files, which would otherwise be applied
   to the restored database: `rm -f ./data/recipes.db-wal ./data/recipes.db-shm`.
3. Unzip into the data folder: `unzip -o open-the-pantry-backup-*.zip -d ./data`.
4. Run `docker compose up -d`.

A backup doesn't contain `encryption.key`, `admin-password.json` or `https/`.
On a new install, either copy those across from the old folder, or set up
email and HTTPS again and create a new settings password.

### Optional: HTTPS on your network

HTTPS is off by default. **Settings → HTTPS** gives the app an `https://`
address with a real Let's Encrypt certificate, for your LAN and VPN only;
nothing is exposed to the internet. "Keep screen awake" and installing the
app on Android need it.

It works with a domain whose DNS is managed in **cPanel**: the app creates
one record for a new name (e.g. `pantry.example.com`) pointing at your
server's LAN address, proves ownership with a temporary record, and renews
the certificate by itself.

**Setting it up:**
1. In cPanel, create an API token (Security → Manage API Tokens). Give it
   an expiry date. If your cPanel allows it, limit it to DNS (Zone Editor).
2. In Settings → HTTPS (which asks for the settings password), enter:
   - the cPanel username;
   - the cPanel address with its port, e.g. `https://cpanel.example.com:2083`;
   - the token;
   - the name to use.

   Press **Save login** (the token is stored encrypted, never shown again).
3. Enter the server's LAN address and an email for Let's Encrypt, then
   **Set up HTTPS**. After a minute or two it shows the link, e.g.
   `https://pantry.example.com:8443`. On an iPhone, add it to the Home
   Screen again: language and theme are kept per address.

Port 8443 is already published and no restart is needed. For `https://`
with no port, publish `443:8443` and set the port to 443 in Settings (if
nothing else uses 443). `http://…:8090` keeps working.

**Safeguards:**
- Only the saved name's record is ever created or changed, and a name
  already in use is refused, so your website can't be repointed.
- The name can only point at a private or VPN address (`10.x`,
  `172.16`–`172.31`, `192.168.x`, Tailscale's `100.64`–`100.127`).
- At most 5 certificate requests a week (Let's Encrypt's limit).
- Changing the cPanel username or address without re-entering the token
  clears it.
- If the name doesn't resolve at home, your router may block DNS answers
  pointing at private addresses ("DNS rebind protection"): allow your
  domain there.

`CPANEL_USERNAME`, `CPANEL_TOKEN`, `CPANEL_BASE_URL` and `PANTRY_DOMAIN` in a
`.env` file still work, and take priority over what's saved in the app. A
token in `.env` is stored as plain text, so keep the file private; it's only
used with `CPANEL_BASE_URL` from `.env` too.

**Other DNS hosts** (Cloudflare, OVH, Gandi…): `docker-compose.https.yml`
does the same job with Traefik. Don't use it alongside the cPanel route.
- Put the settings in `.env` (from `https.env.example`) and the DNS host's
  token in `traefik.env` (from `traefik.env.example`); only Traefik reads
  that file. The comments in those files explain each setting.
- On this route the settings password works only at the `https://` address.
  If Traefik is down, those sections stay closed on purpose.
- Traefik reads the Docker socket, which gives it control of Docker. A
  socket proxy that only allows listing containers closes that, if it
  matters on your network.

### Optional: What to eat? (a random recipe)

This is off by default. **What to eat?** is a small read-only page on its
own port, 8091. Pick choices if you want (meal type, cooking style, main
ingredient, pace, language) and press **Pick one**: titles roll past and it
stops on a matching recipe. **Something else** picks again, with no repeats
until every match has been shown (up to the latest 500). Tap the card to
read or print the recipe, with the same scaling and units buttons as the
main app; **Edit in Open the Pantry** opens it there.

- Within a group the choices are alternatives (Dinner or Lunch); across
  groups, all must match.
- Better-rated recipes come up a little more often (5★ is 1.5 times as
  likely as 1★). Cocktails are never picked.
- Language shows when your recipes are in both; a wrong guess is fixed
  under Language in Edit recipe.
- Choices are remembered on each device; it has its own language switch.

**Turn it on** in Settings → What to eat?. It starts at once, and a link
appears in the top bar.

**If port 8091 is already taken on your machine**, publish another port, e.g.
`"9000:8091"`, and set `RECIPE_APP_PICKER_PUBLIC_PORT: "9000"`. The comments
in `docker-compose.yml` show where. Behind a proxy, use
`RECIPE_APP_PICKER_PUBLIC_URL` and `RECIPE_APP_PUBLIC_URL` (the main app's
address) so the two pages link to each other correctly.

**It has no login, and `RECIPE_APP_API_KEY` doesn't cover it.** Anyone who
can reach the port can read every recipe it can pick, your notes included.
It is plain `http://` only, and nothing can be changed from it. If that's
more than you want, leave it off.

## What it does

### Adding recipes

- **From a link.** The app tries `recipe-scrapers` (100+ sites), then the
  page's schema.org data, then the page itself. It also keeps the page's
  photo. You can add one link, or up to 50 at once.
- **From a PDF.** Pages with text are read directly. Scanned pages go
  through OCR, turned upright and straightened first. The largest photo in
  the PDF becomes the recipe's photo. You can add one file, or up to 20 at
  once.
- **From a photo or screenshot.** OCR reads it, after turning it upright
  and straightening it; a page photographed on a table is cropped from it
  first. Coloured titles and two-column ingredient lists are read too.
  **Combine multiple** joins up to 10 screenshots into one recipe. OCR doesn't read handwriting: type those recipes in.
- **Typed in.** Fields for everything, plus a photo. In the Ingredients or
  Steps box, a short line ending in ":" starts a section, e.g. "Sauce:".
- **Shared from another app.**
  - Android, once the app is installed from the browser (this needs HTTPS):
    the app appears in the share sheet.
  - iPhone: create a Shortcut that receives **URLs** from the **Share
    Sheet**, with two actions:
    1. **URL Encode** on the Shortcut Input.
    2. **Open URLs**: `http://<server>:8090/?share_url=` followed by the
       encoded text.

  Either way, the link is filled in, but nothing is imported until you
  press **Fetch recipe**.
- **By email**: see [Email ingest](#email-ingest-optional).
- **Review before saving.** A single recipe opens a review screen, the
  fields beside the text that was read. Batches, emails and imports are
  saved as *To review* (listed in the side menu); **Looks right** clears
  the mark.
- **Incomplete recipes are refused** (ingredients but no steps, or the
  reverse), with what's missing and another way in, usually saving the
  page as a PDF (Safari: Share → Options → PDF). **Continue anyway** opens
  what was read.
- **Already in your library?** A matching link or title is pointed out,
  with a link to the saved recipe; Save again adds it anyway. Batches and
  emails skip a link that's already saved.
- **French recipes** are read the same way as English ones: headings, step
  numbers, units ("2 c. à soupe", "1,5 tasse"), and OCR in both languages.
- **Each ingredient line is kept as written**, and is also parsed into
  quantity, unit and name.

### Email ingest (optional)

Send a recipe to an inbox you've set up, with a keyword in the subject
(`[RECIPE]` by default), and the app picks it up:
- once a day at the hour you choose;
- or on demand, with **Check email inbox** in the + menu.

The app tries, in order:
1. a PDF attachment;
2. a photo;
3. links in the body;
4. the text itself.

Mail without the keyword is never read. A scan missed at its hour (the
container was down) runs later the same day.

- **Results** are emailed back as `[SUCCESS]`, `[FAILURE]` or `[PARTIAL]`,
  grouped within a cooldown window. A failure lists what was tried, and why
  each attempt gave up. Results that couldn't be sent wait for the next
  scan.
- **Unreadable mail is refused** and marked read: a photo or scanned PDF
  OCR can't read reliably, mail over 30 MB, or mail nested too deeply.
- **Allowed senders.** "Only accept email from" limits which addresses or
  domains can add recipes. Other senders are refused from the From line,
  before the mail is downloaded. A From line can be forged: this filters
  mail, it doesn't prove who sent it.
- **Send test email** checks sending and reading separately, and says what
  it found on the port if a connection fails.
- **Use a dedicated email address**, with an app-specific password, that
  receives nothing else. The app stores that account's password, encrypted.
  **Set up encryption** in Settings creates the key.

### Finding recipes

- **Tags** come in three built-in groups (meal type, cooking style, main
  ingredient), plus your own groups and Custom tags.
  - Tags are suggested from keywords in English and French (Quebec meal
    words: déjeuner is breakfast, dîner lunch, souper supper). Settings →
    Tags manages groups, tags and keywords, and can tag every recipe.
- **Learning** (Settings → Learning, on by default) trains two small models
  on your corrected recipes, one for reading imports and one for tags, each
  used only where it beats the built-in rules (Settings shows the scores;
  the import model needs a 5-point lead, and never flattens a recipe's parts).
  Recipes still *To review* aren't learned from. The models (`data/ml/`)
  are rebuilt automatically and aren't in backups.
- **Search** covers titles, ingredients, steps, notes, original text and
  tags. Words match from their start ("chick" finds chicken), and every word
  must match. Accents and capitals don't matter.
  - **Typos are forgiven** ("chiken" finds chicken, for words of four
    letters or more); the page says what it searched for.
  - **Leave words out** with a minus: `chicken -curry` ("-egg" keeps
    eggplant).
  - **Best match first**: the title counts most, then tags and ingredients.
    When the match isn't in the title, the card shows where, highlighted.
- **Filters**: stack tags (a recipe must have all of them), pace, and the
  cook time you logged. **Sort** by date, title, rating, cook time or
  difficulty. **Clear filters** resets everything.

### A recipe

- **Ratings** come straight from the card: favourite, tastiness 1–5, pace,
  difficulty.
- **Servings** and the **real-world cook time** you logged (`dd:hh:mm`, up to
  60 days). The cook time drives the time filter.
- **Notes**: your own text. They're kept out of shares unless you ask.
- **Photo**: added from the source, uploaded, replaced or removed.
- **View original** opens the page the recipe came from.
- **Scale with ½ / 1× / 2×.** The amount at the start of each ingredient
  line is rewritten in the style it was written in ("1 1/2 cups" → "3
  cups", "2-3" → "4-6"); the unit and the servings follow. Nothing is saved.
- **As written / Weights / Volumes**, after any scaling, from a table of
  about 220 ingredients (`frontend/js/weights.js`); the choice is kept on
  the device. **Weights** shows volumes as grams ("1 cup flour" → "120 g
  flour"); **Volumes** shows weights as cups and spoons in the recipe's
  language ("30 g de beurre" → "2 c. à soupe de beurre"; a French tasse is
  250 ml). A note under the buttons says conversions are approximate.
- **Lines it can't handle safely stay as written**, marked: "1,000 g" (a
  thousand, or one?), two amounts on one line ("1 lb 8 oz"), and
  ingredients with no reliable figure (kosher salt, spice blends, pastes,
  meat, cooked or whipped forms). Amounts in the steps aren't changed.
- **Keep screen awake** while a recipe is open, on every recipe (Settings)
  or on one. This needs HTTPS.

### Managing the library

- **Edit** any recipe, with the original text alongside. **Duplicate**
  makes a variant with its own copy of the photo.
- **Select mode** (the button at the right of the toolbar) works on several
  recipes at once:
  - **Delete** them;
  - add or remove one tag (**Tags…**);
  - with exactly two selected, **Merge…**. You choose where each part comes
    from: title, photo, ingredients and steps, servings and times, link,
    ratings, notes. Tags from both are kept. The other recipe goes to
    Recently deleted. A merge that would give more than 50 tags, or notes
    over 50,000 characters, is refused with the reason.
- **Recently deleted** (side menu) keeps a deleted recipe for 30 days, to
  put back or delete now; then it's deleted for good, with its photo.
- **Settings → Library check** lists recipes missing ingredients, steps,
  tags or a photo (or whose photo file is gone), still to review, or that
  look like the same one.

### Sharing

From a recipe's **Share** menu, or a card's share button:
- **Print**;
- **Download PDF**: it asks whether to include your notes, and never
  includes the photo;
- **Download HTML**: one self-contained file, with the photo;
- **Download JSON**: schema.org format;
- **Image (JPG)**: one 1080 px picture drawn in the browser, notes
  optional; on a phone over HTTPS, **Share…** opens the share sheet;
- **Copy as text**;
- **Email PDF**: from the email-ingest account to up to five addresses (10
  emails an hour), with the settings password.

Exports use the interface language for their labels. The recipe itself is
exported as written.

### Backup and restore

In Settings → Backup & export:
- **Full backup** (`.zip`): the database and every photo and PDF, plus
  restore instructions. This is the one you can restore from. It is taken
  with SQLite's backup API, so it's consistent while the app runs.
- **Recipes as PDFs** (`.zip`): one PDF per recipe, plus an index. It's a
  reading copy that opens anywhere, and can't be restored from. Notes are
  included unless you untick them.
- **Recipes as JSON** (`.zip`): schema.org files with photos, for other
  recipe apps. This app's own fields are kept, so importing the file back
  here loses nothing.
- **Automatic backups**: a full backup every day at the hour you choose.
  The newest ones are kept (7 by default); **Back up now** makes one
  immediately. A failed one is tried again every hour until one works.
  - They go to `data/backups/`. For a real second copy, mount a folder on
    another disk and set `RECIPE_APP_BACKUP_DIR`; the compose file has the
    lines, commented out.
  - Only the app's own backup files are ever deleted there.
  - Changing the schedule needs the settings password.
- **What a backup leaves out**: the email login and password, subject
  keyword, allowed senders, notification address and recent recipients,
  and the HTTPS name and addresses. Anyone on your network can download a
  backup, so these never go in one. The certificate and cPanel login
  (`data/https/`) aren't in any backup.
- **Restore or import** takes one file and handles it by type.
  - **A full backup replaces the library** (settings password, then a check
    and a confirmation; no restart). A safety copy of the current library
    is written first (`restore-safety-<date>.zip`, settings included), so
    restoring it undoes the restore. This server keeps its HTTPS setup,
    backup schedule and email settings; a new install takes the backup's
    mail server names and ports.
  - If a restore fails partway, the app stays closed (503) until it's
    restarted, and finishes the restore at start-up, this server's own
    settings included (kept in `restore-kept.json` meanwhile). If it can't tell
    whether the library was replaced, the log names the folders to check.
  - **Anything else adds its recipes**: schema.org `.json`, a `.zip` of
    them, or a Paprika export (`.paprikarecipes`). A recipe whose link is
    already saved is skipped.

### Interface

- **English or French (Quebec)**, chosen per device. This covers the
  interface, exports, result emails and built-in tags. Your recipes and own
  tags stay as written.
- **Installable** as an app (PWA). Pull down on the list to refresh it and
  pick up a new version.
- **Light or dark theme.** Dark is true black.
- **Text size** up to 26px.
  - Pinch-zoom is off on phones and tablets, on purpose: it pushed buttons
    off screen.
  - On a computer, browser zoom works as usual.
- On wide screens, a recipe shows the ingredients beside the steps.
- **Accessibility**: labelled controls, keyboard and screen-reader support.
  An automated scan checks every screen (see [Development](#development)).

## Security

### Settings password

It guards everything that uses outside credentials:
- Email ingest, HTTPS and Logs;
- Share → Email PDF;
- restoring a backup, and changing the automatic-backup schedule.

How it works:
- **Creating it.** The first time one of those is opened, you create it
  (8+ characters). Do this right after installing.
- **Unlocking.** Entering it unlocks that browser for 15 minutes, with an
  HttpOnly cookie. **Lock now** and **Change password** are in Settings.
  Restarting the container locks every browser.
- **Wrong passwords.** After 5 wrong tries from one address, or 20 from all
  addresses together, each new try has to wait (up to 15 minutes).
  Restarting the container clears that. Someone on your network can keep
  Settings waiting this way, even for the right password; the restart is
  the way out.
- **Once HTTPS is on**, the password works only at the `https://` address.
- **Storage.** It's stored as a salted scrypt hash in `admin-password.json`.
- **Forgotten?** Delete `admin-password.json`, then create a new password.
  Creating it clears the saved email password and cPanel token, and turns
  HTTPS off. Everything else is kept. A damaged file isn't taken for "no
  password": Settings says so, and deleting it is the way to reset.

### Everything else

- **The rest of the app is open to anyone who can reach it.**
  `RECIPE_APP_API_KEY` makes every request need an `X-API-Key` header. The
  pages don't send that header, so it's only useful behind a proxy that
  adds it. An authenticating proxy or a VPN is the better answer.
- **Other websites can't use the app through your browser.**
  - Requests must use an IP address, `localhost`, or a local name (`.local`,
    `.lan`, `.home.arpa`, `.internal`, or no dot). Any other name (a proxy
    domain, a Tailscale name) has to be listed in `RECIPE_APP_ALLOWED_HOSTS`.
    `RECIPE_APP_ALLOWED_HOSTS=*` accepts any name, which turns this check
    off: other websites could then reach the app through your browser (DNS
    rebinding). List the names instead; the log warns at start-up.
  - Requests that change data must carry an `X-Requested-With` header.
  - The pages can't be shown inside another site.
  - A Content-Security-Policy allows only the app's own scripts and
    connections. HSTS isn't sent, because it would break the `http://`
    ports.
- **Everything that comes in is treated as hostile.**
  - **Uploads**: at most 20 MB, checked by their content rather than their
    file extension. A request is cut off as soon as it passes its limit
    (20 MB a file, 1 GB an import, 16 GB a full backup with the settings
    password, 32 MB anything else, each plus 1 MB for the form itself), so no
    more than that reaches the disk.
  - **Images** are re-encoded, which drops metadata and anything hidden in
    them. Only JPEG, PNG, GIF and WEBP decoders are used, and anything over
    50 megapixels is refused.
  - **PDFs**: at most 60 pages, and at most 3 OCR jobs at once. From a PDF's
    photos, only the largest is decoded.
  - **Archives** (backups, imports): at most 20,000 files, checked before
    the file list is read, and size-limited when unpacked.
  - **Downloaded pages**: at most 10 MB and 30 seconds in all, from public
    addresses only; every redirect is checked again.
  - **Outgoing connections** (mail, links, cPanel, Let's Encrypt) check the
    certificate against the image's current roots, and refuse chains
    through known-compromised CA keys (eMudhra, October 2026).
  - **Exports** escape all recipe text; titles in `index.csv` can't run as
    spreadsheet formulas.
  - **Stored file names** must have the exact shape the app generates.
  - The automatic API documentation pages (`/docs`, `/openapi.json`) are
    off.
- **Credentials.** The email password and cPanel token are encrypted
  (Fernet) with `encryption.key`, or with `RECIPE_APP_ENCRYPTION_KEY`, which
  keeps the key out of the data folder (generate one with
  `docker exec open-the-pantry python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
  The API never returns the password; changing the email server or username
  clears it unless you enter it again; backups leave the key out.
- **The cPanel token** can only manage the one saved name through the app,
  but code running in the container would have the token itself: give it
  an expiry date and, where cPanel allows it, DNS access only.
- **Rate limit**: 120 API requests a minute per address (set
  `RECIPE_APP_RATE_LIMIT` to change it). The What to eat? port shares the
  same per-address request limit. Behind a proxy, all clients share the
  proxy's address.
- **One instance only.** The app locks its data folder, so a second copy
  pointed at the same folder refuses to start.
- **Networked folders.** SQLite runs in WAL mode, which isn't reliable on
  NFS. Use a local disk, or a NAS's local volume.

## Known limitations

- **Settings → HTTPS supports cPanel only.** Other DNS hosts can use the
  Traefik file.
- **Some sites refuse automated requests.** That includes sites behind
  Cloudflare's bot check, and paywalled sites. Save the page as a PDF, take
  screenshots, or paste the text instead.
- **Long screenshots saved as PDF can be cut off.** iOS stops a page at 200
  inches. Use Print → PDF, or the site's print button.
- **Parsing PDFs and photos follows rules** until Learning has enough of
  your corrections. Check recipes on the review screen, or under *To
  review*.
- **The recipe's photo from a PDF** is its largest image, which may not be
  the one you want. You can replace it.
- **No handwriting recognition.** Type those recipes in.
- **Email ingest needs a certificate the system trusts.** Use the host name
  your mail provider's certificate is issued for. It's also slow on a
  shared inbox with hundreds of unread messages, which is one more reason
  to use a dedicated address.

## Logs

**Settings → Logs** (needs the settings password) shows the newest entries,
all of them or only warnings. You can download the whole log, or clear it.
The log is also in `docker logs open-the-pantry` and `data/logs/app.log`
(rotated at 1 MB, 3 old files kept).

It records:
- every step of link and email imports;
- every error, with its traceback;
- errors from the pages.

It never contains passwords, keys, email bodies or page contents. Email
subjects and senders are logged, and links without their query or password.

To find one kind of entry, filter with `grep`: `docker logs open-the-pantry
2>&1 | grep otp.scan`. The other tags are `otp.email`, `otp.url`,
`otp.ocr` and `otp.client`. Set `RECIPE_APP_LOG_LEVEL` to `DEBUG` for more
detail, or `WARNING` for less.

## Development

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

- `tests/README.md` lists which test files cover which feature, and
  describes the import corpus (`python -m app.corpus`), which re-reads real
  inputs after a change and reports any that come out worse.
- Every browser test also fails on a JavaScript error or a
  Content-Security-Policy violation. `test_accessibility.py` runs axe-core
  on every screen (English/light, French/dark); the zoom rule is off, since
  zoom is off on purpose.

CI (`.github/workflows/ci.yml`) runs on every push, every Sunday, and on
demand. It runs both test suites, a dependency audit, and a Docker build
that is started and checked. The image is published only once those pass,
so the weekly run refreshes it with the newest base image, trusted roots
and Chrome version (the browser name link imports send). GitHub pauses
scheduled runs after 60 days without a commit.

```
backend/app/        FastAPI app (main.py: start-up, middleware), models, backup, HTTPS, picker
  routes/           the API by area: recipes, ingest, email, settings, backup; common.py shared
  ingestion/        link, PDF, photo and email reading; tagging
  ml/               Settings -> Learning (numpy only)
  templates/        the print/PDF/HTML template
backend/tests/      server tests; corpus/: import corpus cases; tests_browser/: page tests
frontend/           the app: index.html, js/app.js, js/i18n.js, css/;
                    js/amounts.js + js/weights.js: scaling, Weights, Volumes (also used by What to eat?)
frontend-picker/    the What to eat? page (port 8091)
docker-compose*.yml published image, build from source, HTTPS via Traefik
```
