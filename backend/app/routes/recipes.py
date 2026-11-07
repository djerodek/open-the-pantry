"""Recipes and tags: create, list, edit, delete, merge, duplicate, Recently deleted, ratings,
notes, photos, the review queue, tags and tag groups, Learning, and the single-recipe exports."""
import json
import re
import os
import uuid
from typing import Literal, Optional
from datetime import timedelta
from fastapi import Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import case, asc, desc, func
from sqlalchemy.orm import Session, selectinload
from .. import models, schemas
from ..database import get_db, SessionLocal, UPLOADS_DIR, TMP_DIR
from ..ingestion.tagger import suggest_tags, explain_tags, meat_tag_names
from ..export import render_recipe_html, render_recipe_pdf
from ..backup import remove_upload_file
from ..time_utils import available_time_buckets
from .. import review
from .. import i18n
from .. import picker
from .. import search
from .. import duplicates
from .. import interchange
from ..ml import service as learning
from ..file_validation import safe_join
from .common import _apply_tags, _attachment_disposition, _copy_upload, _delete_recipe_files, _demote_to_tmp, _get_or_create_tags, _ingredient_fields, _iso_utc, _logged_minutes, _promote_temp_file, _split_keywords, _utc_now, refresh_user_keywords
from fastapi import APIRouter
from ..logging_setup import get_logger

log = get_logger("app")
router = APIRouter()


# ---------------------------------------------------------------------------
# Recipe CRUD
# ---------------------------------------------------------------------------

@router.post("/api/recipes", response_model=schemas.RecipeOut)
def create_recipe(payload: schemas.RecipeCreate, db: Session = Depends(get_db)):
    # Promoting the file (a filesystem move) can't be rolled back by the DB
    # transaction below it -- if anything after this fails, the except
    # block demotes it back to TMP_DIR so it doesn't become a permanent
    # orphan with no recipe referencing it.
    promoted_image = _promote_temp_file(payload.image_path)
    try:
        recipe = models.Recipe(
            title=payload.title,
            source_type=payload.source_type,
            source_url=payload.source_url,
            servings=payload.servings,
            prep_time=payload.prep_time,
            cook_time=payload.cook_time,
            total_time=payload.total_time,
            image_path=promoted_image,
            raw_text=payload.raw_text,
            ocr_confidence=payload.ocr_confidence,
            favorite=payload.favorite,
            tastiness_rating=payload.tastiness_rating,
            cook_time_rating=payload.cook_time_rating,
            difficulty_rating=payload.difficulty_rating,
            language=payload.language,
            actual_cook_time_minutes=_logged_minutes(payload.actual_cook_time),
        )
        db.add(recipe)
        db.flush()

        for pos, ing in enumerate(payload.ingredients):
            db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **_ingredient_fields(ing)))

        for pos, step_text in enumerate(payload.steps):
            db.add(models.Step(recipe_id=recipe.id, position=pos, text=step_text))

        _apply_tags(db, recipe, payload.tags)

        db.commit()
    except Exception:
        log.debug("create_recipe: caught error, continuing", exc_info=True)
        db.rollback()
        _demote_to_tmp(promoted_image)
        raise

    db.refresh(recipe)
    return recipe


MAX_TAG_FILTERS = 50


@router.get("/api/recipes", response_model=list[schemas.RecipeSummaryOut])
def list_recipes(
    response: Response,
    q: str | None = Query(default=None, max_length=500),
    tags: list[str] | None = Query(default=None),        # repeated ?tags=A&tags=B, AND logic
    max_minutes: Optional[schemas.DbInt] = None,   # actual_cook_time_minutes <= max_minutes
    # Pace rating: repeated ?pace=quick&pace=moderate, OR logic (a recipe has
    # one pace, so AND across values could never match).
    pace: list[Literal["quick", "moderate", "long"]] | None = Query(default=None),
    favorite: bool | None = None,
    to_review: bool | None = None,
    sort: Literal["created", "title", "rating", "cook_time", "difficulty", "relevance"] = "created",
    direction: Literal["asc", "desc"] = "desc",
    db: Session = Depends(get_db),
):
    matched_via: dict[int, set[str]] = {}
    found = None

    if q and q.strip():
        # search.py: words to find (typos corrected when nothing matches),
        # "-word" to leave out, best-match ranks and excerpts. Every word
        # reaches FTS5 quoted; column sets are fixed literals there.
        found = search.run(db, q)
        query = db.query(models.Recipe)
        if found["matched"] is not None:
            matched_via = found["matched"]
            if not matched_via:
                if found["corrected"]:
                    response.headers["X-Search-Corrected"] = json.dumps(found["corrected"])
                return []
            query = query.filter(models.Recipe.id.in_(matched_via.keys()))
        if found["excluded"]:
            query = query.filter(models.Recipe.id.notin_(found["excluded"]))
        if found["corrected"]:
            # ASCII JSON, so it fits a header; the page shows "Showing results for".
            response.headers["X-Search-Corrected"] = json.dumps(found["corrected"])
    else:
        query = db.query(models.Recipe)

    if favorite:
        query = query.filter(models.Recipe.favorite.is_(True))

    if to_review:
        query = query.filter(models.Recipe.needs_review.is_(True))

    if pace:
        query = query.filter(models.Recipe.cook_time_rating.in_(pace))

    if max_minutes is not None:
        query = query.filter(
            models.Recipe.actual_cook_time_minutes.isnot(None),
            models.Recipe.actual_cook_time_minutes <= max_minutes,
        )

    # Stacked tag filters: AND logic -- a recipe must carry every selected tag.
    # At most MAX_TAG_FILTERS (as tags on a recipe): about 1,000 gave a 500
    # (SQLite's expression depth) and a 240 KB log entry each (review of 0158).
    if tags and len(tags) > MAX_TAG_FILTERS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_TAG_FILTERS} tags can be selected at once.")
    if tags:
        for tag_name in tags:
            query = query.filter(
                models.Recipe.id.in_(
                    db.query(models.Recipe.id)
                    .join(models.Recipe.tags)
                    .filter(models.Tag.name == tag_name)
                )
            )

    # selectinload: the summaries below read each recipe's tags, which
    # otherwise costs one extra query per recipe in the list.
    # "relevance" is best match first, while searching for words; without
    # any it's the default order.
    by_rank = sort == "relevance" and found is not None and found["matched"] is not None
    results = query.options(selectinload(models.Recipe.tags)).order_by(
        *_sort_clauses("created" if sort == "relevance" else sort, "desc" if sort == "relevance" else direction)).all()
    if by_rank:
        results.sort(key=lambda r: found["rank"].get(r.id, 0.0))     # stable: ties keep newest first

    summaries = []
    for r in results:
        summary = schemas.RecipeSummaryOut.model_validate(r)
        summary.has_notes = bool((r.notes or "").strip())
        if r.id in matched_via:
            summary.matched_via = sorted(matched_via[r.id])
        if found and r.id in found["snippets"]:
            summary.match_snippet = found["snippets"][r.id][:300]
        summaries.append(summary)
    return summaries


def _sort_clauses(sort: str, direction: str):
    """ORDER BY terms for the recipe list.

    Two things worth knowing about this:

    Unrated / untimed recipes always sort LAST, in both directions. Most
    recipes have no rating and no logged cook time, so a plain ascending
    sort by rating opens with a wall of blanks -- correct, and useless. The
    null-flag term below is deliberately not reversed with the direction,
    which means ascending and descending are not strict mirrors of each
    other. That is the intended trade.

    `NULLS LAST` is avoided in favour of an `x IS NULL` flag column: the
    former needs SQLite 3.30+, and this app ships wherever the host's
    Python happens to link its SQLite from.
    """
    is_desc = direction == "desc"
    order = desc if is_desc else asc

    if sort == "title":
        # Case-insensitive, or "apple" sorts after "Zucchini".
        primary = order(func.lower(models.Recipe.title))
        return (primary, models.Recipe.id.asc())

    if sort == "rating":
        col = models.Recipe.tastiness_rating
    elif sort == "cook_time":
        col = models.Recipe.actual_cook_time_minutes
    elif sort == "difficulty":
        # Stored as text, so it needs an explicit ordinal -- alphabetically
        # "hard" < "medium", which is not the order anyone means.
        col = case(
            (models.Recipe.difficulty_rating == "easy", 1),
            (models.Recipe.difficulty_rating == "medium", 2),
            (models.Recipe.difficulty_rating == "hard", 3),
            else_=None,
        )
    else:  # "created"
        # created_at is never null, so no null-flag term is needed.
        return (order(models.Recipe.created_at), models.Recipe.id.asc())

    # (col IS NULL) sorts 0 before 1, i.e. real values first, always.
    return (asc(col.is_(None)), order(col), models.Recipe.id.asc())


@router.get("/api/recipes/duplicates")
def recipe_duplicates(title: str | None = Query(default=None, max_length=500),
                      url: str | None = Query(default=None, max_length=4000),
                      exclude: Optional[schemas.DbInt] = None,
                      db: Session = Depends(get_db)):
    """Saved recipes with the same link or title (duplicates.py), for the
    warning on the review screen and Add manually. Declared before
    /api/recipes/{recipe_id}, which would otherwise take "duplicates"."""
    return duplicates.find(db, title=title, url=url, exclude_id=exclude)


@router.get("/api/time-buckets")
def get_time_buckets(db: Session = Depends(get_db)):
    """Available '<= X' time-filter options, generated only from data on
    hand (see time_utils.available_time_buckets)."""
    rows = db.query(models.Recipe.actual_cook_time_minutes).filter(
        models.Recipe.actual_cook_time_minutes.isnot(None)
    ).all()
    logged = [r[0] for r in rows]
    return available_time_buckets(logged)


@router.get("/api/library-check")
def library_check(db: Session = Depends(get_db)):
    """Settings -> Library check (0111): recipes missing something, and
    sets that look like the same recipe (duplicates.groups). Read only."""
    ing = dict(db.query(models.Ingredient.recipe_id, func.count()).group_by(models.Ingredient.recipe_id).all())
    steps = dict(db.query(models.Step.recipe_id, func.count()).group_by(models.Step.recipe_id).all())
    tagged = {rid for (rid,) in db.query(models.recipe_tags.c.recipe_id).distinct()}
    checks = {k: [] for k in ("no_ingredients", "no_steps", "no_tags", "no_photo", "photo_missing", "to_review")}
    rows = db.query(models.Recipe.id, models.Recipe.title, models.Recipe.image_path, models.Recipe.needs_review).all()
    for rid, title, image, to_review in sorted(rows, key=lambda r: (r[1] or "").casefold()):
        item = {"id": rid, "title": title}
        if not ing.get(rid):
            checks["no_ingredients"].append(item)
        if not steps.get(rid):
            checks["no_steps"].append(item)
        if rid not in tagged:
            checks["no_tags"].append(item)
        if not image:
            checks["no_photo"].append(item)
        else:
            path = safe_join(UPLOADS_DIR, image)
            if not path or not os.path.isfile(path):
                checks["photo_missing"].append(item)
        if to_review:
            checks["to_review"].append(item)
    return {"total": len(rows), "checks": checks, "duplicates": duplicates.groups(db)}


@router.get("/api/review-queue")
def review_queue(db: Session = Depends(get_db)):
    """How many recipes are still to review (the sidebar's "To review")."""
    return {"count": db.query(models.Recipe).filter(models.Recipe.needs_review.is_(True)).count()}


@router.get("/api/recipes/{recipe_id}/review-hints")
def recipe_review_hints(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """What to check in a recipe saved without review (review.py)."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    uncertain = learning.uncertain_lines(recipe.raw_text) if recipe.raw_text else None
    return review.hints(recipe, uncertain)


@router.post("/api/recipes/{recipe_id}/reviewed", response_model=schemas.RecipeOut)
def mark_reviewed(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """"Looks right": off the review list, and a training example from the
    next retrain (scheduled by retrain_after_changes)."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    recipe.needs_review = False
    db.commit()
    db.refresh(recipe)
    return recipe


@router.post("/api/recipes/{recipe_id}/duplicate", response_model=schemas.RecipeOut)
def duplicate_recipe(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """A copy to make a variant from: everything the recipe has (title with
    " (copy)", ingredients, steps, tags, times, servings, notes, ratings,
    source and original text), with its own copy of the photo so deleting
    one leaves the other's. Not copied: favourite and the "To review" mark.
    The page opens the copy in Edit."""
    src = db.get(models.Recipe, recipe_id)
    if not src:
        raise HTTPException(status_code=404, detail="Recipe not found")
    suffix = " (copie)" if i18n.current_lang.get() == "fr" else " (copy)"
    image = None
    if src.image_path:
        old_path = safe_join(UPLOADS_DIR, src.image_path)
        if old_path and os.path.isfile(old_path):
            image = f"copy-{uuid.uuid4().hex}{os.path.splitext(src.image_path)[1]}"
            _copy_upload(old_path, image, "duplicate_recipe")
    try:
        copy = models.Recipe(
            title=(src.title or "")[:500 - len(suffix)] + suffix,
            source_type=src.source_type, source_url=src.source_url, servings=src.servings,
            prep_time=src.prep_time, cook_time=src.cook_time, total_time=src.total_time,
            image_path=image, raw_text=src.raw_text, notes=src.notes, ocr_confidence=src.ocr_confidence,
            autotag_ignored=src.autotag_ignored, tastiness_rating=src.tastiness_rating,
            cook_time_rating=src.cook_time_rating, difficulty_rating=src.difficulty_rating,
            language=src.language, actual_cook_time_minutes=src.actual_cook_time_minutes,
        )
        db.add(copy)
        db.flush()
        for ing in src.ingredients:
            db.add(models.Ingredient(recipe_id=copy.id, position=ing.position, raw_line=ing.raw_line,
                                     quantity=ing.quantity, unit=ing.unit, name=ing.name))
        for step in src.steps:
            db.add(models.Step(recipe_id=copy.id, position=step.position, text=step.text))
        _apply_tags(db, copy, [schemas.TagIn(name=t.name, category=t.category, subgroup=t.subgroup) for t in src.tags])
        db.commit()
    except Exception:
        log.warning("duplicate_recipe: copy of %s not saved", recipe_id, exc_info=True)
        db.rollback()
        if image:
            remove_upload_file(os.path.join(UPLOADS_DIR, image))
        raise
    db.refresh(copy)
    log.info("Recipe %s duplicated as %s", recipe_id, copy.id)
    return copy


NOTES_MAX = 50_000   # schemas.NotesUpdate


@router.post("/api/recipes/merge", response_model=schemas.RecipeOut)
def merge_recipes(payload: schemas.MergeRequest, db: Session = Depends(get_db)):
    """Two recipes into one (0110). The "keep" recipe is updated in place
    with the part chosen from each; the other goes to Recently deleted, so
    it can be put back. Tags: both sets. Notes or tags that together go
    over a recipe's limits refuse the merge rather than being cut. A photo taken from the other recipe is copied, since
    the other keeps its own while it waits. The result counts as checked
    (not "To review")."""
    if payload.keep_id == payload.other_id:
        raise HTTPException(status_code=400, detail="Pick two different recipes.")
    keep, other = db.get(models.Recipe, payload.keep_id), db.get(models.Recipe, payload.other_id)
    if keep is None or other is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    take = payload.take
    pick = lambda part: other if getattr(take, part) == "other" else keep    # noqa: E731

    notes = {"keep": keep.notes, "other": other.notes,
             "both": "\n\n".join(n.strip() for n in (keep.notes, other.notes) if n and n.strip())}[take.notes]
    # Nothing is cut off without saying so (GPT review of 0119): the merge
    # is refused and the page shows why.
    if notes and len(notes) > NOTES_MAX:
        raise HTTPException(status_code=400, detail=f"The two recipes' notes together are longer than "
                            f"{NOTES_MAX:,} characters. Keep one recipe's notes, or shorten them first.")
    tag_count = len(keep.tags) + len([t for t in other.tags if t not in keep.tags])
    if tag_count > MAX_TAGS_PER_RECIPE:
        raise HTTPException(status_code=400, detail=f"Together these recipes have {tag_count} tags; a recipe "
                            f"can have at most {MAX_TAGS_PER_RECIPE}. Remove some tags first.")

    # The photo is copied only once the merge can go ahead: a refusal above
    # left the copy behind with nothing pointing at it (GPT review of 0123).
    new_image, dropped_image = keep.image_path, None
    if take.photo == "other" and other.image_path:
        src = safe_join(UPLOADS_DIR, other.image_path)
        if src and os.path.isfile(src):
            new_image = f"merge-{uuid.uuid4().hex}{os.path.splitext(other.image_path)[1]}"
            _copy_upload(src, new_image, "merge_recipes")
    elif take.photo == "other" or take.photo == "none":
        new_image = None
    if new_image != keep.image_path:
        dropped_image = keep.image_path
    try:
        keep.title = pick("title").title
        keep.image_path = new_image
        if take.content == "other":
            db.query(models.Ingredient).filter_by(recipe_id=keep.id).delete()
            db.query(models.Step).filter_by(recipe_id=keep.id).delete()
            for ing in other.ingredients:
                db.add(models.Ingredient(recipe_id=keep.id, position=ing.position, raw_line=ing.raw_line,
                                         quantity=ing.quantity, unit=ing.unit, name=ing.name))
            for step in other.steps:
                db.add(models.Step(recipe_id=keep.id, position=step.position, text=step.text))
            keep.raw_text, keep.source_type, keep.ocr_confidence = other.raw_text, other.source_type, other.ocr_confidence
            # A language set by hand describes the text it came with.
            keep.language = other.language
        d = pick("details")
        keep.servings, keep.prep_time, keep.cook_time, keep.total_time = d.servings, d.prep_time, d.cook_time, d.total_time
        keep.source_url = pick("source").source_url
        r = pick("ratings")
        (keep.favorite, keep.tastiness_rating, keep.cook_time_rating, keep.difficulty_rating,
         keep.actual_cook_time_minutes) = (r.favorite, r.tastiness_rating, r.cook_time_rating,
                                           r.difficulty_rating, r.actual_cook_time_minutes)
        keep.notes = notes or None
        keep.tags = list(keep.tags) + [t for t in other.tags if t not in keep.tags]
        keep.tags_text = " ".join(t.name for t in keep.tags)
        keep.needs_review = False
        other.deleted_at = _utc_now()
        db.commit()
        picker.forget_language(keep.id)
    except Exception:
        log.warning("merge_recipes: %s + %s not saved", keep.id, other.id, exc_info=True)
        db.rollback()
        if new_image and new_image != keep.image_path and new_image.startswith("merge-"):
            remove_upload_file(os.path.join(UPLOADS_DIR, new_image))
        raise
    if dropped_image:
        path = safe_join(UPLOADS_DIR, dropped_image)
        if path and os.path.isfile(path):
            try:
                remove_upload_file(path)
            except OSError:
                log.debug("merge_recipes: old photo not removed", exc_info=True)
    db.refresh(keep)
    log.info("Recipes merged: %s kept, %s to Recently deleted", keep.id, other.id)
    return keep


@router.get("/api/recipes/{recipe_id}", response_model=schemas.RecipeOut)
def get_recipe(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return recipe


@router.delete("/api/recipes/{recipe_id}")
def delete_recipe(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """To Recently deleted: hidden everywhere, restorable for
    RECENTLY_DELETED_DAYS, then deleted for good (with its files) by the
    sweep, or sooner from Recently deleted."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    recipe.deleted_at = _utc_now()
    db.commit()
    return {"ok": True, "days": RECENTLY_DELETED_DAYS}


@router.post("/api/recipes/batch-delete")
def batch_delete_recipes(payload: schemas.BatchDeleteRequest, db: Session = Depends(get_db)):
    """To Recently deleted, like a single delete. Per-item commit, so a
    failure mid-batch leaves the ones before it done and reported."""
    deleted, missing, failed = [], [], []
    for recipe_id in payload.ids:
        recipe = db.get(models.Recipe, recipe_id)
        if not recipe:
            missing.append(recipe_id)
            continue
        try:
            recipe.deleted_at = _utc_now()
            db.commit()
        except Exception as e:
            log.warning("batch_delete_recipes: caught error, continuing", exc_info=True)
            db.rollback()
            failed.append({"id": recipe_id, "error": str(e)})
            continue
        deleted.append(recipe_id)
    return {"deleted": deleted, "missing": missing, "failed": failed, "days": RECENTLY_DELETED_DAYS}


# ---------------------------------------------------------------------------
# Recently deleted (0109). A deleted recipe keeps its row and files, with
# deleted_at set, and is hidden from every query (models._hide_recently_
# deleted). It can be put back for RECENTLY_DELETED_DAYS; after that the
# sweep (start-up and every 6 hours) deletes it for good, files included.
# ---------------------------------------------------------------------------
RECENTLY_DELETED_DAYS = 30


def _binned(db: Session):
    return (db.query(models.Recipe).execution_options(include_deleted=True)
            .filter(models.Recipe.deleted_at.isnot(None)))


# Recipes deleted for good per commit: other writes get the lock between
# chunks instead of waiting for the whole batch (Claude review of 0138).
DELETE_CHUNK = 100


def _delete_for_good(db: Session, recipes: list) -> int:
    """Rows first, files once the commit has landed (a file deleted before
    a failed commit would leave a row pointing at nothing), a chunk at a
    time."""
    done = 0
    for start in range(0, len(recipes), DELETE_CHUNK):
        chunk = recipes[start:start + DELETE_CHUNK]
        for r in chunk:
            db.delete(r)
        db.commit()
        for r in chunk:
            _delete_recipe_files(r)
        done += len(chunk)
    return done


def purge_recently_deleted() -> int:
    """The sweep's part: recipes deleted more than RECENTLY_DELETED_DAYS ago."""
    db = SessionLocal()
    try:
        cutoff = _utc_now() - timedelta(days=RECENTLY_DELETED_DAYS)
        gone = _delete_for_good(db, _binned(db).filter(models.Recipe.deleted_at < cutoff).all())
    finally:
        db.close()
    if gone:
        log.info("Recently deleted: %d recipe(s) older than %d days deleted for good", gone, RECENTLY_DELETED_DAYS)
    return gone


@router.get("/api/recently-deleted")
def recently_deleted(db: Session = Depends(get_db)):
    now = _utc_now()
    out = []
    for r in _binned(db).order_by(models.Recipe.deleted_at.desc(), models.Recipe.id.desc()).all():
        left = r.deleted_at + timedelta(days=RECENTLY_DELETED_DAYS) - now
        out.append({"id": r.id, "title": r.title, "image_path": r.image_path,
                    "deleted_at": _iso_utc(r.deleted_at),
                    "days_left": max(0, left.days + (1 if left.seconds or left.microseconds else 0))})
    return {"days": RECENTLY_DELETED_DAYS, "recipes": out}


@router.post("/api/recently-deleted/{recipe_id}/restore")
def restore_deleted_recipe(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    recipe = _binned(db).filter(models.Recipe.id == recipe_id).first()
    if recipe is None:
        raise HTTPException(status_code=404, detail="Not in Recently deleted")
    recipe.deleted_at = None
    # A tag deleted meanwhile left its name in the search column.
    recipe.tags_text = " ".join(t.name for t in recipe.tags)
    db.commit()
    return {"ok": True, "id": recipe.id, "title": recipe.title}


@router.delete("/api/recently-deleted/{recipe_id}")
def delete_recipe_for_good(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    recipe = _binned(db).filter(models.Recipe.id == recipe_id).first()
    if recipe is None:
        raise HTTPException(status_code=404, detail="Not in Recently deleted")
    _delete_for_good(db, [recipe])
    return {"ok": True}


@router.delete("/api/recently-deleted")
def empty_recently_deleted(db: Session = Depends(get_db)):
    return {"deleted": _delete_for_good(db, _binned(db).all())}


# A recipe holds at most this many tags (RecipeCreate.tags).
MAX_TAGS_PER_RECIPE = 50


@router.post("/api/recipes/batch-tags")
def batch_tag_recipes(payload: schemas.BatchTagRequest, db: Session = Depends(get_db)):
    """Select mode -> Tags: add one existing tag to, or remove it from,
    every selected recipe, in one commit. Reports which recipes changed,
    which already had it (or didn't), which have no room for another tag
    and which no longer exist. The retrain middleware picks this path up."""
    tag = db.get(models.Tag, payload.tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    changed, unchanged, full, missing = [], [], [], []
    for recipe_id in dict.fromkeys(payload.ids):
        recipe = db.get(models.Recipe, recipe_id)
        if recipe is None:
            missing.append(recipe_id)
            continue
        has = any(t.id == tag.id for t in recipe.tags)
        if payload.action == "add" and not has:
            if len(recipe.tags) >= MAX_TAGS_PER_RECIPE:
                full.append(recipe_id)
                continue
            recipe.tags.append(tag)
        elif payload.action == "remove" and has:
            recipe.tags = [t for t in recipe.tags if t.id != tag.id]
        else:
            unchanged.append(recipe_id)
            continue
        recipe.tags_text = " ".join(t.name for t in recipe.tags)   # search column, see _apply_tags
        changed.append(recipe_id)
    db.commit()
    log.info("Batch tags: %s %r on %d recipe(s)", payload.action, tag.name, len(changed))
    return {"changed": changed, "unchanged": unchanged, "full": full, "missing": missing}


@router.put("/api/recipes/{recipe_id}", response_model=schemas.RecipeOut)
def update_recipe(recipe_id: schemas.DbId, payload: schemas.RecipeCreate, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    new_image = _promote_temp_file(payload.image_path, current=recipe.image_path)
    old_image = recipe.image_path
    try:
        recipe.title = payload.title
        recipe.servings = payload.servings
        recipe.prep_time = payload.prep_time
        recipe.cook_time = payload.cook_time
        recipe.total_time = payload.total_time
        recipe.image_path = new_image
        recipe.raw_text = payload.raw_text
        recipe.favorite = payload.favorite
        recipe.tastiness_rating = payload.tastiness_rating
        recipe.cook_time_rating = payload.cook_time_rating
        recipe.difficulty_rating = payload.difficulty_rating
        # Left out of the request: unchanged (schemas.RecipeCreate).
        if "language" in payload.model_fields_set:
            recipe.language = payload.language
        recipe.actual_cook_time_minutes = _logged_minutes(payload.actual_cook_time)
        # Saved from Edit: someone has looked at it.
        recipe.needs_review = False

        db.query(models.Ingredient).filter_by(recipe_id=recipe.id).delete()
        db.query(models.Step).filter_by(recipe_id=recipe.id).delete()
        for pos, ing in enumerate(payload.ingredients):
            db.add(models.Ingredient(recipe_id=recipe.id, position=pos, **_ingredient_fields(ing)))
        for pos, step_text in enumerate(payload.steps):
            db.add(models.Step(recipe_id=recipe.id, position=pos, text=step_text))

        _apply_tags(db, recipe, payload.tags)

        db.commit()
        picker.forget_language(recipe.id)          # its text may have changed
    except Exception:
        log.debug("update_recipe: caught error, continuing", exc_info=True)
        db.rollback()
        # New image was promoted on the assumption this commit would
        # succeed -- demote it back to TMP_DIR rather than leaving an
        # orphan, and leave the old image file untouched since the recipe
        # row wasn't actually updated.
        if old_image != new_image:
            _demote_to_tmp(new_image)
        raise

    # Only remove the superseded file once the swap has actually committed.
    if old_image and old_image != new_image:
        old_path = safe_join(UPLOADS_DIR, old_image)
        if old_path and os.path.isfile(old_path):
            remove_upload_file(old_path)

    db.refresh(recipe)
    return recipe


@router.patch("/api/recipes/{recipe_id}/rating", response_model=schemas.RecipeOut)
def update_rating(recipe_id: schemas.DbId, payload: schemas.RatingUpdate, db: Session = Depends(get_db)):
    """Lightweight endpoint for the card-level quick controls (favorite
    toggle, tastiness/pace/difficulty popovers) -- avoids resending the
    full recipe payload just to change one rating."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    # The three ratings are cleared by sending an explicit null, so "omitted"
    # and "set to null" have to be told apart -- `is not None` collapses them
    # and makes clearing silently do nothing (200 OK, no change). Pydantic's
    # model_fields_set only contains keys the client actually sent.
    provided = payload.model_fields_set

    # favorite is a non-nullable bool: absent means "leave alone", and there
    # is no null case to honour.
    if payload.favorite is not None:
        recipe.favorite = payload.favorite
    if "tastiness_rating" in provided:
        recipe.tastiness_rating = payload.tastiness_rating
    if "cook_time_rating" in provided:
        recipe.cook_time_rating = payload.cook_time_rating
    if "difficulty_rating" in provided:
        recipe.difficulty_rating = payload.difficulty_rating
    # Like the ratings: null or "" clears, absent leaves it alone.
    if "actual_cook_time" in provided:
        recipe.actual_cook_time_minutes = _logged_minutes(payload.actual_cook_time)
    if "servings" in provided:
        recipe.servings = (payload.servings or "").strip() or None

    db.commit()
    db.refresh(recipe)
    return recipe


@router.patch("/api/recipes/{recipe_id}/notes", response_model=schemas.RecipeOut)
def update_notes(recipe_id: schemas.DbId, payload: schemas.NotesUpdate, db: Session = Depends(get_db)):
    """Lightweight endpoint for the recipe-detail notes button -- avoids
    resending the full recipe payload just to add/edit/clear notes."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    recipe.notes = payload.notes or None  # '' and None both clear

    db.commit()
    db.refresh(recipe)
    return recipe


@router.patch("/api/recipes/{recipe_id}/image", response_model=schemas.RecipeOut)
def update_image(recipe_id: schemas.DbId, payload: schemas.ImageUpdate, db: Session = Depends(get_db)):
    """Lightweight endpoint for manually uploading, replacing, or removing
    a recipe's showcase image without resending the full recipe payload.
    payload.image_path is either a TMP_DIR draft filename from a prior
    POST /api/upload-image call (to promote as the new image) or omitted/
    null (to remove the current image entirely)."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")

    old_image = recipe.image_path
    if payload.image_path:
        # The contract is "pass a filename returned by a prior
        # POST /api/upload-image". Verify it actually exists as a draft
        # rather than storing a reference to a file that was never
        # uploaded -- _promote_temp_file passes unknown names through
        # unchanged, which would leave a permanently-broken image link.
        draft_path = safe_join(TMP_DIR, payload.image_path)
        if not (draft_path and os.path.isfile(draft_path)):
            raise HTTPException(
                status_code=400,
                detail="Unknown image reference -- upload the image first.",
            )
    new_image = _promote_temp_file(payload.image_path) if payload.image_path else None

    try:
        recipe.image_path = new_image
        db.commit()
    except Exception:
        log.debug("update_image: caught error, continuing", exc_info=True)
        db.rollback()
        if new_image and new_image != old_image:
            _demote_to_tmp(new_image)
        raise

    # Only remove the superseded file once the swap has actually committed.
    if old_image and old_image != new_image:
        old_path = safe_join(UPLOADS_DIR, old_image)
        if old_path and os.path.isfile(old_path):
            remove_upload_file(old_path)

    db.refresh(recipe)
    return recipe


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------

@router.post("/api/tags/auto-apply", response_model=schemas.AutoTagResult)
def auto_apply_tags(dry_run: bool = False, payload: Optional[schemas.AutoTagApply] = None,
                    db: Session = Depends(get_db)):
    """Run the tagger over every recipe and add the suggested tags each one
    is missing. Only adds -- tags already on a recipe, including ones added
    by hand, are left as they are, and removing unwanted ones is up to the
    user. dry_run=true reports what would be added without saving, for the
    confirmation step in Settings.

    Suggestions are otherwise made only when a recipe is added, so recipes
    saved before a tagger fix keep the old result (e.g. clams tagged
    Vegetarian before shellfish counted as Fish). The saved steps stand in
    for the page text used at import; imported page text includes site
    navigation that would add noise."""
    recipes = (db.query(models.Recipe)
               .options(selectinload(models.Recipe.ingredients), selectinload(models.Recipe.steps),
                        selectinload(models.Recipe.tags))
               .order_by(models.Recipe.id).all())
    picks = {s.id: set(s.tags) for s in (payload.selections or [])} if payload else {}
    meat_names = meat_tag_names()
    ignored = [r for r in recipes if r.autotag_ignored]
    recipes = [r for r in recipes if not r.autotag_ignored]
    changes = []
    for r in recipes:
        have = {(t.name, t.category) for t in r.tags}
        # Vegetarian is the tagger's "no meat keyword found"; if a meat or fish
        # tag is already on the recipe (say, added by hand), it's wrong.
        has_meat = any(t.category == "main_ingredient" and t.name in meat_names for t in r.tags)
        suggested = suggest_tags(
            r.title, [i.name or i.raw_line for i in r.ingredients], " ".join(s.text for s in r.steps)
        )
        new = [(n, c, sg) for n, c, sg in suggested
               if (n, c) not in have and not (n == "Vegetarian" and has_meat)]
        if payload is not None and payload.selections is not None and not dry_run:
            # Only what was left ticked in the preview. Names are checked
            # against the fresh suggestions, so the category always comes
            # from the tagger and a stale or made-up entry does nothing.
            picked = picks.get(r.id, set())
            new = [t for t in new if t[0] in picked]
        if new:
            changes.append((r, new))

    if not dry_run and changes:
        for r, new in changes:
            r.tags.extend(_get_or_create_tags(
                db, [schemas.TagIn(name=n, category=c, subgroup=sg) for n, c, sg in new]))
            r.tags_text = " ".join(t.name for t in r.tags)   # search column, see _apply_tags
        db.commit()
        log.info("Auto-tag: added %d tag(s) to %d recipe(s)", sum(len(n) for _, n in changes), len(changes))

    return schemas.AutoTagResult(
        dry_run=dry_run,
        recipes_ignored=len(ignored),
        recipes_scanned=len(recipes),
        recipes_changed=len(changes),
        tags_added=sum(len(n) for _, n in changes),
        changes=[schemas.AutoTagChange(id=r.id, title=r.title, added=[n for n, _, _ in new]) for r, new in changes],
    )


@router.patch("/api/recipes/{recipe_id}/autotag-ignore", response_model=schemas.RecipeOut)
def set_autotag_ignored(recipe_id: schemas.DbId, payload: schemas.AutoTagIgnore, db: Session = Depends(get_db)):
    """Leave a recipe out of (or put it back into) the Settings tag scan, so
    the list doesn't fill up with suggestions already turned down."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    recipe.autotag_ignored = payload.ignored
    db.commit()
    db.refresh(recipe)
    return recipe


@router.post("/api/tags/suggest", response_model=list[schemas.TagSuggestionOut])
def suggest_tags_for_form(payload: schemas.TagSuggestRequest):
    """The edit form's Suggest tags button: suggestions for what's typed in
    the form (saved or not), minus tags it already lists, each with where
    it came from. Saves nothing. Works for recipes skipped by the Settings
    scan too."""
    current = {t.strip().lower() for t in payload.current_tags}
    suggested = explain_tags(payload.title, payload.ingredients, " ".join(payload.steps))
    meat = meat_tag_names()
    has_meat = any(t in {m.lower() for m in meat} for t in current) or any(t["name"] in meat for t in suggested)
    return [t for t in suggested
            if t["name"].lower() not in current and not (t["name"] == "Vegetarian" and has_meat)]


# ---------------------------------------------------------------------------
# Settings -> Learning: models trained on the user's own recipes (app/ml)
# ---------------------------------------------------------------------------

@router.get("/api/learning")
def learning_status():
    return learning.status()


@router.post("/api/learning/train")
def learning_train():
    """Retrain now (takes seconds); returns the new status."""
    if not learning.enabled():
        raise HTTPException(status_code=409, detail="Learning is turned off.")
    result = learning.train_now()
    if result is None:
        raise HTTPException(status_code=409, detail="Training is already running.")
    return result


@router.put("/api/learning")
def learning_settings(payload: schemas.LearningSettingsIn):
    return learning.set_enabled(payload.enabled)


# ---------------------------------------------------------------------------
# Tag groups and tag definitions (Settings -> Tag groups)
# ---------------------------------------------------------------------------

@router.get("/api/tag-groups", response_model=list[schemas.TagGroupOut])
def list_tag_groups(db: Session = Depends(get_db)):
    return db.query(models.TagGroup).order_by(models.TagGroup.position, models.TagGroup.label).all()


@router.post("/api/tag-groups", response_model=schemas.TagGroupOut)
def create_tag_group(payload: schemas.TagGroupIn, db: Session = Depends(get_db)):
    label = payload.label.strip()
    if db.query(models.TagGroup).filter(func.lower(models.TagGroup.label) == label.lower()).first():
        raise HTTPException(status_code=409, detail=f'There is already a group called "{label}".')
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-") or "group"
    key, n = f"g-{slug}", 2
    while db.get(models.TagGroup, key):
        key, n = f"g-{slug}-{n}", n + 1
    group = models.TagGroup(key=key, label=label, builtin=False,
                            position=100 + db.query(models.TagGroup).count())
    db.add(group)
    db.commit()
    return group


@router.delete("/api/tag-groups/{key}")
def delete_tag_group(key: str, db: Session = Depends(get_db)):
    """Deletes a group added in Settings and its tags, which are removed
    from any recipes that have them. Built-in groups can't be deleted."""
    group = db.get(models.TagGroup, key)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if group.builtin:
        raise HTTPException(status_code=400, detail="Built-in groups can't be deleted.")
    tags = db.query(models.Tag).filter_by(category=key).all()
    learning.drop_known_tags([(t.name, t.category) for t in tags])
    touched = _remove_tags(db, tags)
    db.delete(group)
    db.commit()
    refresh_user_keywords(db)
    return {"deleted_tags": len(tags), "recipes_changed": touched}


def _remove_tags(db: Session, tags: list) -> int:
    """Detach tags from their recipes (keeping each recipe's search column
    right) and delete them. Returns how many recipes changed."""
    touched = set()
    for tag in tags:
        for r in list(tag.recipes):
            r.tags.remove(tag)
            r.tags_text = " ".join(t.name for t in r.tags)
            touched.add(r.id)
        db.delete(tag)
    return len(touched)


@router.post("/api/tags", response_model=schemas.TagOut)
def create_tag(payload: schemas.TagDefIn, db: Session = Depends(get_db)):
    """Add a tag to a group, with the words the tagger should look for.
    If the tag already exists there, its keywords are updated instead."""
    if not db.get(models.TagGroup, payload.category):
        raise HTTPException(status_code=400, detail="No such group.")
    name = payload.name.strip()
    tag = db.query(models.Tag).filter(models.Tag.category == payload.category,
                                      func.lower(models.Tag.name) == name.lower()).first()
    if not tag:
        tag = models.Tag(name=name, category=payload.category, subgroup=None, user_defined=True)
        db.add(tag)
    tag.keywords = ", ".join(_split_keywords(payload.keywords))
    tag.rules_out_vegetarian = payload.rules_out_vegetarian and payload.category == "main_ingredient"
    db.commit()
    db.refresh(tag)
    refresh_user_keywords(db)
    return tag


@router.put("/api/tags/{tag_id}", response_model=schemas.TagOut)
def update_tag(tag_id: schemas.DbId, payload: schemas.TagDefUpdate, db: Session = Depends(get_db)):
    """Change the extra keywords of any tag, built-in ones included."""
    tag = db.get(models.Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    tag.keywords = ", ".join(_split_keywords(payload.keywords))
    tag.rules_out_vegetarian = payload.rules_out_vegetarian and tag.category == "main_ingredient"
    db.commit()
    db.refresh(tag)
    refresh_user_keywords(db)
    return tag


@router.delete("/api/tags/{tag_id}")
def delete_tag(tag_id: schemas.DbId, db: Session = Depends(get_db)):
    """Delete a tag added in Settings, removing it from any recipes."""
    tag = db.get(models.Tag, tag_id)
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")
    if not tag.user_defined:
        raise HTTPException(status_code=400, detail="Only tags added in Settings can be deleted here.")
    learning.drop_known_tags([(tag.name, tag.category)])
    touched = _remove_tags(db, [tag])
    db.commit()
    refresh_user_keywords(db)
    return {"recipes_changed": touched}


@router.get("/api/tags", response_model=list[schemas.TagOut])
def list_tags(db: Session = Depends(get_db)):
    return db.query(models.Tag).order_by(models.Tag.category, models.Tag.subgroup, models.Tag.name).all()


@router.get("/api/recipes/{recipe_id}/export.pdf")
def export_pdf(recipe_id: schemas.DbId, include_notes: bool = False, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    abs_image = safe_join(UPLOADS_DIR, recipe.image_path) if recipe.image_path else None
    # The showcase image is deliberately never included in the shared PDF --
    # not a query param/user choice, unlike include_notes.
    pdf_bytes = render_recipe_pdf(recipe, abs_image, include_notes=include_notes, include_image=False)
    disposition = _attachment_disposition(recipe.title, "pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


@router.get("/api/recipes/{recipe_id}/export.html")
def export_html(recipe_id: schemas.DbId, include_notes: bool = False, db: Session = Depends(get_db)):
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    abs_image = safe_join(UPLOADS_DIR, recipe.image_path) if recipe.image_path else None
    html_str = render_recipe_html(recipe, abs_image, include_notes=include_notes)
    disposition = _attachment_disposition(recipe.title, "html")
    return Response(
        content=html_str,
        media_type="text/html",
        headers={"Content-Disposition": disposition},
    )


@router.get("/api/recipes/{recipe_id}/export.json")
def export_json(recipe_id: schemas.DbId, db: Session = Depends(get_db)):
    """One recipe as a schema.org Recipe .json (Share -> Download JSON).
    Without its photo: a single .json file has nowhere to put it."""
    recipe = db.get(models.Recipe, recipe_id)
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found")
    body = json.dumps(interchange.recipe_to_jsonld(recipe), ensure_ascii=False, indent=1)
    return Response(content=body, media_type="application/ld+json",
                    headers={"Content-Disposition": _attachment_disposition(recipe.title, "json")})
