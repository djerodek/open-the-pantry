import ipaddress
import json
import re
import socket
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

from ..logging_setup import get_logger

log = get_logger("url")

# A generic/custom UA gets blocked by some recipe sites' bot filtering.
# This mirrors a current desktop Chrome UA for compatibility; it's still an
# honest identification of an HTTP client, not spoofing a browser's behavior.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class UrlValidationError(ValueError):
    pass


def validate_public_url(url: str):
    """Reject URLs that resolve to private/loopback/link-local/reserved
    addresses. This endpoint has no auth in front of it, so without this
    check it could be used as an SSRF proxy against internal network
    services or cloud metadata endpoints (169.254.169.254 etc). Runs before
    any fetch is attempted -- including recipe_scrapers' own internal
    fetch, which this app doesn't otherwise control.

    This is a DNS-resolve-time check, not a connection-time one, so it
    doesn't defend against DNS rebinding (a host resolving to a public IP
    now and a private one at actual fetch time). That's a deliberate
    scope/complexity tradeoff for a personal self-hosted tool, not an
    oversight -- closing it fully would mean a custom requests transport
    that re-validates the IP at connect time."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UrlValidationError("Only http/https URLs are supported.")
    if not parsed.hostname:
        raise UrlValidationError("URL has no hostname.")

    try:
        addrinfo = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror:
        raise UrlValidationError("Could not resolve host.")

    for family, _, _, _, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise UrlValidationError("URLs resolving to private/internal network addresses aren't allowed.")


MAX_REDIRECTS = 5


def safe_get(url: str, timeout: int = 15) -> requests.Response:
    """requests.get()'s default (allow_redirects=True) follows redirects
    without re-validating them -- a page could return a 302 to
    169.254.169.254 or an internal address, completely bypassing
    validate_public_url's check on the *original* URL. This validates
    every hop before following it, not just the first one.

    Every page fetch in URL ingestion goes through here. recipe_scrapers
    used to be called with scrape_me(url), which fetches the page with its
    own HTTP client -- so the most common path skipped these checks, and a
    public URL redirecting to a LAN address would have been followed. It
    now gets the HTML this function already fetched (see ingest_url).

    Residual gap, accepted for a LAN app: DNS is resolved once to validate
    and again by requests to connect, so a hostname that changes its
    answer in between (DNS rebinding) isn't caught."""
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        validate_public_url(current_url)
        try:
            resp = requests.get(current_url, headers={"User-Agent": USER_AGENT}, timeout=timeout, allow_redirects=False)
        except requests.RequestException as e:
            log.warning("GET %s failed: %s: %s", current_url, type(e).__name__, e)
            raise
        log.info("GET %s -> %s (%s bytes, %s)", current_url, resp.status_code,
                 len(resp.content), resp.headers.get("Content-Type", "?"))
        if resp.is_redirect or resp.is_permanent_redirect:
            next_url = resp.headers.get("Location")
            if not next_url:
                break
            current_url = requests.compat.urljoin(current_url, next_url)
            log.info("  redirect -> %s", current_url)
            continue
        if resp.status_code >= 400:
            # Bot-protection pages (Cloudflare etc.) answer 403/503 with an
            # HTML challenge. Its <title> says which, so log it.
            log.warning("GET %s refused: HTTP %s, page title %r, server %r", current_url,
                        resp.status_code, _page_title(resp.text), resp.headers.get("Server"))
        resp.raise_for_status()
        return resp
    raise UrlValidationError("Too many redirects, or a redirect with no Location header.")


def _page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:120] if m else ""


class UrlIngestResult:
    def __init__(self, title, ingredients, steps, servings=None, prep_time=None,
                 cook_time=None, total_time=None, image_url=None, raw_text="",
                 method=""):
        self.title = title
        self.ingredients = ingredients  # list[str] raw lines
        self.steps = steps  # list[str] raw lines
        self.servings = servings
        self.prep_time = prep_time
        self.cook_time = cook_time
        self.total_time = total_time
        self.image_url = image_url
        self.raw_text = raw_text
        self.method = method  # which extraction path succeeded, for debugging/UI


def _try_recipe_scrapers(url: str, html: str):
    """recipe_scrapers over HTML that safe_get() already fetched -- never
    scrape_me(), which does its own unguarded fetch. supported_only=False
    keeps the old two-step behaviour in one call: the site-specific parser
    for domains the library knows, generic schema.org guessing for the
    rest."""
    try:
        from recipe_scrapers import scrape_html
    except ImportError:
        return None
    try:
        scraper = scrape_html(html, org_url=url, supported_only=False)
    except Exception as e:
        log.info("recipe-scrapers: no scraper for %s: %s: %s", url, type(e).__name__, e)
        return None

    try:
        ingredients = scraper.ingredients()
        instructions = scraper.instructions()
        steps = [s.strip() for s in instructions.split("\n") if s.strip()]
        image_url = None
        try:
            image_url = scraper.image()
        except Exception:
            pass
        servings = None
        try:
            servings = str(scraper.yields())
        except Exception:
            pass
        total_time = None
        try:
            total_time = str(scraper.total_time())
        except Exception:
            pass

        return UrlIngestResult(
            title=scraper.title(),
            ingredients=ingredients,
            steps=steps,
            servings=servings,
            total_time=total_time,
            image_url=image_url,
            raw_text=instructions,
            method="recipe-scrapers",
        )
    except Exception as e:
        log.info("recipe-scrapers: parse failed for %s: %s: %s", url, type(e).__name__, e)
        return None


def _try_json_ld(html: str):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except (json.JSONDecodeError, TypeError):
            continue

        candidates = data if isinstance(data, list) else [data]
        # Some sites nest the Recipe inside an @graph array.
        expanded = []
        for c in candidates:
            if isinstance(c, dict) and "@graph" in c:
                expanded.extend(c["@graph"])
            else:
                expanded.append(c)

        for item in expanded:
            if not isinstance(item, dict):
                continue
            item_type = item.get("@type", "")
            types = item_type if isinstance(item_type, list) else [item_type]
            if "Recipe" not in types:
                continue

            title = item.get("name", "Untitled Recipe")

            raw_ingredients = item.get("recipeIngredient") or item.get("ingredients") or []
            if isinstance(raw_ingredients, str):
                raw_ingredients = [raw_ingredients]

            instructions = item.get("recipeInstructions", [])
            steps = []
            if isinstance(instructions, str):
                steps = [s.strip() for s in instructions.split("\n") if s.strip()]
            elif isinstance(instructions, list):
                for step in instructions:
                    if isinstance(step, dict):
                        text = step.get("text") or step.get("name")
                        if text:
                            steps.append(text.strip())
                    elif isinstance(step, str):
                        steps.append(step.strip())

            image = item.get("image")
            image_url = None
            if isinstance(image, str):
                image_url = image
            elif isinstance(image, list) and image:
                image_url = image[0] if isinstance(image[0], str) else image[0].get("url")
            elif isinstance(image, dict):
                image_url = image.get("url")

            servings = item.get("recipeYield")
            if isinstance(servings, list):
                servings = servings[0] if servings else None

            return UrlIngestResult(
                title=title,
                ingredients=raw_ingredients,
                steps=steps,
                servings=str(servings) if servings else None,
                prep_time=item.get("prepTime"),
                cook_time=item.get("cookTime"),
                total_time=item.get("totalTime"),
                image_url=image_url,
                raw_text="\n".join(steps),
                method="json-ld",
            )
    return None


def _try_heuristic_html(html: str):
    """Last-resort fallback for pages with no structured data. Strips boilerplate
    with a readability-style pass, then looks for headings matching
    'ingredients' / 'instructions' and grabs the following list/paragraph content."""
    soup = BeautifulSoup(html, "html.parser")

    title_tag = soup.find("h1") or soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else "Untitled Recipe"

    ingredients = _collect_after_heading(soup, r"ingredients?")
    steps = _collect_after_heading(soup, r"instructions?|directions?|method|steps?")

    if not ingredients and not steps:
        return None

    og_image = soup.find("meta", property="og:image")
    image_url = og_image["content"] if og_image and og_image.get("content") else None

    return UrlIngestResult(
        title=title,
        ingredients=ingredients,
        steps=steps,
        image_url=image_url,
        raw_text="\n".join(steps),
        method="heuristic-html",
    )


def _collect_after_heading(soup, pattern):
    heading_re = re.compile(pattern, re.IGNORECASE)
    results = []
    for heading in soup.find_all(re.compile("^h[1-6]$")):
        if heading_re.search(heading.get_text()):
            sib = heading.find_next_sibling()
            while sib and sib.name in ("ul", "ol", "p", "div"):
                if sib.name in ("ul", "ol"):
                    results.extend(li.get_text(strip=True) for li in sib.find_all("li"))
                    sib = sib.find_next_sibling()
                    break
                elif sib.name == "p":
                    results.append(sib.get_text(strip=True))
                    sib = sib.find_next_sibling()
                else:
                    sib = sib.find_next_sibling()
            if results:
                break
    return [r for r in results if r]


def ingest_url(url: str) -> UrlIngestResult:
    """
    Three-tier ingestion, no LLM:
      1. recipe-scrapers (100+ site-specific parsers)
      2. schema.org JSON-LD (covers most other sites with structured data)
      3. heuristic HTML scrape (best-effort fallback, needs manual review)
    Raises ValueError if none succeed -- caller should route to manual entry.
    """
    validate_public_url(url)  # SSRF guard -- must run before any fetch, see above

    # One fetch, through the guarded client, shared by all three tiers.
    resp = safe_get(url)
    html = resp.text

    for name, attempt in (("recipe-scrapers", lambda: _try_recipe_scrapers(url, html)),
                          ("json-ld", lambda: _try_json_ld(html)),
                          ("heuristic", lambda: _try_heuristic_html(html))):
        try:
            result = attempt()
        except Exception:
            log.exception("%s: crashed on %s", name, url)
            result = None
        if result:
            log.info("%s: got %r from %s (%d ingredients, %d steps)", name, result.title, url,
                     len(result.ingredients or []), len(result.steps or []))
            return result
        log.info("%s: nothing usable on %s", name, url)

    # Enough about the page to tell "blocked" from "no recipe markup" from
    # "recipe markup we can't read", without logging the page itself.
    ld_types = re.findall(r'"@type"\s*:\s*"?\[?\s*"([A-Za-z]+)"', html)
    log.warning(
        "No recipe extracted from %s: HTTP %s, %d bytes, title %r, %d ld+json blocks, "
        "@types seen %s, 'Recipe' in page: %s",
        url, resp.status_code, len(html), _page_title(html),
        html.count("application/ld+json"), sorted(set(ld_types))[:12], "Recipe" in html,
    )
    raise ValueError(
        "Could not extract a recipe from this URL automatically. "
        "Use manual entry instead, pasting from the page."
    )
