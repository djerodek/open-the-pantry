"""Settings -> Learning: the line classifier for imports and the learned
tag suggestions (app/ml)."""
import io
import os
import zipfile

import numpy as np
import pytest

from tests.ml_corpus import corpus
from tests.ml_tag_corpus import tagged_recipes


# ---------------------------------------------------------------------------
# The numpy classifiers
# ---------------------------------------------------------------------------

def test_softmax_and_sigmoid_learn_and_round_trip(tmp_path):
    from app.ml import linear
    rng = np.random.default_rng(0)
    rows, y = [], []
    for _ in range(600):
        c = int(rng.integers(0, 3))
        rows.append({f"w{c}_{rng.integers(0, 10)}": 1.0, f"noise{rng.integers(0, 50)}": 1.0})
        y.append(c)
    X, y = linear.Sparse(rows), np.array(y)
    W = linear.train_softmax(X, y, 3)
    assert (linear.predict_softmax(X, W).argmax(1) == y).mean() > 0.98
    Y = np.stack([y == 0, y == 2], 1).astype(float)
    W2 = linear.train_sigmoid(X, Y)
    assert ((linear.predict_sigmoid(X, W2) > 0.5) == Y).mean() > 0.98
    np.savez_compressed(tmp_path / "w.npz", W=W.astype(np.float32))
    with np.load(tmp_path / "w.npz", allow_pickle=False) as a:
        assert np.allclose(a["W"], W, atol=1e-5)


def test_feature_columns_are_stable_across_processes():
    """Python's hash() is salted per process; the model would read garbage
    after a restart if columns came from it."""
    import subprocess, sys
    from app.ml.linear import feature_index
    code = "from app.ml.linear import feature_index; print(feature_index('w:flour'))"
    for seed in ("1", "2"):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             env={**os.environ, "PYTHONHASHSEED": seed})
        assert int(out.stdout.strip()) == feature_index("w:flour")


# ---------------------------------------------------------------------------
# Line labels from saved recipes
# ---------------------------------------------------------------------------

def test_align_labels_wrapped_lines_and_step_numbers():
    from app.ml import segmenter as S
    lines = ["Page 1 of 2", "Tomato Soup", "Ingredients", "1 can (400 ml) crushed", "tomatoes", "1 tsp salt",
             "Method", "3", "Heat the oil in a pot and", "add the onion.", "Simmer for 20 minutes.", "Notes"]
    labels, cov_i, cov_s = S.align(lines, "Tomato Soup", ["1 can (400 ml) crushed tomatoes", "1 tsp salt"],
                                   ["Heat the oil in a pot and add the onion.", "Simmer for 20 minutes."])
    got = [S.LABELS[l] for l in labels]
    assert got == ["other", "title", "other", "ingredient", "ingredient", "ingredient", "other", "step",
                   "step", "step", "step", "other"]
    assert cov_i == 1.0 and cov_s == 1.0


def test_a_short_line_in_both_takes_its_neighbours_section():
    """"salt and pepper." finishes a step here, though it's also an ingredient."""
    from app.ml import segmenter as S
    lines = ["Salt and pepper", "1 cup rice", "Cook the rice, then season with", "salt and pepper."]
    labels, _, _ = S.align(lines, "", ["Salt and pepper", "1 cup rice"],
                           ["Cook the rice, then season with salt and pepper."])
    assert [S.LABELS[l] for l in labels] == ["ingredient", "ingredient", "step", "step"]


def test_an_unrelated_text_is_not_used_for_training():
    from app.ml import segmenter as S
    ex = S.Example(1, "Soup", ["1 cup water", "1 tsp salt"], ["Boil the water.", "Add salt."],
                   raw_text="Totally different\ntext about\nsomething else")
    assert S.prepare(ex) and not ex.imported and ex.weight == S.PSEUDO_WEIGHT


# ---------------------------------------------------------------------------
# The learned segmenter
# ---------------------------------------------------------------------------

def _examples(data, start=0):
    from app.ml import segmenter as S
    return [S.Example(start + i, t, ing, st, raw) for i, (raw, t, ing, st, _) in enumerate(data)]


def test_segmenter_beats_the_rules_where_they_struggle_and_is_used():
    """Exact results on new documents, layout by layout: the learned model
    must get most of the hard layouts right (where the rules get none) and
    never do worse than the rules on any layout. (It used to check steps
    only "or layout == screenshot", which hid screenshot steps being wrong
    every time -- DeepSeek review of 0067.)"""
    from app.ml import segmenter as S
    from app.ingestion import pdf_ingest as P
    model, report = S.train(_examples(corpus(30, seed=11)))
    assert report["status"] == "active", report
    assert report["learned_accuracy"] >= report["rules_accuracy"] + S.MIN_GAIN
    for layout in ("screenshot", "cookbook", "email", "blog", "french"):
        learned = {"ingredients": 0, "steps": 0}
        rules_ok = {"ingredients": 0, "steps": 0}
        for raw, title, ings, steps, _ in corpus(10, seed=12, layouts=[layout]):
            rules = P.rule_segment_raw_text(raw)
            out = model.segment(raw, rules) or rules
            for key, want in (("ingredients", ings), ("steps", steps)):
                learned[key] += out[key] == want
                rules_ok[key] += rules[key] == want
        for key in learned:
            assert learned[key] >= rules_ok[key], (layout, key, learned, rules_ok)
            if layout in ("screenshot", "cookbook"):
                assert learned[key] >= 8, (layout, key, learned)


def test_circle_markers_and_misread_numbers_are_not_step_numbers():
    from app.ml.segmenter import _real_step_numbers
    assert _real_step_numbers(["1. Heat the oil.", "2. Add the onion.", "and stir.", "3. Serve."])
    assert _real_step_numbers(["Étape 1", "Chauffer.", "Étape 2", "Servir."])
    assert not _real_step_numbers(["9 Transfer the mix.", "3) Bake it."])          # misread circles
    assert not _real_step_numbers(["1. Heat the oil.", "@ Add the onion."])          # symbol leftover
    assert not _real_step_numbers(["Heat the oil.", "Add the onion."])


def test_segmenter_stays_off_when_the_rules_do_as_well():
    from app.ml import segmenter as S
    model, report = S.train(_examples(corpus(12, seed=3, layouts=["blog"])))
    assert model is None and report["status"] == "rules_better"


def test_segmenter_needs_enough_imported_recipes():
    from app.ml import segmenter as S
    typed = [S.Example(100 + i, "Typed", ["1 cup x", "2 tbsp y"], ["Mix.", "Bake."]) for i in range(30)]
    model, report = S.train(_examples(corpus(S.MIN_IMPORTED - 1)) + typed)
    assert model is None and report["status"] == "not_enough_data"
    assert report["imported_recipes"] == S.MIN_IMPORTED - 1 and report["other_recipes"] == 30


def test_learned_result_falls_back_to_the_rules_when_it_finds_nothing(monkeypatch):
    """A model that finds no ingredients or no steps where the rules did
    hands back None, so the rules' result is used."""
    from app.ml import segmenter as S
    from app.ingestion import pdf_ingest as P
    model, _ = S.train(_examples(corpus(16, seed=2)))
    raw = corpus(1, seed=4, layouts=["email"])[0][0]
    rules = P.rule_segment_raw_text(raw)
    n = len(P.prepare_lines(raw))
    monkeypatch.setattr(model, "label", lambda lines, r: ([S.OTHER] * n, np.zeros((n, 4))))
    assert model.segment(raw, rules) is None


def test_segment_raw_text_uses_the_hook_and_survives_its_failure():
    from app.ingestion import pdf_ingest as P
    raw = "Soup\nIngredients\n1 cup water\nInstructions\n1. Boil it."
    try:
        P.set_learned_segmenter(lambda text, rules: {**rules, "title_guess": "From the model"})
        assert P.segment_raw_text(raw)["title_guess"] == "From the model"
        P.set_learned_segmenter(lambda text, rules: None)
        assert P.segment_raw_text(raw) == P.rule_segment_raw_text(raw)

        def boom(text, rules):
            raise RuntimeError("broken model")
        P.set_learned_segmenter(boom)
        assert P.segment_raw_text(raw) == P.rule_segment_raw_text(raw)
    finally:
        P.set_learned_segmenter(None)


# ---------------------------------------------------------------------------
# Learned tags
# ---------------------------------------------------------------------------

def _tag_examples(data):
    from app.ml import tags as T
    from app.ingestion import tagger
    return [T.TagExample(i, t, ing, st, tags, {(n, c) for n, c, _ in tagger.keyword_suggestions(t, ing, st)})
            for i, (t, ing, st, tags) in enumerate(data)]


SUBGROUPS = {("Pasta night", "custom"): None, ("Curry", "custom"): None}


def test_tag_model_learns_tags_no_keyword_covers_and_stays_precise():
    from app.ml import tags as T
    model, report = T.train(_tag_examples(tagged_recipes(80)), SUBGROUPS)
    assert report["status"] == "active" and set(report["learned"]) == {"Curry", "Pasta night"}
    tp = fp = 0
    for title, ings, steps, tags in tagged_recipes(60, seed=99):
        got = {(n, c) for n, c, _ in model.suggest(title, ings, steps)}
        tp += len(got & tags)
        fp += len(got - tags)
    assert tp >= 20 and fp / max(tp + fp, 1) <= 0.1
    assert (model.thresholds >= T.FLOOR).all()


def test_tag_model_needs_enough_tagged_recipes():
    from app.ml import tags as T
    model, report = T.train(_tag_examples(tagged_recipes(T.MIN_RECIPES - 1)), SUBGROUPS)
    assert model is None and report["status"] == "not_enough_data"


def test_learned_tags_merge_with_the_keywords():
    from app.ingestion import tagger
    try:
        # A learned meat tag withdraws the keywords' "no meat found" Vegetarian.
        tagger.set_learned_tagger(lambda *a: [("Game", "main_ingredient", None)])
        got = tagger.suggest_tags("Braised greens", ["kale", "stock"], "")
        assert ("Game", "main_ingredient", None) in got and not any(n == "Vegetarian" for n, _, _ in got)
        # A learned Vegetarian doesn't override a meat keyword.
        tagger.set_learned_tagger(lambda *a: [("Vegetarian", "main_ingredient", None)])
        got = tagger.suggest_tags("Chicken soup", ["chicken"], "")
        assert not any(n == "Vegetarian" for n, _, _ in got)
        # Nothing doubled.
        tagger.set_learned_tagger(lambda *a: [("Chicken", "main_ingredient", None)])
        assert [n for n, _, _ in tagger.suggest_tags("Chicken soup", ["chicken"], "")].count("Chicken") == 1

        def boom(*a):
            raise RuntimeError("broken model")
        tagger.set_learned_tagger(boom)
        assert tagger.suggest_tags("Chicken soup", ["chicken"], "") == \
            tagger.keyword_suggestions("Chicken soup", ["chicken"], "")
    finally:
        tagger.set_learned_tagger(None)


# ---------------------------------------------------------------------------
# Settings -> Learning, through the API
# ---------------------------------------------------------------------------

@pytest.fixture
def learning_reset(client):
    from app.ml import service
    from app.ingestion import pdf_ingest, tagger
    yield service
    pdf_ingest.set_learned_segmenter(None)
    tagger.set_learned_tagger(None)
    if os.path.isdir(service.ML_DIR):
        for name in os.listdir(service.ML_DIR):
            os.remove(os.path.join(service.ML_DIR, name))
    service._report.clear()
    service._warned_settings = False


def test_train_status_toggle_and_reload(client, learning_reset):
    service = learning_reset
    from app.ingestion import pdf_ingest
    made = []
    try:
        for raw, title, ings, steps, layout in corpus(16, seed=21):
            r = client.post("/api/recipes", json={
                "title": title, "source_type": "screenshot", "raw_text": raw,
                "ingredients": [{"raw_line": i, "name": i} for i in ings], "steps": steps, "tags": []})
            assert r.status_code == 200, r.text
            made.append(r.json()["id"])

        r = client.post("/api/learning/train")
        assert r.status_code == 200
        st = r.json()
        assert st["enabled"] and not st["training"] and st["trained_at"]
        assert st["segmenter"]["status"] == "active", st
        assert pdf_ingest._learned_segmenter is not None
        assert any(n.startswith("segmenter-") and n.endswith(".npz") for n in os.listdir(service.ML_DIR))

        # A restart reads the saved model back.
        pdf_ingest.set_learned_segmenter(None)
        service.load()
        assert pdf_ingest._learned_segmenter is not None

        # Off: rules only, and no training.
        r = client.put("/api/learning", json={"enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False
        assert pdf_ingest._learned_segmenter is None
        assert client.post("/api/learning/train").status_code == 409
        service.load()
        assert pdf_ingest._learned_segmenter is None, "turned off survives a restart"
        r = client.put("/api/learning", json={"enabled": True})
        assert r.json()["enabled"] is True and pdf_ingest._learned_segmenter is not None
    finally:
        for rid in made:
            client.delete(f"/api/recipes/{rid}")


def test_status_before_any_training(client, learning_reset):
    st = client.get("/api/learning").json()
    assert st["segmenter"]["status"] == "untrained" and st["tags"]["status"] == "untrained"
    fr = client.get("/api/learning", headers={"X-App-Lang": "fr"})
    assert fr.status_code == 200


def test_changes_schedule_a_retrain_and_reads_dont(client, sample_recipe_payload, monkeypatch):
    from app.ml import service
    calls = []
    monkeypatch.setattr(service, "schedule_retrain", lambda *a, **k: calls.append(1))
    rid = client.post("/api/recipes", json={**sample_recipe_payload, "title": "Retrain trigger"}).json()["id"]
    assert len(calls) == 1
    client.get(f"/api/recipes/{rid}")
    client.get("/api/recipes")
    client.patch(f"/api/recipes/{rid}/rating", json={"favorite": True})
    client.post("/api/tags/suggest", json={"title": "x", "ingredients": [], "steps": [], "current_tags": []})
    assert len(calls) == 1
    client.delete(f"/api/recipes/{rid}")
    assert len(calls) == 2


def test_autotrain_is_off_in_the_test_suite():
    from app.ml import service
    assert not service.autotrain_allowed()


def test_model_files_hold_no_pickles_and_stay_out_of_backups(client, learning_reset):
    service = learning_reset
    made = []
    try:
        for raw, title, ings, steps, layout in corpus(10, seed=31):
            made.append(client.post("/api/recipes", json={
                "title": title, "source_type": "pdf", "raw_text": raw,
                "ingredients": [{"raw_line": i, "name": i} for i in ings], "steps": steps,
                "tags": []}).json()["id"])
        client.post("/api/learning/train")
        for name in os.listdir(service.ML_DIR):
            if name.endswith(".npz"):
                with np.load(os.path.join(service.ML_DIR, name), allow_pickle=False) as a:
                    assert all(a[k].dtype != object for k in a.files)
        z = zipfile.ZipFile(io.BytesIO(client.get("/api/backup/database.zip").content))
        assert not any(n.startswith("ml/") or n.endswith(".npz") for n in z.namelist())
    finally:
        for rid in made:
            client.delete(f"/api/recipes/{rid}")


# ---------------------------------------------------------------------------
# ChatGPT review of 0067
# ---------------------------------------------------------------------------

class _FakeTagModel:
    def __init__(self, tags):
        self.tags = tags

    def suggest(self, *a):
        return list(self.tags)


def test_a_deleted_tag_is_not_suggested_or_created_again(client, learning_reset):
    """The learned model still knows a tag deleted since its last training;
    batch and email imports save suggestions, which would have created the
    tag again (and not as a Settings tag, so it couldn't be deleted)."""
    service = learning_reset
    from app.ingestion import tagger
    g = client.post("/api/tag-groups", json={"label": "Review group"}).json()
    t = client.post("/api/tags", json={"name": "Retired tag", "category": g["key"], "keywords": ""}).json()
    service._apply(None, _FakeTagModel([("Retired tag", g["key"], None)]))
    try:
        assert ("Retired tag", g["key"], None) in tagger.suggest_tags("Soup", ["water"], "")
        assert client.delete(f"/api/tags/{t['id']}").status_code == 200
        assert not any(n == "Retired tag" for n, _, _ in tagger.suggest_tags("Soup", ["water"], ""))
        r = client.post("/api/tags/suggest", json={"title": "Soup", "ingredients": ["water"], "steps": [],
                                                   "current_tags": []})
        assert not any(x["name"] == "Retired tag" for x in r.json())
        assert not any(x["name"] == "Retired tag" for x in client.get("/api/tags").json())
    finally:
        service._apply(None, None)
        client.delete(f"/api/tag-groups/{g['key']}")


def test_a_second_training_request_is_refused_while_one_runs(client, learning_reset):
    service = learning_reset
    assert service._lock.acquire(blocking=False)
    try:
        r = client.post("/api/learning/train")
        assert r.status_code == 409 and "already running" in r.json()["detail"]
        assert service.train_now() is None
    finally:
        service._lock.release()


def test_each_training_switches_all_files_at_once(client, learning_reset):
    import json
    service = learning_reset
    made = []
    try:
        for raw, title, ings, steps, layout in corpus(12, seed=41):
            made.append(client.post("/api/recipes", json={
                "title": title, "source_type": "screenshot", "raw_text": raw,
                "ingredients": [{"raw_line": i, "name": i} for i in ings], "steps": steps,
                "tags": []}).json()["id"])
        for _ in range(2):
            assert client.post("/api/learning/train").status_code == 200
        with open(os.path.join(service.ML_DIR, "model.json")) as f:
            files = json.load(f)["files"]
        weights = sorted(n for n in os.listdir(service.ML_DIR) if n.endswith(".npz"))
        assert weights == sorted(v for v in files.values() if v), "older trainings' files are removed"
        assert files["segmenter"] and files["segmenter"].startswith("segmenter-")
    finally:
        for rid in made:
            client.delete(f"/api/recipes/{rid}")


def test_an_unreadable_settings_file_means_off(client, learning_reset):
    service = learning_reset
    assert service.enabled(), "no file: on (first run)"
    os.makedirs(service.ML_DIR, exist_ok=True)
    with open(os.path.join(service.ML_DIR, "settings.json"), "w") as f:
        f.write("{not json")
    assert not service.enabled()
    assert client.put("/api/learning", json={"enabled": True}).json()["enabled"] is True


# ---------------------------------------------------------------------------
# DeepSeek review of 0067
# ---------------------------------------------------------------------------

def test_a_dry_run_of_the_tag_scan_does_not_retrain(client, monkeypatch):
    from app.ml import service
    calls = []
    monkeypatch.setattr(service, "schedule_retrain", lambda *a, **k: calls.append(1))
    assert client.post("/api/tags/auto-apply?dry_run=true").status_code == 200
    assert calls == []


def test_name_is_translated_only_in_the_https_list():
    from app.i18n import translate
    fr = translate("Settings → HTTPS still needs: cPanel username, API token, cPanel address, name. "
                   "Fill them in with the cPanel login and save.", "fr")
    assert fr == ("Paramètres → HTTPS demande encore : nom d'utilisateur cPanel, jeton d'API, adresse cPanel, nom. "
                  "Remplissez-les avec l'identifiant cPanel et enregistrez.")
    assert translate("Settings → HTTPS still needs: name. Fill them in with the cPanel login and save.", "fr") \
        .startswith("Paramètres → HTTPS demande encore : nom.")
    assert translate("Unknown field: name, try again.", "fr") == "Unknown field: name, try again."


def test_the_damaged_key_message_says_what_to_do_here():
    from app.i18n import translate
    from app import https_setup
    import inspect
    src = inspect.getsource(https_setup.save_login)
    assert "Settings → Email ingest explains" not in src
    msg = ("The token wasn't saved: encryption.key in the data folder is damaged (the email password uses it "
           "too). Put back the copy it came from; or delete it and save the token again, which makes a new key, "
           "then enter the email password again in Settings → Email ingest.")
    assert translate(msg, "fr").startswith("Le jeton n'a pas été enregistré : encryption.key")


# ---------------------------------------------------------------------------
# Claude review round 6
# ---------------------------------------------------------------------------

def test_tag_learning_is_bounded_by_the_most_used_tags():
    """400 tags used to be trained together: 72 s and 2.8 GB."""
    import random
    from app.ml import tags as T
    rng = random.Random(3)
    names = [f"tag{i}" for i in range(120)]
    ex = []
    for i in range(80):
        tags = {(t, "custom") for t in names if rng.random() < 0.2}
        ex.append(T.TagExample(i, f"recipe {i}", ["flour", "water"], "mix", tags, set()))
    model, report = T.train(ex, {(t, "custom"): None for t in names})
    assert report["tags_considered"] == T.MAX_TAGS and report["tags_skipped"] == 120 - T.MAX_TAGS


def test_tag_learning_still_finds_the_learnable_tags_one_at_a_time():
    from app.ml import tags as T
    model, report = T.train(_tag_examples(tagged_recipes(80)), SUBGROUPS)
    assert set(report["learned"]) == {"Curry", "Pasta night"}
    assert model.W.shape[1] == 2


def test_a_recipe_carries_a_bounded_number_of_tags(client, sample_recipe_payload):
    many = [{"name": f"t{i}", "category": "custom"} for i in range(51)]
    assert client.post("/api/recipes", json={**sample_recipe_payload, "tags": many}).status_code == 422
    long_name = [{"name": "x" * 101, "category": "custom"}]
    assert client.post("/api/recipes", json={**sample_recipe_payload, "tags": long_name}).status_code == 422


def test_training_takes_a_heavy_job_slot(learning_reset, monkeypatch):
    service = learning_reset
    entered = []

    class Gate:
        def __enter__(self):
            entered.append(1)

        def __exit__(self, *a):
            return False
    monkeypatch.setattr(service, "_heavy_gate", Gate())
    monkeypatch.setattr(service, "_train_locked", lambda: {"ok": True})
    assert service.train_now() == {"ok": True} and entered == [1]


def test_main_hands_the_ocr_semaphore_to_learning(client):
    from app import main
    from app.ml import service
    assert service._heavy_gate is main._heavy_job_semaphore


# ---------------------------------------------------------------------------
# ChatGPT review of 0070
# ---------------------------------------------------------------------------

def test_a_tag_made_by_saving_a_recipe_can_be_suggested_after_training(client, sample_recipe_payload, learning_reset):
    """The list of existing tags was only refreshed by Settings, so a tag
    first created on a recipe was filtered out of learned suggestions."""
    service = learning_reset
    from app.ingestion import tagger
    r = client.post("/api/recipes", json={**sample_recipe_payload, "title": "New tag via recipe",
                                          "tags": [{"name": "Grandma's", "category": "custom"}]})
    rid = r.json()["id"]
    try:
        assert service._known_tags is None or ("Grandma's", "custom") not in service._known_tags
        assert client.post("/api/learning/train").status_code == 200
        assert ("Grandma's", "custom") in service._known_tags
        service._apply(None, _FakeTagModel([("Grandma's", "custom", None)]))
        assert ("Grandma's", "custom", None) in tagger.suggest_tags("Pie", ["apples"], "")
    finally:
        service._apply(None, None)
        client.delete(f"/api/recipes/{rid}")


def test_a_deletion_drops_the_tag_before_it_commits(client, learning_reset, monkeypatch):
    service = learning_reset
    from app import main
    g = client.post("/api/tag-groups", json={"label": "Race group"}).json()
    t = client.post("/api/tags", json={"name": "Racy", "category": g["key"], "keywords": ""}).json()
    seen = []
    real = main._remove_tags

    def spy(db, tags):
        seen.append(("Racy", g["key"]) in service._known_tags)   # at this point nothing is committed
        return real(db, tags)
    monkeypatch.setattr(main, "_remove_tags", spy)
    assert client.delete(f"/api/tags/{t['id']}").status_code == 200
    assert seen == [False]
    client.delete(f"/api/tag-groups/{g['key']}")


def test_known_tag_refreshes_are_atomic():
    import threading
    from app.ml import service
    order = []

    def slow_query():
        order.append("read")
        threading.Event().wait(0.05)
        return [("A", "custom")]
    t = threading.Thread(target=service.refresh_known_tags, args=(slow_query,))
    t.start()
    threading.Event().wait(0.01)
    service.drop_known_tags([("A", "custom")])       # waits for the refresh to finish
    order.append("dropped")
    t.join()
    assert order == ["read", "dropped"] and ("A", "custom") not in service._known_tags
