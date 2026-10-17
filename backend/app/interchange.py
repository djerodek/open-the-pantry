"""Recipes in standard formats: schema.org Recipe JSON-LD out, schema.org
JSON and Paprika in.

Export (Settings -> Backup & export -> Recipes as JSON, and Share -> Download
JSON on a recipe):
- one schema.org Recipe object per recipe (what recipe sites publish and
  most recipe managers can read), with the app's own fields that schema.org
  has no place for (tags with their groups, ratings, favourite, pace,
  difficulty, real-world cook time, notes) under "x-open-the-pantry", so
  importing the file back here loses nothing;
- the library: recipes/<id>-<name>.json plus images/<photo> in one zip.

Import (Settings -> Backup & export -> Import or restore):
- a schema.org Recipe .json (one recipe, a list, or an @graph), or a zip
  of them (this app's export, or another app's);
- a Paprika .paprikarecipes file: a zip with one gzip-compressed JSON
  object per recipe (name, ingredients and directions as newline-separated
  text, servings, times, categories, rating, on_favorites, difficulty,
  notes, source_url, photo_data as base64). Field names as documented by
  people who have taken the format apart; Paprika publishes no spec.

Nothing here touches the database: the parsers return ImportedRecipe
objects and main.import_recipes saves them (duplicate check, tags, photo).
Every archive is read with limits on entry count, entry size and total
unpacked size, and only by entry name, so a hostile zip can't write
anywhere or fill the disk.
"""
from __future__ import annotations

import base64
import binascii
import gzip
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field

from .ingestion.url_ingest import _flatten_instructions

EXT_KEY = "x-open-the-pantry"
EXT_VERSION = 1

MAX_ENTRIES = 20_000                  # files in one archive
MAX_ENTRY_BYTES = 25 * 1024 * 1024    # one unpacked file (a recipe or a photo)
MAX_TOTAL_BYTES = 2 * 1024 ** 3       # everything unpacked
MAX_RECIPES = 5_000                   # recipes in one import

# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

_ISO = re.compile(r"^P(?:\d+D)?(?:T(?:\d+H)?(?:\d+M)?(?:\d+S)?)?$", re.I)
_TIME_PART = re.compile(r"(\d+(?:[.,]\d+)?)\s*(d|days?|j|jours?|h|hrs?|hours?|heures?|m|mins?|minutes?)\b", re.I)


def iso_duration(text: str | None) -> str | None:
    """A time as schema.org wants it (ISO 8601, "PT1H20M"), from what the
    app stores: already ISO (JSON-LD imports keep it), or text such as
    "1 hr 20 min", "45 minutes", "1 h 30". None when it can't be read."""
    if not text:
        return None
    t = str(text).strip()
    if _ISO.match(t) and t.upper() not in ("P", "PT"):
        return t.upper()
    if re.fullmatch(r"\d+", t):
        return f"PT{int(t)}M"
    minutes = 0.0
    found = False
    for num, unit in _TIME_PART.findall(t):
        n = float(num.replace(",", "."))
        u = unit.lower()
        found = True
        if u.startswith(("d", "j")):
            minutes += n * 1440
        elif u.startswith("h"):
            minutes += n * 60
        else:
            minutes += n
    m = re.fullmatch(r"(\d+)\s*h\s*(\d+)", t, re.I)       # "1 h 30"
    if m:
        minutes, found = int(m[1]) * 60 + int(m[2]), True
    if not found or minutes <= 0:
        return None
    total = int(round(minutes))
    h, mm = divmod(total, 60)
    return "PT" + (f"{h}H" if h else "") + (f"{mm}M" if mm else "")


def _instructions(steps: list[str]) -> list[dict]:
    """HowToStep objects, with part names ("Sauce:") as HowToSection
    holding the steps after them."""
    out, section = [], None
    for text in steps:
        t = (text or "").strip()
        if not t:
            continue
        if t.endswith(":") and len(t.split()) <= 6:
            section = {"@type": "HowToSection", "name": t.rstrip(":").strip(), "itemListElement": []}
            out.append(section)
            continue
        step = {"@type": "HowToStep", "text": t}
        (section["itemListElement"] if section is not None else out).append(step)
    return [s for s in out if s.get("@type") != "HowToSection" or s["itemListElement"]]


def _ddhhmm(minutes: int | None) -> str | None:
    if not minutes:
        return None
    d, rest = divmod(int(minutes), 1440)
    h, m = divmod(rest, 60)
    return f"{d:02d}:{h:02d}:{m:02d}"


def recipe_to_jsonld(recipe, image: str | None = None) -> dict:
    """One saved recipe as a schema.org Recipe. `image`: what to put in
    "image" (a path inside the export zip), or None."""
    ingredients = [(i.raw_line or " ".join(p for p in (i.quantity, i.unit, i.name) if p)).strip()
                   for i in recipe.ingredients]
    tags = sorted(recipe.tags, key=lambda t: (t.category, t.name))
    doc = {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": recipe.title,
        "recipeIngredient": [i for i in ingredients if i],
        "recipeInstructions": _instructions([s.text for s in recipe.steps]),
    }
    if recipe.servings:
        doc["recipeYield"] = recipe.servings
    for key, value in (("prepTime", recipe.prep_time), ("cookTime", recipe.cook_time),
                       ("totalTime", recipe.total_time)):
        iso = iso_duration(value)
        if iso:
            doc[key] = iso
    meal = [t.name for t in tags if t.category == "meal_type"]
    if meal:
        doc["recipeCategory"] = meal
    if tags:
        doc["keywords"] = ", ".join(t.name for t in tags)
    if recipe.source_url and re.match(r"^https?://", recipe.source_url, re.I):
        doc["url"] = recipe.source_url
    if image:
        doc["image"] = image
    if recipe.notes:
        doc["description"] = recipe.notes
    if recipe.created_at:
        doc["dateCreated"] = recipe.created_at.isoformat()
    doc[EXT_KEY] = {
        "version": EXT_VERSION,
        "source_type": recipe.source_type,
        "tags": [{"name": t.name, "category": t.category, "subgroup": t.subgroup} for t in tags],
        "notes": recipe.notes,
        "favorite": bool(recipe.favorite),
        "to_review": bool(getattr(recipe, "needs_review", False)),
        "tastiness_rating": recipe.tastiness_rating,
        "cook_time_rating": recipe.cook_time_rating,
        "difficulty_rating": recipe.difficulty_rating,
        "actual_cook_time": _ddhhmm(recipe.actual_cook_time_minutes),
        # As stored: schema.org's times above are ISO, these are what the
        # recipe page shows.
        "prep_time": recipe.prep_time,
        "cook_time": recipe.cook_time,
        "total_time": recipe.total_time,
    }
    return doc


def safe_name(title: str, recipe_id: int) -> str:
    stem = re.sub(r"[^\w\- ]+", "", title or "", flags=re.UNICODE).strip().replace(" ", "-")[:60] or "recipe"
    return f"{recipe_id}-{stem}"


README = """Open the Pantry: recipes as schema.org JSON

recipes/  one file per recipe, a schema.org Recipe (https://schema.org/Recipe),
          the format recipe websites publish. Most recipe managers can
          import it. "x-open-the-pantry" holds this app's own fields (tags
          and their groups, ratings, favourite, notes...).
images/   the recipes' photos, named in each recipe's "image".

To bring these recipes back into Open the Pantry: Settings -> Backup &
export -> Import or restore, and choose this zip. They are added to the
library (recipes already there are skipped); nothing is replaced.
"""


def build_jsonld_zip(recipes, uploads_dir: str, dest_zip_path: str, lock=None) -> dict:
    """The library as recipes/*.json + images/*. Returns {"recipes", "images"}."""
    import contextlib
    count = images = 0
    with zipfile.ZipFile(dest_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", README)
        for r in recipes:
            image_ref = None
            if r.image_path:
                path = os.path.join(uploads_dir, os.path.basename(r.image_path))
                with (lock or contextlib.nullcontext()):
                    if os.path.isfile(path):
                        zf.write(path, f"images/{os.path.basename(r.image_path)}", compress_type=zipfile.ZIP_STORED)
                        image_ref = f"images/{os.path.basename(r.image_path)}"
                        images += 1
            doc = recipe_to_jsonld(r, image_ref)
            zf.writestr(f"recipes/{safe_name(r.title, r.id)}.json", json.dumps(doc, ensure_ascii=False, indent=1))
            count += 1
    return {"recipes": count, "images": images}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

class ImportFileError(ValueError):
    """The file can't be imported at all (wrong kind, damaged, too big)."""


@dataclass
class ImportedRecipe:
    title: str
    origin: str                              # file or entry name, for the report
    ingredients: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    servings: str | None = None
    prep_time: str | None = None
    cook_time: str | None = None
    total_time: str | None = None
    source_url: str | None = None
    source_type: str = "import"
    notes: str | None = None
    favorite: bool = False
    to_review: bool = False
    tastiness_rating: int | None = None
    cook_time_rating: str | None = None
    difficulty_rating: str | None = None
    actual_cook_time: str | None = None
    tags: list[dict] = field(default_factory=list)       # {name, category, subgroup}: kept as they are
    tag_names: list[str] = field(default_factory=list)   # labels from another app: matched to existing tags
    custom_tag_names: list[str] = field(default_factory=list)  # the person's own categories (Paprika): kept as Custom
    image_bytes: bytes | None = None
    image_url: str | None = None


def _text(value, limit: int) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
        if value is None:
            return None
    t = str(value).strip()
    return t[:limit].strip() or None


def _lines(value) -> list[str]:
    if isinstance(value, str):
        return [l.strip() for l in value.splitlines() if l.strip()]
    if isinstance(value, list):
        return [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
    return []


def _rating(value) -> int | None:
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 5 else None


_DIFFICULTY = {"easy": "easy", "facile": "easy", "medium": "medium", "moyen": "medium", "moyenne": "medium",
               "intermediate": "medium", "hard": "hard", "difficult": "hard", "difficile": "hard"}


def _image_from(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl")
    return value if isinstance(value, str) and value.strip() else None


def _recipe_items(data) -> list[dict]:
    """Every schema.org Recipe object in a decoded JSON document."""
    found = []

    def walk(node, depth=0):
        if depth > 6:
            return
        if isinstance(node, list):
            for n in node:
                walk(n, depth + 1)
        elif isinstance(node, dict):
            kinds = node.get("@type")
            kinds = kinds if isinstance(kinds, list) else [kinds]
            if "Recipe" in kinds:
                found.append(node)
            elif "@graph" in node:
                walk(node["@graph"], depth + 1)
    walk(data)
    return found


def from_jsonld(item: dict, origin: str, read_member=None) -> ImportedRecipe:
    """One schema.org Recipe object. read_member(name) -> bytes reads a
    relative "image" from the same zip, when there is one."""
    ext = item.get(EXT_KEY) if isinstance(item.get(EXT_KEY), dict) else {}
    instructions = item.get("recipeInstructions")
    steps = (_lines(instructions) if isinstance(instructions, str)
             else _flatten_instructions(instructions) if isinstance(instructions, (list, dict)) else [])
    r = ImportedRecipe(
        title=_text(item.get("name"), 500) or "Untitled Recipe",
        origin=origin,
        ingredients=_lines(item.get("recipeIngredient") or item.get("ingredients")),
        steps=steps,
        servings=_text(item.get("recipeYield"), 100),
        prep_time=_text(ext.get("prep_time") or item.get("prepTime"), 200),
        cook_time=_text(ext.get("cook_time") or item.get("cookTime"), 200),
        total_time=_text(ext.get("total_time") or item.get("totalTime"), 200),
        source_url=_text(item.get("url") if isinstance(item.get("url"), str) else None, 2000),
    )
    if ext:
        r.source_type = _text(ext.get("source_type"), 20) or "import"
        r.notes = _text(ext.get("notes"), 20_000)
        r.favorite = bool(ext.get("favorite"))
        r.to_review = ext.get("to_review") is True
        r.tastiness_rating = _rating(ext.get("tastiness_rating"))
        r.cook_time_rating = ext.get("cook_time_rating") if ext.get("cook_time_rating") in ("quick", "moderate", "long") else None
        r.difficulty_rating = ext.get("difficulty_rating") if ext.get("difficulty_rating") in ("easy", "medium", "hard") else None
        act = ext.get("actual_cook_time")
        r.actual_cook_time = act if isinstance(act, str) and re.fullmatch(r"\d{2}:\d{2}:\d{2}", act) else None
        r.tags = [{"name": _text(t.get("name"), 100), "category": _text(t.get("category"), 60) or "custom",
                   "subgroup": _text(t.get("subgroup"), 60)}
                  for t in (ext.get("tags") or [])[:50] if isinstance(t, dict) and _text(t.get("name"), 100)]
    else:
        names = []
        for key in ("recipeCategory", "recipeCuisine"):
            names += [n for n in _lines(item.get(key))]
        kw = item.get("keywords")
        names += [k.strip() for k in (kw.split(",") if isinstance(kw, str) else _lines(kw)) if k.strip()]
        r.tag_names = list(dict.fromkeys(n[:100] for n in names))[:50]
        rating = item.get("aggregateRating")
        if isinstance(rating, dict) and _rating(rating.get("ratingValue")) and not rating.get("ratingCount"):
            r.tastiness_rating = _rating(rating.get("ratingValue"))   # one person's own rating, not a site's average
    image = _image_from(item.get("image"))
    if image:
        if re.match(r"^https?://", image, re.I):
            r.image_url = image
        elif read_member:
            r.image_bytes = read_member(image)
    return r


def from_paprika(obj: dict, origin: str) -> ImportedRecipe:
    r = ImportedRecipe(
        title=_text(obj.get("name"), 500) or "Untitled Recipe",
        origin=origin,
        ingredients=_lines(obj.get("ingredients")),
        steps=[re.sub(r"^\s*\d+[.)]\s+", "", s) for s in _lines(obj.get("directions"))],
        servings=_text(obj.get("servings"), 100),
        prep_time=_text(obj.get("prep_time"), 200),
        cook_time=_text(obj.get("cook_time"), 200),
        total_time=_text(obj.get("total_time"), 200),
        source_url=_text(obj.get("source_url"), 2000),
        notes=_text(obj.get("notes"), 20_000),
        favorite=bool(obj.get("on_favorites")),
        tastiness_rating=_rating(obj.get("rating")),
        difficulty_rating=_DIFFICULTY.get((obj.get("difficulty") or "").strip().lower()),
        custom_tag_names=[c[:100] for c in _lines(obj.get("categories"))][:50],
    )
    if isinstance(obj.get("photo_data"), str) and obj["photo_data"].strip():
        try:
            r.image_bytes = base64.b64decode(obj["photo_data"], validate=False)
        except (binascii.Error, ValueError):
            r.image_bytes = None
    if not r.image_bytes:
        r.image_url = _image_from(obj.get("image_url"))
    return r


def _check_archive(zf: zipfile.ZipFile):
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ImportFileError(f"This archive has {len(infos)} files; the limit is {MAX_ENTRIES}.")
    if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
        raise ImportFileError("This archive unpacks to more than 2 GB.")


def _read(zf: zipfile.ZipFile, name: str, limit: int = MAX_ENTRY_BYTES) -> bytes | None:
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > limit or info.is_dir():
        return None
    with zf.open(info) as f:
        data = f.read(limit + 1)
    return data if len(data) <= limit else None


def _gunzip(data: bytes, limit: int = MAX_ENTRY_BYTES) -> bytes | None:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as g:
            out = g.read(limit + 1)
    except (OSError, EOFError):
        return None
    return out if len(out) <= limit else None


def _json(data: bytes):
    try:
        return json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def read_file(path: str, filename: str) -> tuple[list[ImportedRecipe], list[dict]]:
    """Recipes found in an uploaded file, and per-entry problems
    [{"origin", "error"}]. Raises ImportFileError for a file that can't be
    read at all."""
    with open(path, "rb") as f:
        head = f.read(4)
    if head.startswith(b"PK"):
        try:
            zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile:
            raise ImportFileError("This file is damaged or isn't a zip.")
        with zf:
            _check_archive(zf)
            return _read_zip(zf)
    if os.path.getsize(path) > MAX_ENTRY_BYTES:
        raise ImportFileError("This JSON file is larger than 25 MB.")
    with open(path, "rb") as f:
        data = _json(f.read())
    if data is None:
        raise ImportFileError("This file isn't JSON, a zip or a Paprika export.")
    items = _recipe_items(data)
    if not items:
        raise ImportFileError("There is no schema.org Recipe in this file.")
    return [from_jsonld(it, filename) for it in items[:MAX_RECIPES]], []


def _read_zip(zf: zipfile.ZipFile) -> tuple[list[ImportedRecipe], list[dict]]:
    recipes, problems = [], []
    names = [i.filename for i in zf.infolist() if not i.is_dir()]
    base_dirs = {}

    def reader_for(entry: str):
        folder = entry.rsplit("/", 1)[0] + "/" if "/" in entry else ""

        def read_member(rel: str):
            rel = rel.lstrip("/")
            if ".." in rel.split("/"):
                return None
            # "images/x.jpg" next to "recipes/": relative to the zip's root,
            # or to the recipe file's folder.
            return _read(zf, rel) or _read(zf, folder + rel)
        return read_member

    for name in names:
        if len(recipes) >= MAX_RECIPES:
            problems.append({"origin": name, "error": f"More than {MAX_RECIPES} recipes in one file; the rest were left out."})
            break
        low = name.lower()
        base = os.path.basename(name)
        if base.startswith(".") or "__macosx/" in low:
            continue
        if low.endswith(".paprikarecipe"):
            raw = _read(zf, name)
            obj = _json(_gunzip(raw) if raw and raw[:2] == b"\x1f\x8b" else raw or b"")
            if not isinstance(obj, dict):
                problems.append({"origin": base, "error": "Couldn't read this Paprika recipe."})
                continue
            recipes.append(from_paprika(obj, base))
        elif low.endswith(".json"):
            raw = _read(zf, name)
            data = _json(raw) if raw else None
            if data is None:
                problems.append({"origin": base, "error": "Couldn't read this JSON file."})
                continue
            items = _recipe_items(data)
            if not items and low.endswith("manifest.json"):
                continue
            if not items:
                problems.append({"origin": base, "error": "No schema.org Recipe in this file."})
                continue
            recipes.extend(from_jsonld(it, base, reader_for(name)) for it in items)
    if not recipes and not problems:
        raise ImportFileError("No recipes found in this zip (schema.org .json files or a Paprika export).")
    return recipes[:MAX_RECIPES], problems
