# Security

*English · [Français](SECURITY.fr.md)*

What Open the Pantry guards against, and how. The short version is in the
[README](README.md): **there is no login, so run it on your own network
only.**

## Settings password

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
- **Storage.** A salted scrypt hash in `admin-password.json`.
- **Forgotten?** Delete `admin-password.json`, then create a new password.
  Creating it clears the saved email password and cPanel token, and turns
  HTTPS off. Everything else is kept. A damaged file isn't taken for "no
  password": Settings says so, and deleting it is the way to reset.

## Everything else

- **The rest of the app is open to anyone who can reach it.**
  `RECIPE_APP_API_KEY` makes every request need an `X-API-Key` header. The
  pages don't send that header, so it's only useful behind a proxy that
  adds it. An authenticating proxy or a VPN is the better answer.
- **What to eat?** (port 8091, off by default) has no login either, and
  `RECIPE_APP_API_KEY` doesn't cover it: anyone who can reach the port can
  read every recipe it can pick, notes included. It is plain `http://` and
  read-only.
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
    password, 32 MB anything else), so no more than that reaches the disk.
  - **Images** are re-encoded, which drops metadata and anything hidden in
    them. Only JPEG, PNG, GIF and WEBP decoders are used, and anything over
    50 megapixels is refused. A photo read for text is first reduced to 35
    megapixels, and up to 3 are prepared at once. Up to 10 photos combined
    into one recipe share one OCR clock (below).
  - **PDFs**: at most 60 pages, at most 3 OCR jobs at once, and only the
    largest of a PDF's photos is decoded.
  - **OCR clock**: an import's OCR stops 10 minutes after its first
    Tesseract run, the page or photo under way included. Saving the files
    and waiting for an OCR job to be free come before it and don't count.
  - **Batches** (links, PDFs): one at a time, and 15 minutes for the whole
    batch, upload included. A second batch is refused before the app reads
    its upload, and an upload idle for 60 seconds ends the request. What
    wasn't started in time is listed as not tried, to send again.
  - **Email**: mail over 30 MB, nested too deeply, or that OCR can't read
    reliably is refused and marked read; senders not allowed are refused
    from the From line, before the mail is downloaded.
  - **Archives** (backups, imports): at most 20,000 files, checked before
    the file list is read, and size-limited when unpacked.
  - **Downloaded pages**: at most 10 MB and 30 seconds in all, however
    slowly the server answers, from public addresses only; every redirect
    is checked again.
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
- **What a backup leaves out**, since anyone on your network can download
  one: the email login and password, subject keyword, allowed senders,
  notification address and recent recipients, the HTTPS name and
  addresses, and `encryption.key`, `admin-password.json` and `data/https/`
  (the certificate and cPanel login). A backup still holds every recipe,
  note and photo, unencrypted.
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
