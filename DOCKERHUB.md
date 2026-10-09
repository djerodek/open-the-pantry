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

Open http://localhost:8090 — data persists in `./data` beside the compose file.
Then create the settings password right away (Settings → Email ingest asks for
it): until it exists, the first person on your network to open that section
sets it.

Updating:

    docker compose pull && docker compose up -d

Pin a version tag instead of `main` if you want to control exactly when
updates apply.

## Features

- Ingest from URL (100+ site parsers, JSON-LD, heuristic fallback), PDF
  (text-layer detection, OCR only where needed), screenshots (single or
  combined multi-screenshot), or manual entry
- Reads English and French recipes: headings, units, and English + French
  OCR
- Tag groups: meal type, cooking style (incl. cocktails), main ingredient,
  plus your own groups and keywords; automatic tag suggestions (English and
  French words), applied to new recipes or, after a review, to all of them
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
- Full backup (restorable) or every recipe as PDFs, from Settings
- Logs: Settings → Logs (view, download, clear), `docker logs
  open-the-pantry`, or `data/logs/app.log`

Full documentation, source, and security notes:
https://github.com/djerodek/open-the-pantry
