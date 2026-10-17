# Open the Pantry

Self-hosted, searchable recipe manager. Add recipes by URL, PDF, screenshot,
or manual entry; browse, filter, rate, and export clean printable copies.
Interface in English or French. No LLM dependency.

---

## ⚠️ Run this on your own network only

**No authentication by default.** Anyone who can reach this container can read,
modify, and delete every recipe, and use its API. There is no login screen and
no user accounts. The exception: Email ingest, HTTPS, Logs and Email PDF, which
use your email account or DNS login, need a settings password created in the
app (15-minute unlock). Forgotten: delete `data/admin-password.json`; the new
password then clears the saved email password and HTTPS, to be set up again.

**Do not expose it to the internet.** Run it on your LAN, or reach it remotely
via VPN (WireGuard, Tailscale, OpenVPN) or behind a reverse proxy that enforces
authentication itself.

This isn't theoretical: a vulnerability letting any unauthenticated caller read
arbitrary files from the container and delete the app's own database was found
and fixed late in development, after multiple review passes had reported the
code clean. It's fixed and regression-tested — but others may exist. The network
boundary is the real protection.

## 📋 Unmaintained

Built with AI coding tools for personal home use, published in case it's useful
to others. Provided as-is: no support, no issue triage, no commitment to fix
bugs or ship security updates. Forks welcome.

---

## Run it

Save as `docker-compose.yml`:

    services:
      open-the-pantry:
        image: djerodek/open-the-pantry:main
        container_name: open-the-pantry
        ports:
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

Open http://localhost:8090 — data persists in `./data` beside the compose file.
Then create the settings password right away (Settings → Email ingest asks for
it): until it exists, the first person on your network to open that section
sets it.

Updating:

    docker compose pull && docker compose up -d

Pin the image by digest (`djerodek/open-the-pantry@sha256:...`) instead of
`main` if you want to control exactly when updates apply.

## Features

- Ingest from URL (100+ site parsers, JSON-LD, heuristic fallback), PDF
  (text-layer detection, OCR only where needed), screenshots (single or
  combined multi-screenshot), or manual entry
- Reads English and French recipes: headings, units, and English + French
  OCR
- Tag groups: meal type, cooking style (incl. cocktails), main ingredient,
  plus your own groups and keywords; automatic tag suggestions (English and
  French words), applied to new recipes or, after a review, to all of them
- Learns from your own saved recipes, on the server (no LLM, no outside
  service): how your photo, PDF and email imports are laid out, and how you
  tag; used only where it beats the built-in rules on your recipes
- Full-text search across recipes and tags; stacked filters; pace and
  cook-time filters; sort by date, title, rating, cook time or difficulty
- Ratings, favorites, per-recipe notes (marked on the recipe cards),
  showcase images
- Print, PDF, and self-contained HTML export; email the PDF to someone
  straight from the Share menu
- English or French (Quebec) interface, chosen per device, including
  exports and notification emails
- PWA: installable, offline shell, pull down to refresh (and pick up a new
  version), light/dark (true black) themes, adjustable text size, keyboard
  and screen-reader accessible
- Optional "keep screen awake" while a recipe is open (needs HTTPS; see
  below)
- Optional HTTPS on your LAN with a real Let's Encrypt certificate, set up
  from Settings (cPanel-managed domains; see the README)
- Optional email ingest (off by default): email a link, PDF, photo or the
  recipe text to a dedicated inbox
- Half or double a recipe's ingredient amounts on its page (½ / 1× / 2×)
- Warns when a recipe being added is already in the library (same link or
  title); batch and emailed links already saved are skipped
- Share a page's link to the app (Android share sheet, or an iPhone
  Shortcut) to import it
- Optional "What to eat?" page (off by default): a second, read-only page on
  port 8091 that picks a random recipe by meal type, cooking style, main
  ingredient or pace, shuffles through titles and stops on one to read and
  print right there (read-only; edit in the main app); no login, so anyone
  who can reach the port can read your recipes; switched on in Settings
- Full backup (restorable), every recipe as PDFs, or every recipe as schema.org
  JSON for other recipe apps; import schema.org JSON or a Paprika export
- Logs: Settings → Logs (view, download, clear), `docker logs
  open-the-pantry`, or `data/logs/app.log`

Full documentation, source, and security notes:
https://github.com/djerodek/open-the-pantry
