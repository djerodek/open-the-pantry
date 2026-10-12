"""Settings -> Learning: training, saving and switching on the learned
segmenter and tagger.

Models are retrained from the database in the background: shortly after
start-up, and a couple of minutes after recipes or tags change (a burst of
edits trains once). Training reads every recipe, cross-validates against
the built-in rules, and keeps a model only where it did better. It takes
seconds for a few hundred recipes.

Files, all in data/ml/ (not in backups: they are rebuilt from the recipes):
  model.json      what was trained, when, and how it scored
  segmenter.npz   the line model's weights (when in use)
  tags.npz        the tag models' weights (when any tag has one)
  settings.json   {"enabled": true|false}
Arrays are read with allow_pickle=False, so the files hold numbers only.
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
FORMAT = 1
RETRAIN_DELAY = 120          # seconds after the last change
STARTUP_DELAY = 30

_lock = threading.Lock()      # one training at a time
_timer_lock = threading.Lock()
_timer: threading.Timer | None = None
_state = {"training": False, "error": None}
_report: dict = {}


def _path(name):
    return os.path.join(ML_DIR, name)


def autotrain_allowed() -> bool:
    """Off in the test suite, where a background retrain would change what
    other tests' imports return."""
    return os.environ.get("RECIPE_APP_LEARNING_AUTOTRAIN", "1") != "0"


def enabled() -> bool:
    try:
        with open(_path("settings.json"), encoding="utf-8") as f:
            return bool(json.load(f).get("enabled", True))
    except (OSError, ValueError):
        return True


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


def _remove(name):
    try:
        os.remove(_path(name))
    except FileNotFoundError:
        pass


def _apply(seg_model, tag_model):
    """Hands the models to the importers (or takes them away)."""
    pdf_ingest.set_learned_segmenter(seg_model.segment if seg_model else None)
    tagger.set_learned_tagger(tag_model.suggest if tag_model else None)


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
    try:
        if report.get("segmenter", {}).get("status") == "active":
            with np.load(_path("segmenter.npz"), allow_pickle=False) as a:
                seg = segmenter.SegmenterModel.from_arrays(a)
            if seg.W.shape != (DIM, len(segmenter.LABELS)):
                seg = None
        if report.get("tags", {}).get("status") == "active":
            with np.load(_path("tags.npz"), allow_pickle=False) as a:
                tag = tag_ml.TagModel.from_arrays(a, report["tags"]["model_tags"])
            if tag.W.shape != (DIM, len(tag.tags)):
                tag = None
    except (OSError, ValueError, KeyError):
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


def train_now() -> dict:
    """Trains both models from the database and puts them in use. Returns
    the status. Waits if a training is already running."""
    global _report
    from ..database import SessionLocal
    with _lock:
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
            if seg_model:
                _write_npz("segmenter.npz", seg_model.arrays())
            else:
                _remove("segmenter.npz")
            if tag_model:
                _write_npz("tags.npz", tag_model.arrays())
                tag_report["model_tags"] = [list(t) for t in tag_model.tags]
            else:
                _remove("tags.npz")
            report = {"format": FORMAT, "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "seconds": round(time.monotonic() - started, 1),
                      "segmenter": seg_report, "tags": tag_report}
            _write_json("model.json", report)
            _report = report
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
        train_now()
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
