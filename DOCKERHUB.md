# Open the Pantry

Self-hosted, searchable recipe manager. Add recipes by URL, PDF, screenshot,
or manual entry; browse, filter, rate, and export clean printable copies.
Interface in English or French. No LLM dependency.

---

## ⚠️ Run this on your own network only

**There is no login.** Anyone who can reach the container can read, change
and delete every recipe. Only Email ingest, HTTPS, Logs, Email PDF and
restoring a backup need a settings password, created in the app.

**Don't expose it to the internet.** Use it on your LAN, or from outside
through a VPN or a reverse proxy that does its own authentication. A
serious file-access bug was found and fixed late in development, after
several reviews had called the code clean; others may exist. The network
boundary is the real protection.

## 📋 Unmaintained

Built with AI coding tools for personal use and published as-is: no
support, no commitment to fixes. Forks welcome.

---

## Run it

Save as `docker-compose.yml`:

    services:
      open-the-pantry:
        image: djerodek/open-the-pantry:main
        container_name: open-the-pantry
        ports:           # every address of the machine: firewall them on a VPS
          - "8090:8090"
          - "8443:8443"   # HTTPS, once set up in Settings
          - "8091:8091"   # What to eat?, once switched on in Settings
        env_file:         # optional; see the README's HTTPS section
          - path: .env
            required: false
        volumes:
          - ./data:/app/data
        restart: unless-stopped
        # environment:
        #   # Optional: email ingest can create its own key in Settings.
        #   # Set this only to keep the key outside ./data.
        #   RECIPE_APP_ENCRYPTION_KEY: "see-README"
        #   # Recommended if anything beyond you can reach this.
        #   RECIPE_APP_API_KEY: "a-long-random-string"

Then:

    docker compose up -d

(`env_file` with `required: false` needs Docker Compose 2.24 or later.)

Open `http://<server>:8090`; data lives in `./data`. Then create the
settings password right away (Settings → Email ingest asks for it): until it
exists, the first person on your network to open that section sets it.

**Updating:** `docker compose pull && docker compose up -d`. The image is
rebuilt every Sunday (newest base image, trusted roots and Chrome version);
a weekly cron job running that command keeps a server current. Pin a digest
(`djerodek/open-the-pantry@sha256:...`) instead of `main` to choose when.

## How it differs

Compared with Mealie and Tandoor (their documentation, October 2026):
photos, screenshots and PDFs are read with OCR and rules on your server, with
no AI model, API key or GPU. It learns from your corrections, reads English
and Quebec French, takes recipes by email, and picks a random one for you.
**Not for you if** you want meal planning, shopping lists, nutrition or
several user accounts.

## Features

- **Add recipes** from a link (100+ site parsers, schema.org, the page
  itself), a PDF (OCR only where needed), screenshots (one or several
  combined), email (optional), or typed in; one at a time with a review
  screen, or in batches marked *To review*. Already-saved links are flagged.
- **English and French** (Quebec): the interface, per device, and the
  recipes it reads.
- **Learns from your corrections**, on the server: how your imports are
  laid out and how you tag, used only where it beats the built-in rules.
- **Find recipes**: tag groups with automatic suggestions, full-text search
  (typos forgiven, best match first, `-word` to leave out), stacked filters,
  ratings, pace, cook time.
- **On a recipe**: ½ / 1× / 2× scaling, and As written / Weights / Volumes
  (volumes as grams, or weights as cups and spoons); notes; keep the screen
  awake (needs HTTPS).
- **Manage**: edit, duplicate, merge two recipes, tag or delete several at
  once, Recently deleted (30 days), and a library check.
- **Share**: print, PDF, self-contained HTML, JSON, a JPG image, or email
  the PDF from the Share menu.
- **What to eat?** (optional, port 8091): a read-only page that picks a
  random recipe from your choices, with the same scaling and units. It has
  no login, so anyone who can reach the port can read your recipes.
- **HTTPS on your LAN** (optional): a real Let's Encrypt certificate, set
  up from Settings for cPanel-managed domains.
- **Backups**: full backups (credentials left out) on demand or daily, to
  another disk if you like (`RECIPE_APP_BACKUP_DIR`), restored from
  Settings; every recipe as PDFs or schema.org JSON; import schema.org JSON
  or a Paprika export.
- **PWA**: installable, light/dark themes, adjustable text size, keyboard
  and screen-reader accessible.
- **Logs**: Settings → Logs, `docker logs open-the-pantry`, or
  `data/logs/app.log`.

Full documentation, source, and security notes:
https://github.com/djerodek/open-the-pantry
