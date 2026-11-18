# Open the Pantry

*English · [Français](README.fr.md)*

A self-hosted recipe manager. Add recipes from a link, a PDF, a photo or
screenshot, an email, or by typing them in; browse them by tag, search,
rate and share them. English or French (Quebec). One Docker image, no AI
service.

> ## ⚠️ Run this on your own network only
>
> **There is no login.** Anyone who can reach the app can read, change and
> delete every recipe. Only the parts that use your email account or DNS
> login, and restoring or scheduling backups, need the
> [settings password](#settings-password).
>
> **Don't expose it to the internet.** Use it on your LAN, or from outside
> through a VPN (WireGuard, Tailscale…) or a reverse proxy that does its own
> authentication. A serious file-access bug was found and fixed late in
> development, after several reviews had called the code clean. Others may
> exist. The network boundary is the real protection.
> [SECURITY.md](SECURITY.md) lists what the app guards against.

> **Unmaintained.** Built with AI coding tools for personal use and
> published as-is: no support, no commitment to fixes. Forks are welcome.

## How it differs

Compared with Mealie and Tandoor, as their documentation stood in November
2026:

- **Photos, screenshots and PDFs are read without an AI model**: OCR and
  rules on your server, with no API key and no GPU. Mealie and Tandoor read
  photos through an AI model you connect. A model may read a messy photo
  better; the [import corpus](backend/tests/README.md#import-corpus)
  measures how this app does on your own recipes.
- **It learns from your corrections**: how your imports are laid out and
  how you tag, used only where that beats the built-in rules.
- **English and Quebec French**, in the interface and in what it reads.
- **Recipes by email**, sent to an inbox the app checks.
- **What to eat?**, a read-only page that picks a random recipe.
- **Your own measures**: tastiness, pace, difficulty and real cook time,
  all usable as filters.
- **One container**, SQLite, no accounts.

**Not for you if** you want meal planning, shopping lists, nutrition, or
several users with their own accounts. Mealie and Tandoor have those.

## Run it

You need Docker with Compose 2.24 or later.

```bash
curl -O https://raw.githubusercontent.com/djerodek/open-the-pantry/main/docker-compose.yml
docker compose up -d
```

Open `http://<server>:8090`, then **create the settings password right
away** (Settings → Email ingest, HTTPS or Logs asks for it): until one
exists, the first person on your network to open one of them sets it.

- **Update**: `docker compose pull && docker compose up -d`; the database
  is upgraded at start-up. `:main` is rebuilt every Sunday, so a weekly cron
  job running that command keeps it current. To choose when to upgrade, pin
  a digest: `image: djerodek/open-the-pantry@sha256:<digest>`.
- **Build from source**: `docker compose -f docker-compose.build.yml up -d --build`.
- **Images** for `linux/amd64` and `linux/arm64`. The app runs as UID 1000
  (or `PUID`/`PGID`). The ports are published on every address of the
  machine, and Docker's published ports bypass ufw: on a machine with a
  public address, firewall them.
- **Memory**: about 160 MB idle, up to about 3.4 GB with three large scans
  at once (`mem_limit`/`cpus` lines are in the compose file, commented out).
- **Time zone**: `TZ` (default `UTC`) sets the hour of the email scan and
  the automatic backups.
- **Use a local disk**, not NFS: SQLite's journal isn't reliable there.

### Your data

Everything lives in one folder, `./data` by default (the left side of the
`volumes:` line): the database `recipes.db`, photos and PDFs in `uploads/`,
`encryption.key`, `admin-password.json` (the settings password), `https/`,
`logs/`, `ml/` (learned models, rebuilt automatically) and `backups/`.

**To move it:** `docker compose down`, `cp -a ./data /new/path`, change the
left side of the volume line to `/new/path`, `docker compose up -d`. Check
your recipes before deleting the old folder.

**To restore a backup by hand** (when the app won't start; otherwise use
Settings):
1. `docker compose down`
2. `rm -f ./data/recipes.db-wal ./data/recipes.db-shm`
3. `unzip -o open-the-pantry-backup-*.zip -d ./data`
4. `docker compose up -d`

A backup doesn't contain `encryption.key`, `admin-password.json` or
`https/`: copy them from the old folder, or set up email and HTTPS again.

## What it does

### Adding recipes

- **From a link**, one or up to 50 at once, with the page's photo. Over 100
  sites have their own reader; others are read from the page's recipe data,
  then its text.
- **From a PDF**, one or up to 20 at once. Scanned pages go through OCR;
  the largest photo becomes the recipe's.
- **From photos or screenshots**, up to 10 combined into one recipe. Pages
  are cropped, turned upright and straightened; coloured titles and
  two-column ingredient lists are read. Not handwriting: type those in.
- **Typed in.** A short line ending in ":" starts a section ("Sauce:").
- **Shared from another app**: on Android once the app is installed (needs
  HTTPS); on iPhone, a Shortcut that receives URLs from the Share Sheet,
  URL-encodes them and opens `http://<server>:8090/?share_url=<encoded>`.
- **By email**: see below.

A single recipe opens a review screen beside the text that was read;
batches, emails and imports are saved as *To review*. Incomplete recipes
(no steps, or no ingredients) are refused with another way in, and a link
or title already in your library is pointed out. French recipes are read
like English ones.

### Email ingest (optional)

Mail with a keyword in its subject (`[RECIPE]` by default) sent to an inbox
you set up is picked up once a day, or with **Check email inbox** in the +
menu. The app tries a PDF attachment, then a photo, then the first 3 links,
then the text, and emails back `[SUCCESS]`, `[FAILURE]` or `[PARTIAL]`. A
scan missed while the container was down runs later the same day; results
that couldn't be sent wait for the next scan.

- Mail without the keyword is never read; "Only accept email from" limits
  senders (a From line can be forged: it filters, it doesn't prove).
- **Use a dedicated address** with an app-specific password. The app stores
  it encrypted; **Set up encryption** in Settings creates the key.

### Finding and using recipes

- **Tags** in three built-in groups (meal type, cooking style, main
  ingredient) plus your own, suggested from keywords in English and French.
- **Learning** (Settings → Learning) trains two small models on your
  corrected recipes, for reading imports and for tags; Settings shows how
  each scores against the rules.
- **Search** covers every field, forgives typos, takes `-word` to leave a
  word out, and puts the best match first. Filters stack.
- **On a recipe**: ratings, servings, logged cook time, notes; ½ / 1× / 2×
  scaling; **Weights** and **Volumes** conversions from a table of about
  220 ingredients, which leave a line as written when no conversion is
  safe; keep the screen awake (needs HTTPS); optional checkboxes.
- **Library**: edit, duplicate, merge two recipes, tag or delete several at
  once; **Recently deleted** keeps recipes 30 days; **Library check** lists
  incomplete recipes and likely duplicates.
- **Share**: print, PDF, HTML, JSON, a JPG image, text, or email the PDF.

### What to eat? (optional)

A small read-only page on port 8091 that picks a random recipe from your
choices. Turn it on in Settings → What to eat?. **It has no login**: anyone
who can reach the port can read every recipe it can pick, notes included.
If 8091 is taken, publish another port (e.g. `"9000:8091"`) and set
`RECIPE_APP_PICKER_PUBLIC_PORT: "9000"`; behind a proxy, set
`RECIPE_APP_PICKER_PUBLIC_URL` and `RECIPE_APP_PUBLIC_URL`.

### Backup and restore

Settings → Backup & export:
- **Full backup** (`.zip`): the database, every photo and PDF, and restore
  instructions. The one you can restore from.
- **Recipes as PDFs** or **as JSON** (schema.org, for other recipe apps).
- **Automatic backups**: daily at the hour you choose, the newest 7 kept,
  in `data/backups/`. For a real second copy, mount a folder on another
  disk and set `RECIPE_APP_BACKUP_DIR` (lines in the compose file).
- **Backups leave out** the email login, notification addresses and HTTPS
  settings ([full list](SECURITY.md)), since anyone on your network can
  download one. They aren't
  encrypted: keep them where only you can read them.
- **Restore or import**: a full backup replaces the library (settings
  password; a safety copy is written first; this server keeps its own
  HTTPS, schedule and email settings). If a restore fails partway, the app
  stays closed until restarted and finishes it at start-up. schema.org
  JSON, a zip of it, or a Paprika export adds its recipes instead.

### HTTPS (optional)

Settings → HTTPS gives the app an `https://` address with a Let's Encrypt
certificate, for your LAN and VPN only, on domains whose DNS is managed in
cPanel. Keep screen awake and installing the app on Android need it.
Other DNS hosts can use Traefik instead. Setup: [docs/https.md](docs/https.md).

### Interface

English or French per device; installable (PWA); light or dark theme; text
size; the recipe list as titles, cards with photos, or tiles; pinch-zoom off on phones on purpose (it pushed buttons off screen);
keyboard and screen-reader support, checked by an automated scan.

## Settings password

It guards Email ingest, HTTPS, Logs, Email PDF, restoring a backup and the
backup schedule.
- Created the first time one of those is opened (8+ characters).
- Unlocks a browser for 15 minutes. Restarting the container locks every
  browser and clears the wait after wrong tries.
- Once HTTPS is on, it works only at the `https://` address.
- **Forgotten?** Delete `admin-password.json` and create a new one. That
  clears the saved email password and cPanel token and turns HTTPS off;
  everything else is kept.

## Known limitations

- **Some sites refuse automated requests** (Cloudflare's bot check,
  paywalls): save the page as a PDF, take screenshots, or paste the text.
- **Long screenshots saved as PDF on iOS are cut at 200 inches**: use Print
  → PDF.
- **Imports follow rules** until Learning has your corrections: check them
  on the review screen or under *To review*.
- **A PDF's recipe photo** is its largest image; replace it if needed.
- **Email ingest** needs a certificate the system trusts, and is slow on an
  inbox with hundreds of unread messages.

## Logs

Settings → Logs, `docker logs open-the-pantry`, or `data/logs/app.log`.
Passwords, keys, email bodies and page contents are never logged.
`RECIPE_APP_LOG_LEVEL` sets the detail.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md): tests, CI, and the source layout.
