# Docker Hub overview — copy/paste

Docker Hub's repository "Overview" field is edited on the website and does
**not** sync from GitHub automatically. Paste the block below into it
(Repository → Manage → Overview).

---

```markdown
# Open the Pantry

Self-hosted, searchable recipe manager. Add recipes by URL, PDF, screenshot,
or manual entry; browse, filter, rate, and export clean printable copies.
No LLM dependency.

---

## ⚠️ Run this on your own network only

**No authentication by default.** Anyone who can reach this container can read,
modify, and delete every recipe, and use its API. There is no login screen and
no user accounts.

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

Updating:

    docker compose pull && docker compose up -d

Pin a version tag instead of `latest` if you want to control exactly when
updates apply.

## Features

- Ingest from URL (100+ site parsers, JSON-LD, heuristic fallback), PDF
  (text-layer detection, OCR only where needed), screenshots (single or
  combined multi-screenshot), or manual entry
- Tag categories: meal type, cooking style (incl. cocktails), main ingredient
- Full-text search across recipes and tags; stacked filters; cook-time filter
- Ratings, favorites, per-recipe notes, showcase images
- Print, PDF, and self-contained HTML export
- PWA: installable, offline shell, light/dark (true black) themes, adjustable
  text size, keyboard and screen-reader accessible
- Optional email ingest (off by default)

Full documentation, source, and security notes:
https://github.com/djerodek/open-the-pantry
```
