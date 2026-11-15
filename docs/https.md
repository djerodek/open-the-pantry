# HTTPS on your network

*English · [Français](https.fr.md)*

HTTPS is off by default. **Settings → HTTPS** gives the app an `https://`
address with a real Let's Encrypt certificate, for your LAN and VPN only;
nothing is exposed to the internet. Keep screen awake, installing the app
on Android, and sharing to it from Android need it.

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

## Other DNS hosts

Cloudflare, OVH, Gandi…: `docker-compose.https.yml`
does the same job with Traefik. Don't use it alongside the cPanel route.
- Put the settings in `.env` (from `https.env.example`) and the DNS host's
  token in `traefik.env` (from `traefik.env.example`); only Traefik reads
  that file. The comments in those files explain each setting.
- On this route the settings password works only at the `https://` address.
  If Traefik is down, those sections stay closed on purpose.
- Traefik reads the Docker socket, which gives it control of Docker. A
  socket proxy that only allows listing containers closes that, if it
  matters on your network.
