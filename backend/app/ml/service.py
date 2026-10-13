"""Settings -> Learning: training, saving and switching on the learned
segmenter and tagger.

Models are retrained from the database in the background: shortly after
start-up, and a couple of minutes after recipes or tags change (a burst of
edits trains once). Training reads every recipe, cross-validates against
the built-in rules, and keeps a model only where it did better. It takes
seconds for a few hundred recipes.

Files, all in data/ml/ (not in backups: they are rebuilt from the recipes):
  model.json            what was trained, when, how it scored, and which
                        weight files belong to that training
  segmenter-<gen>.npz   the line model's weights (when in use)
  tags-<gen>.npz        the tag models' weights (when any tag has one)
  settings.json         {"enabled": true|false}
Each training writes its weight files under a new <gen> and then replaces
model.json in one step, so a training that fails half-way (a full disk)
never pairs one training's segmenter with another's tags. Older weight
files are deleted afterwards. Arrays are read with allow_pickle=False, so
the files hold numbers only.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

import numpy as np

from ..database import DATA_DIR
from ..logging_setup import get_logger
from ..ingestion import pdf_ingest, tagger
from . import segmenter, tags as tag_ml
from .linear import DIM

log = get_logger("learning")

ML_DIR = os.path.join(DATA_DIR, "ml")
# Bumped when features or file layout change: older files are ignored and
# the models retrained.
FORMAT = 2
RETRAIN_DELAY = 120          # seconds after the last change
STARTUP_DELAY = 30

_lock = threading.Lock()      # one training at a time
_timer_lock = threading.Lock()
_timer: threading.Timer | None = None
_state = {"training": False, "error": None}
_report: dict = {}
# The tags that exist right now (main.refresh_user_keywords() sets it after
# every tag or group change). A learned suggestion for a tag deleted since
# the last training is dropped: saving it would create the tag again.
_known_tags: frozenset | None = None
# main.py hands over the semaphore that limits OCR and PDF work to a few at
# once; training takes a slot too, so it never runs alongside three OCR jobs.
_heavy_gate = None
_warned_settings = False


def _path(name):
    return os.path.join(ML_DIR, name)


def autotrain_allowed() -> bool:
    """Off in the test suite, where a background retrain would change what
    other tests' imports return."""
    return os.environ.get("RECIPE_APP_LEARNING_AUTOTRAIN", "1") != "0"


def enabled() -> bool:
    """On unless switched off. A settings file that exists but can't be
    read counts as off: it may be hiding an "off"."""
    global _warned_settings
    try:
        with open(_path("settings.json"), encoding="utf-8") as f:
            return bool(json.load(f)["enabled"])
    except FileNotFoundError:
        return True
    except (OSError, ValueError, KeyError, TypeError):
        if not _warned_settings:
            _warned_settings = True
            log.warning("Learning: %s can't be read; learning stays off until it is switched on again "
                        "in Settings", _path("settings.json"), exc_info=True)
        return False


def _write_json(name, data):
    os.makedirs(ML_DIR, exist_ok=True)
    tmp = _path(name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, _path(name))


def _write_npz(name, arrays):
    os.makedirs(ML_DIR, exist_ok=True)
    tmp = _path(name + ".tmp.npz")
    np.savez_compressed(tmp, **arrays)
    os.replace(tmp, _path(name))


def _remove_stale_weights(keep: set):
    for name in os.listdir(ML_DIR):
        if name.endswith(".npz") and name not in keep:
            try:
                os.remove(_path(name))
            except OSError:
                log.warning("Learning: couldn't remove old model file %s", name, exc_info=True)


def set_heavy_gate(gate):
    global _heavy_gate
    _heavy_gate = gate


_known_lock = threading.Lock()


def set_known_tags(tags):
    """{(name, category)} of every tag that exists."""
    global _known_tags
    _known_tags = frozenset((n, c) for n, c in tags)


def refresh_known_tags(query):
    """Reads the tags (query() -> [(name, category)]) and sets them, as one
    step: a refresh that read the table before a deletion can't overwrite
    one that read it after."""
    with _known_lock:
        set_known_tags(query())


def drop_known_tags(tags):
    """Before a deletion is committed: an import running meanwhile can no
    longer take the tag from the learned model."""
    global _known_tags
    with _known_lock:
        if _known_tags is not None:
            _known_tags = _known_tags - frozenset((n, c) for n, c in tags)


def _refresh_known_from_db():
    from .. import models
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        refresh_known_tags(lambda: db.query(models.Tag.name, models.Tag.category).all())
    finally:
        db.close()


def _learned_tagger(model):
    def suggest(title, ingredient_names, step_text):
        known = _known_tags
        return [t for t in model.suggest(title, ingredient_names, step_text)
                if known is None or (t[0], t[1]) in known]
    return suggest


def _apply(seg_model, tag_model):
    """Hands the models to the importers (or takes them away)."""
    pdf_ingest.set_learned_segmenter(seg_model.segment if seg_model else None)
    tagger.set_learned_tagger(_learned_tagger(tag_model) if tag_model else None)


def load():
    """Start-up: use the models saved by the last training, if any."""
    global _report
    try:
        with open(_path("model.json"), encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        _apply(None, None)
        return
    if report.get("format") != FORMAT:
        _apply(None, None)
        return
    _report = report
    if not enabled():
        _apply(None, None)
        return
    seg = tag = None
    files = report.get("files") or {}
    try:
        if report.get("segmenter", {}).get("status") == "active":
            with np.load(_path(files["segmenter"]), allow_pickle=False) as a:
                seg = segmenter.SegmenterModel.from_arrays(a)
            if seg.W.shape != (DIM, len(segmenter.LABELS)):
                seg = None
        if report.get("tags", {}).get("status") == "active":
            with np.load(_path(files["tags"]), allow_pickle=False) as a:
                tag = tag_ml.TagModel.from_arrays(a, report["tags"]["model_tags"])
            if tag.W.shape != (DIM, len(tag.tags)):
                tag = None
    except Exception:   # a truncated file is a BadZipFile, among others
        log.warning("Couldn't read the saved learning models; they'll be retrained", exc_info=True)
        seg = tag = None
    _apply(seg, tag)


def _examples(db):
    from .. import models
    from sqlalchemy.orm import selectinload
    recipes = (db.query(models.Recipe)
               .options(selectinload(models.Recipe.ingredients), selectinload(models.Recipe.steps),
                        selectinload(models.Recipe.tags))
               .order_by(models.Recipe.id).all())
    seg_ex, tag_ex = [], []
    for r in recipes:
        ings = [i.raw_line or i.name or "" for i in r.ingredients]
        ings = [i for i in ings if i.strip()]
        steps = [s.text for s in r.steps if (s.text or "").strip()]
        raw = r.raw_text if r.source_type in ("pdf", "screenshot", "email") else None
        seg_ex.append(segmenter.Example(r.id, r.title or "", ings, steps, raw))
        names = [i.name or i.raw_line or "" for i in r.ingredients]
        step_text = " ".join(steps)
        kw = {(n, c) for n, c, _ in tagger.keyword_suggestions(r.title, names, step_text)}
        tag_ex.append(tag_ml.TagExample(r.id, r.title or "", names, step_text,
                                        {(t.name, t.category) for t in r.tags}, kw))
    subgroups = {(t.name, t.category): t.subgroup for t in db.query(models.Tag).all()}
    return seg_ex, tag_ex, subgroups


def train_now() -> dict | None:
    """Trains both models from the database and puts them in use. Returns
    the status, or None if a training is already running (it isn't queued:
    a second request would only train on the same recipes again)."""
    if not _lock.acquire(blocking=False):
        return None
    try:
        gate = _heavy_gate
        if gate is None:
            return _train_locked()
        with gate:
            return _train_locked()
    finally:
        _lock.release()


def _train_locked() -> dict:
    global _report
    from ..database import SessionLocal
    if not enabled():
        return status()
    _state["training"] = True
    _state["error"] = None
    started = time.monotonic()
    try:
        db = SessionLocal()
        try:
            seg_ex, tag_ex, subgroups = _examples(db)
        finally:
            db.close()
        seg_model, seg_report = segmenter.train(seg_ex)
        tag_model, tag_report = tag_ml.train(tag_ex, subgroups)
        gen = f"{time.time_ns():x}"
        files = {"segmenter": None, "tags": None}
        if seg_model:
            files["segmenter"] = f"segmenter-{gen}.npz"
            _write_npz(files["segmenter"], seg_model.arrays())
        if tag_model:
            files["tags"] = f"tags-{gen}.npz"
            _write_npz(files["tags"], tag_model.arrays())
            tag_report["model_tags"] = [list(t) for t in tag_model.tags]
        report = {"format": FORMAT, "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "seconds": round(time.monotonic() - started, 1), "files": files,
                  "segmenter": seg_report, "tags": tag_report}
        os.makedirs(ML_DIR, exist_ok=True)
        _write_json("model.json", report)          # the switch to this training
        _remove_stale_weights({f for f in files.values() if f})
        _report = report
        # Tags can also be created by saving a recipe or an import, which
        # don't refresh the list; the model just trained may know them.
        _refresh_known_from_db()
        # Turned off while this ran: keep the files, but don't use them.
        if enabled():
            _apply(seg_model, tag_model)
        else:
            _apply(None, None)
        log.info("Learning: trained in %.1fs; segmenter %s (learned %s vs rules %s on %d imported recipes); "
                 "tags %s (%s)", report["seconds"], seg_report["status"], seg_report.get("learned_accuracy"),
                 seg_report.get("rules_accuracy"), seg_report["imported_recipes"], tag_report["status"],
                 ", ".join(tag_report.get("learned", [])) or "none")
    except Exception as e:
        _state["error"] = str(e) or e.__class__.__name__
        log.exception("Learning: training failed; keeping the previous models")
    finally:
        _state["training"] = False
    return status()


def schedule_retrain(delay: float = RETRAIN_DELAY):
    """Retrain after `delay` seconds; a later call pushes it back, so a
    batch of changes trains once."""
    global _timer
    if not autotrain_allowed() or not enabled():
        return
    with _timer_lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(delay, _run_scheduled)
        _timer.daemon = True
        _timer.start()


def _run_scheduled():
    try:
        if train_now() is None:
            # One is running, and it may have read the recipes before the
            # latest change: go again once it's done.
            schedule_retrain()
    except Exception:
        log.exception("Learning: scheduled training failed")


def set_enabled(on: bool) -> dict:
    _write_json("settings.json", {"enabled": bool(on)})
    if on:
        load()
        schedule_retrain(delay=1)
    else:
        _apply(None, None)
    return status()


def status() -> dict:
    seg = dict(_report.get("segmenter") or {})
    tg = dict(_report.get("tags") or {})
    tg.pop("model_tags", None)
    return {
        "enabled": enabled(),
        "training": _state["training"],
        "error": _state["error"],
        "trained_at": _report.get("trained_at"),
        "segmenter": seg or {"status": "untrained", "needed": segmenter.MIN_IMPORTED},
        "tags": tg or {"status": "untrained", "needed": tag_ml.MIN_RECIPES},
    }
