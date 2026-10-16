"""What to eat? / On mange quoi?: a second, read-only page on its own port.

Choose a meal type, cooking style, main ingredient and/or pace (or none),
press one button, get one random recipe that matches, as a link into the
main app. specs/dinner-picker-spec.md has the full design.

What lives here:

* The picker's API: GET /api/picker/options, /random and /status. Mounted
  on both ports (the main app uses /status for its top-bar link).
* The switch, PUT /api/picker/settings, on the main port only. Saved in the
  database (PickerSettings), so a restored backup brings it back; read at
  start-up. Not behind the settings password: the page shows nothing the
  main app doesn't already show without one, and changes nothing.
* The picker port (8091 in the container). A second uvicorn listener in this
  process serves a separate, small app, not the main one, started and
  stopped from Settings without a restart (the same pattern as the HTTPS
  listener in https_setup.py). It mounts no route that changes anything, so
  nothing can be changed through it. The host-name check and the rate limit
  apply as on the main port (main.py passes the same middleware);
  RECIPE_APP_API_KEY does not, since it guards write and admin routes and
  this port has none.

Matching differs from the main list on purpose. Within a category the
choices are alternatives (Dinner or Lunch; quick or moderate); across
categories all must hold. A category with nothing chosen restricts nothing.
Cocktails are never offered and never picked: any recipe tagged with the
Cocktails meal type or a cocktail cooking style is a drink, even if it also
carries a food tag.
"""
import asyncio
import contextlib
import os
import random
from typing import Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from . import https_setup, models, schemas
from .database import SessionLocal, UPLOADS_DIR, get_db
from .file_validation import safe_join
from .init_db import SEED_TAGS
from .logging_setup import get_logger

log = get_logger("picker")


def _port(var: str, default: int) -> int:
    raw = os.environ.get(var, "").strip()
    if not raw:
        return default
    try:
        port = int(raw)
        if 1 <= port <= 65535:
            return port
    except ValueError:
        pass
    log.warning("%s=%r isn't a port number (1-65535); using %s", var, raw, default)
    return default


def _web_address(var: str) -> str:
    """An http(s) address from the environment, without a trailing slash, or
    "" if unset or not a web address (logged: it would make a broken link)."""
    raw = os.environ.get(var, "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme.lower() in ("http", "https") and parts.netloc:
        return raw.rstrip("/")
    log.warning("%s=%r isn't an http:// or https:// address; ignored", var, raw)
    return ""


# The port inside the container. The compose files publish 8091:8091.
LISTEN_PORT = _port("RECIPE_APP_PICKER_PORT", 8091)
# The links between the two pages. Needed only when the compose file
# publishes a port under another number, or behind a proxy.
PICKER_PUBLIC_URL = _web_address("RECIPE_APP_PICKER_PUBLIC_URL")
PICKER_PUBLIC_PORT = _port("RECIPE_APP_PICKER_PUBLIC_PORT", LISTEN_PORT)
MAIN_PUBLIC_URL = _web_address("RECIPE_APP_PUBLIC_URL")
MAIN_PUBLIC_PORT = _port("RECIPE_APP_MAIN_PUBLIC_PORT", 8090)

CATEGORIES = ("meal_type", "cooking_style", "main_ingredient")
PACES = ("quick", "moderate", "long")
# Tastiness nudges the odds a little and never rules anything out: 5 stars
# comes up 1.5x as often as 1 star, 1.2x as often as unrated. Favourites,
# pace and difficulty carry no weight (pace is a filter only).
TASTINESS_WEIGHTS = {None: 1.0, 1: 0.8, 2: 0.9, 3: 1.0, 4: 1.1, 5: 1.2}
MAX_EXCLUDE = 500          # recipe ids already shown this round; extra ones ignored
MAX_IDS_PER_CATEGORY = 500
SHUFFLE_TITLES = 8         # other matching titles for the shuffle effect
IMAGE_EXTENSIONS = (".jpg", ".png", ".gif", ".webp")

# Built-in tags first, in the order they're seeded (Breakfast, Brunch,
# Lunch, Dinner...); tags added later follow alphabetically.
_BUILTIN_ORDER = {(name, category): i for i, (name, category, _sub) in enumerate(SEED_TAGS)}

# Seeded in tests (test_picker.py); otherwise seeded from the system.
_rng = random.Random()


# ---------------------------------------------------------------------------
# Which recipes can be picked
# ---------------------------------------------------------------------------

def _is_cocktail_tag(category: str, name: str, subgroup: Optional[str]) -> bool:
    return ((category == "meal_type" and (name or "").strip().lower() == "cocktails")
            or (category == "cooking_style" and subgroup == "cocktail"))


def _drink_recipe_ids():
    """Recipes with a cocktail tag, as a subquery: never offered, never picked."""
    cocktail_tags = select(models.Tag.id).where(or_(
        and_(models.Tag.category == "meal_type", func.lower(func.trim(models.Tag.name)) == "cocktails"),
        and_(models.Tag.category == "cooking_style", models.Tag.subgroup == "cocktail"),
    ))
    return select(models.recipe_tags.c.recipe_id).where(models.recipe_tags.c.tag_id.in_(cocktail_tags))


def _tag_order(name: str, category: str):
    i = _BUILTIN_ORDER.get((name, category))
    return (0, i, "") if i is not None else (1, 0, name.casefold())


def _safe_image(name: Optional[str]) -> Optional[str]:
    # The name comes from the database, which a restored backup can fill
    # with anything; only a name the app could have stored is sent on.
    return name if name and safe_join(UPLOADS_DIR, name) and name.endswith(IMAGE_EXTENSIONS) else None


# ---------------------------------------------------------------------------
# Addresses for the links between the two pages
# ---------------------------------------------------------------------------

def _request_host(request: Optional[Request]) -> str:
    host = (request.url.hostname if request is not None else None) or "localhost"
    return f"[{host}]" if ":" in host else host     # IPv6 literal


def main_url(request: Optional[Request]) -> str:
    """Where the main app is, for the result card and the header link: the
    configured address, else the secure address while HTTPS is on (the
    Traefik route's, or the app's own while its listener runs), else plain
    http on the name the picker was opened at."""
    if MAIN_PUBLIC_URL:
        return MAIN_PUBLIC_URL
    proxy_https = _web_address("RECIPE_APP_HTTPS_URL")
    if proxy_https:
        return proxy_https
    try:
        st = https_setup.status()
        if st.get("enabled") and st.get("url") and st.get("listening"):
            return st["url"].rstrip("/")
    except Exception:
        log.exception("Couldn't read the HTTPS status for the picker's link")
    return f"http://{_request_host(request)}:{MAIN_PUBLIC_PORT}"


def picker_url(request: Optional[Request]) -> str:
    """Where the picker is, for the main app's top bar and Settings. Always
    plain http:// unless RECIPE_APP_PICKER_PUBLIC_URL says otherwise (the
    picker itself has no HTTPS), even when the main app was opened over it."""
    if PICKER_PUBLIC_URL:
        return PICKER_PUBLIC_URL + "/"
    return f"http://{_request_host(request)}:{PICKER_PUBLIC_PORT}/"


# ---------------------------------------------------------------------------
# The switch (saved in the database) and the listener
# ---------------------------------------------------------------------------

def saved_enabled() -> bool:
    db = SessionLocal()
    try:
        s = db.get(models.PickerSettings, 1)
        return bool(s and s.enabled)
    finally:
        db.close()


def _save_enabled(on: bool):
    db = SessionLocal()
    try:
        s = db.get(models.PickerSettings, 1)
        if s is None:
            s = models.PickerSettings(id=1)
            db.add(s)
        s.enabled = bool(on)
        db.commit()
    finally:
        db.close()


_app: Optional[FastAPI] = None        # the picker port's app (create_app)
_server = None
_server_task: Optional[asyncio.Task] = None
_listen_error: Optional[str] = None   # "port_in_use" / "failed", until the next start
_switch_lock: Optional[tuple] = None  # (event loop, asyncio.Lock)


def running() -> bool:
    return _server is not None and bool(_server.started)


def status(request: Optional[Request] = None) -> dict:
    """enabled: switched on AND the listener is running (what the main
    app's top-bar link goes by). on: the saved switch. listen_error: why it
    isn't running while switched on."""
    on, up = saved_enabled(), running()
    return {
        "enabled": on and up,
        "url": picker_url(request) if on and up else None,
        "on": on,
        "port": LISTEN_PORT,
        "public_port": PICKER_PUBLIC_PORT,
        "listen_error": _listen_error if on and not up else None,
    }


def _make_server(app: FastAPI):
    import uvicorn

    class Server(uvicorn.Server):
        # The main server owns the process's signal handling.
        @contextlib.contextmanager
        def capture_signals(self):
            yield

    # Proxy headers and FORWARDED_ALLOW_IPS as on the main port, so the rate
    # limit counts the real visitor behind a proxy. log_config and log_level
    # None: uvicorn's loggers are process-wide, and any other value
    # reconfigures them for the main port too (https_setup._make_server).
    config = uvicorn.Config(app, host="0.0.0.0", port=LISTEN_PORT, lifespan="off",
                            log_config=None, log_level=None, server_header=False, proxy_headers=True)
    return Server(config)


async def _serve(server):
    global _server, _server_task, _listen_error
    try:
        await server.serve()
    except SystemExit:
        # uvicorn exits the process when it can't bind; here that must only
        # end this listener, never the app.
        log.error("What to eat?: couldn't listen on port %s (in use?)", LISTEN_PORT)
        if _server is server:
            _listen_error = "port_in_use"
    except Exception:
        log.exception("What to eat? listener stopped")
        if _server is server:
            _listen_error = "failed"
    finally:
        if _server is server:
            _server = None
            _server_task = None


async def start_server():
    """Starts the picker listener unless it's already running."""
    global _server, _server_task, _listen_error
    if _server is not None or _app is None:
        return
    _listen_error = None
    server = _make_server(_app)
    _server = server
    _server_task = asyncio.create_task(_serve(server))
    # Give it a moment to bind, so a port problem shows up in the status.
    for _ in range(50):
        if server.started or _server is not server:
            break
        await asyncio.sleep(0.1)
    if server.started:
        log.info("What to eat?: listening on port %s", LISTEN_PORT)


async def stop_server():
    global _server, _server_task, _listen_error
    server, task = _server, _server_task
    _server, _server_task, _listen_error = None, None, None
    if server is not None:
        server.should_exit = True
    if task is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=10)
        log.info("What to eat?: stopped")


def _lock() -> asyncio.Lock:
    # One switch change at a time. Created for the running loop (tests run
    # more than one).
    global _switch_lock
    loop = asyncio.get_running_loop()
    if _switch_lock is None or _switch_lock[0] is not loop:
        _switch_lock = (loop, asyncio.Lock())
    return _switch_lock[1]


async def startup():
    """At app start: serve the picker if the saved switch is on (also after
    a restored backup, which is put back with the app stopped)."""
    if saved_enabled():
        await start_server()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

router = APIRouter()           # both ports
settings_router = APIRouter()  # main port only


@router.get("/api/picker/options")
def picker_options(request: Request, db: Session = Depends(get_db)):
    """The tags of the three built-in categories, and the pace values, that
    are on at least one pickable recipe, with how many."""
    drinks = _drink_recipe_ids()
    rows = (db.query(models.Tag.id, models.Tag.name, models.Tag.category, models.Tag.subgroup,
                     func.count(models.recipe_tags.c.recipe_id))
            .join(models.recipe_tags, models.recipe_tags.c.tag_id == models.Tag.id)
            .filter(models.Tag.category.in_(CATEGORIES), models.recipe_tags.c.recipe_id.notin_(drinks))
            .group_by(models.Tag.id).all())
    out = {c: [] for c in CATEGORIES}
    for tag_id, name, category, subgroup, count in sorted(rows, key=lambda r: _tag_order(r[1], r[2])):
        if count and not _is_cocktail_tag(category, name, subgroup):
            out[category].append({"id": tag_id, "name": name, "subgroup": subgroup, "count": count})
    pace_counts = dict(db.query(models.Recipe.cook_time_rating, func.count(models.Recipe.id))
                       .filter(models.Recipe.id.notin_(drinks), models.Recipe.cook_time_rating.in_(PACES))
                       .group_by(models.Recipe.cook_time_rating).all())
    out["pace"] = [{"value": p, "count": pace_counts[p]} for p in PACES if pace_counts.get(p)]
    out["total"] = db.query(func.count(models.Recipe.id)).filter(models.Recipe.id.notin_(drinks)).scalar() or 0
    out["main_url"] = main_url(request)
    return out


def weighted_choice(candidates: list, rng: random.Random = None) -> int:
    """One recipe id from [(id, tastiness_rating)], tastier ones slightly
    more often (TASTINESS_WEIGHTS)."""
    rng = rng or _rng
    ids = [rid for rid, _ in candidates]
    weights = [TASTINESS_WEIGHTS.get(rating, 1.0) for _, rating in candidates]
    return rng.choices(ids, weights=weights, k=1)[0]


def _selected_tags(db: Session, chosen: dict) -> dict:
    """The tag ids chosen per category, checked: an id that doesn't exist
    is ignored (deleted since the page loaded); one from another category
    is a mistake in the request."""
    wanted = {cat: list(dict.fromkeys(ids or []))[:MAX_IDS_PER_CATEGORY] for cat, ids in chosen.items()}
    all_ids = {i for ids in wanted.values() for i in ids}
    known = dict(db.query(models.Tag.id, models.Tag.category).filter(models.Tag.id.in_(all_ids)).all()) if all_ids else {}
    selected = {}
    for cat, ids in wanted.items():
        for i in ids:
            if i not in known:
                continue
            if known[i] != cat:
                raise HTTPException(status_code=422, detail="A chosen tag isn't in the category it was sent as.")
            selected.setdefault(cat, []).append(i)
    return selected


def _recipe_out(recipe: models.Recipe) -> dict:
    tags = sorted((t for t in recipe.tags if t.category in CATEGORIES),
                  key=lambda t: (CATEGORIES.index(t.category), _tag_order(t.name, t.category)))
    return {
        "id": recipe.id,
        "title": recipe.title,
        "image_path": _safe_image(recipe.image_path),
        "cook_time_rating": recipe.cook_time_rating if recipe.cook_time_rating in PACES else None,
        "tags": [{"name": t.name, "category": t.category} for t in tags],
    }


@router.get("/api/picker/random")
def picker_random(
    meal_type: list[int] | None = Query(default=None),
    cooking_style: list[int] | None = Query(default=None),
    main_ingredient: list[int] | None = Query(default=None),
    pace: list[Literal["quick", "moderate", "long"]] | None = Query(default=None),
    exclude: list[int] | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """One random pickable recipe matching the choices: OR within a
    category, AND across them. `exclude` holds the recipes already shown
    this round; when every match is in it, the answer says exhausted and the
    page starts a new round."""
    selected = _selected_tags(db, {"meal_type": meal_type, "cooking_style": cooking_style,
                                   "main_ingredient": main_ingredient})
    query = db.query(models.Recipe.id, models.Recipe.tastiness_rating).filter(
        models.Recipe.id.notin_(_drink_recipe_ids()))
    for ids in selected.values():
        query = query.filter(models.Recipe.id.in_(
            select(models.recipe_tags.c.recipe_id).where(models.recipe_tags.c.tag_id.in_(ids))))
    if pace:
        query = query.filter(models.Recipe.cook_time_rating.in_(set(pace)))
    matches = query.all()

    shown = set((exclude or [])[:MAX_EXCLUDE])
    left = [(rid, rating) for rid, rating in matches if rid not in shown]
    nothing = {"recipe": None, "shuffle_titles": [], "match_count": len(matches), "remaining": 0}
    if not matches:
        return {**nothing, "exhausted": False}
    if not left:
        return {**nothing, "exhausted": True}

    chosen = weighted_choice(left)
    others = [rid for rid, _ in matches if rid != chosen]
    shuffle_ids = _rng.sample(others, min(SHUFFLE_TITLES, len(others)))
    titles = dict(db.query(models.Recipe.id, models.Recipe.title)
                  .filter(models.Recipe.id.in_(shuffle_ids)).all()) if shuffle_ids else {}
    recipe = (db.query(models.Recipe).options(selectinload(models.Recipe.tags))
              .filter(models.Recipe.id == chosen).one())
    return {
        "recipe": _recipe_out(recipe),
        "shuffle_titles": [titles[i] for i in shuffle_ids if i in titles],
        "match_count": len(matches),
        "remaining": len(left) - 1,
        "exhausted": False,
    }


@router.get("/api/picker/status")
def picker_status(request: Request):
    return status(request)


@settings_router.put("/api/picker/settings")
async def picker_settings(payload: schemas.PickerSettingsIn, request: Request):
    """Settings -> What to eat?: saves the switch and starts or stops the
    listener at once. A port that's in use leaves the switch on with the
    reason in listen_error, like the HTTPS listen error."""
    async with _lock():
        _save_enabled(payload.enabled)
        if payload.enabled:
            await start_server()
        else:
            await stop_server()
    return status(request)


# ---------------------------------------------------------------------------
# The picker port's app
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))


def _first_dir(*candidates: str) -> str:
    # The image copies the files next to the app (Dockerfile); a checkout
    # has them at the repository's top level (tests, running from source).
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]


PICKER_STATIC_DIR = _first_dir(os.path.join(_HERE, "..", "picker-static"),
                               os.path.join(_HERE, "..", "..", "frontend-picker"))
MAIN_STATIC_DIR = _first_dir(os.path.join(_HERE, "..", "static"),
                             os.path.join(_HERE, "..", "..", "frontend"))


def _not_found():
    return JSONResponse(status_code=404, content={"detail": "Not Found"})


def create_app(middleware: list) -> FastAPI:
    """The app served on the picker port. `middleware` is main.py's own
    (host check, rate limit, error logging, cache headers, language), in
    the order to add them (the last added runs first)."""
    global _app
    a = FastAPI(title="What to eat?", docs_url=None, redoc_url=None, openapi_url=None)
    a.include_router(router)

    @a.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @a.get("/")
    def page():
        path = os.path.join(PICKER_STATIC_DIR, "index.html")
        return FileResponse(path) if os.path.isfile(path) else _not_found()

    @a.get("/js/i18n.js")
    def i18n_js():
        # Shared with the main app: the language, the French catalog and the
        # tag labels.
        path = os.path.join(MAIN_STATIC_DIR, "js", "i18n.js")
        return FileResponse(path) if os.path.isfile(path) else _not_found()

    @a.get("/uploads/{name}")
    def photo(name: str):
        # Recipe photos only: a name the app could have stored (the same
        # allow-list and safe_join as everywhere else), with an image
        # extension.
        path = safe_join(UPLOADS_DIR, name)
        if not path or not name.endswith(IMAGE_EXTENSIONS) or not os.path.isfile(path):
            return _not_found()
        return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})

    if os.path.isdir(PICKER_STATIC_DIR):
        a.mount("/picker-static", StaticFiles(directory=PICKER_STATIC_DIR), name="picker-static")
    else:
        log.warning("No picker page files at %s: the What to eat? page won't load",
                    os.path.realpath(PICKER_STATIC_DIR))

    for m in middleware:
        a.middleware("http")(m)

    # Added last, so it runs first: nothing on this port takes anything but
    # GET, and every other request is a plain 404 (not the main port's 403
    # for a write without X-Requested-With, which would suggest the route
    # exists here).
    @a.middleware("http")
    async def read_only(request: Request, call_next):
        if request.method not in ("GET", "HEAD"):
            return _not_found()
        return await call_next(request)

    _app = a
    return a
