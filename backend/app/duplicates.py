"""Is this recipe already in the library?

Two signals:
- the same web address (the strong one): compared without the scheme,
  "www.", the fragment, a trailing slash or tracking parameters (utm_*,
  fbclid and the like), so the link from a newsletter and the one from the
  browser's address bar match;
- the same title: compared without case, accents, punctuation or extra
  spaces ("Crêpes!" = "crepes").

Interactive adds (the review screen, Add manually) show either as a warning
and let the person save anyway. Unattended adds (batch links, email) skip a
recipe whose link is already saved and save one whose title matches with a
note, since two different recipes can share a title ("Banana bread").
"""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit

from sqlalchemy.orm import Session

from . import models

_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|_ga|igshid|ref|ref_src|cmpid)$", re.I)


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        port_number = parts.port      # ValueError for "example.com:443443" or ":abc"
    except ValueError:
        # Not a usable address. Raising here took down a whole batch, the
        # duplicate check and every emailed link once one saved recipe had
        # such a link (Claude review round 9).
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    if host.startswith("www."):
        host = host[4:]
    # Only the scheme's own default port is dropped: http://x:443 and
    # https://x:80 are other addresses (GPT review of 0093).
    default = 80 if parts.scheme.lower() == "http" else 443
    port = f":{port_number}" if port_number and port_number != default else ""
    path = re.sub(r"/+$", "", parts.path) or ""
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not _TRACKING.match(k)))
    return f"{host}{port}{path}" + (f"?{query}" if query else "")


def normalize_title(title: str | None) -> str | None:
    if not title:
        return None
    text = unicodedata.normalize("NFKD", title)
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"[\W_]+", " ", text).strip()
    return text or None


def find(db: Session, title: str | None = None, url: str | None = None,
         exclude_id: int | None = None, limit: int = 5) -> list[dict]:
    """Recipes that look like this one, link matches first:
    [{"id", "title", "reason": "link" | "title"}]."""
    want_url, want_title = normalize_url(url), normalize_title(title)
    if not want_url and not want_title:
        return []
    by_link, by_title = [], []
    for rid, rtitle, rurl in db.query(models.Recipe.id, models.Recipe.title, models.Recipe.source_url):
        if rid == exclude_id:
            continue
        if want_url and normalize_url(rurl) == want_url:
            by_link.append({"id": rid, "title": rtitle, "reason": "link"})
        elif want_title and normalize_title(rtitle) == want_title:
            by_title.append({"id": rid, "title": rtitle, "reason": "title"})
    return (by_link + by_title)[:limit]


def same_link(db: Session, url: str | None) -> dict | None:
    """The saved recipe with this link, if any."""
    found = find(db, url=url, limit=1)
    return found[0] if found and found[0]["reason"] == "link" else None


def groups(db: Session) -> list[list[dict]]:
    """Sets of recipes that look like the same one (same link or same
    title, as find() decides), for Settings -> Library check. A recipe
    sharing a link with one and a title with another puts all three in one
    set. Each set oldest first; sets by their first title."""
    rows = db.query(models.Recipe.id, models.Recipe.title, models.Recipe.source_url).order_by(models.Recipe.id).all()
    parent = {rid: rid for rid, _, _ in rows}

    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    first_by_key: dict = {}
    for rid, title, url in rows:
        for key in (("link", normalize_url(url)), ("title", normalize_title(title))):
            if key[1] is None:
                continue
            if key in first_by_key:
                parent[root(rid)] = root(first_by_key[key])
            else:
                first_by_key[key] = rid
    sets: dict = {}
    for rid, title, _ in rows:
        sets.setdefault(root(rid), []).append({"id": rid, "title": title})
    found = [s for s in sets.values() if len(s) > 1]
    return sorted(found, key=lambda s: normalize_title(s[0]["title"]) or "")
