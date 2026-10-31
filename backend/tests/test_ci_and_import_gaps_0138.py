"""0138: CI actions on Node 24 and a weekly run; the two import gaps the
corpus's public cases showed: an email's greeting taken as the title and
its sign-off joined to the last step (p003), and prep and cook times from
recipe-scrapers dropped (p001). The workflow is read as text (no YAML
library in the test requirements)."""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first."""


def _ci():
    return (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def test_actions_are_the_node_24_releases():
    """The tag in each comment names a release whose action.yml says
    `using: node24`, checked when pinned (2026-10-30). checkout v4 and
    setup-python v5 were Node 20, forced onto 24 with a warning."""
    pinned = dict(re.findall(r"uses:\s*([\w./-]+)@[0-9a-f]{40} # (v[\d.]+)", _ci()))
    assert pinned == {
        "actions/checkout": "v7.0.1", "actions/setup-python": "v7.1.0",
        "docker/setup-buildx-action": "v4.4.1", "docker/build-push-action": "v7.4.0",
        "docker/setup-qemu-action": "v4.4.0", "docker/login-action": "v4.7.0",
        "docker/metadata-action": "v6.2.0", "peter-evans/dockerhub-description": "v5.0.0",
    }


def test_a_weekly_run_that_publishes_like_a_push():
    ci = _ci()
    on = ci[ci.index("\non:"):ci.index("\njobs:")]
    assert re.search(r'schedule:\n\s+- cron: "\d+ \d+ \* \* \d"', on) and "workflow_dispatch:" in on
    publish = ci[ci.index("\n  publish:"):]
    # Never for a pull request; for main whatever started it.
    assert "if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'" in publish
    # The base image pulled fresh on each build, so the weekly one has the newest.
    assert ci.count("pull: true") == 2


def seg(text):
    from app.ingestion.pdf_ingest import rule_segment_raw_text
    return rule_segment_raw_text(text)


ING = "Ingrédients\n250 g de farine\n1 pincée de sel\n\nPréparation\n1. Mélanger.\n2. Cuire 20 minutes.\n"


@pytest.mark.parametrize("greeting", ["Salut!", "Bonjour à tous,", "Hi Marc,", "Hello!", "Coucou !", "Dear Anna,"])
def test_a_greeting_is_not_the_title(greeting):
    assert seg(f"{greeting}\nVoici la recette promise.\n\nPâte brisée\n\n{ING}")["title_guess"] == "Pâte brisée"


def test_a_name_starting_with_a_greeting_word_is_still_a_name():
    assert seg(f"Hello Dolly Bars\n\n{ING}")["title_guess"] == "Hello Dolly Bars"


@pytest.mark.parametrize("sign_off", ["Bonne cuisine!\nJulie", "Bon appétit!", "Enjoy!\nSam", "Cheers,\nSam",
                                      "Best regards,\nSam", "À bientôt!"])
def test_a_sign_off_is_not_part_of_the_last_step(sign_off):
    assert seg(f"Pâte brisée\n\n{ING}\n{sign_off}\n")["steps"] == ["Mélanger.", "Cuire 20 minutes."]


def test_a_last_step_beginning_like_a_sign_off_stays():
    steps = seg("Pâte brisée\n\nIngredients\n1 cup flour\n\nMethod\n1. Mix.\n2. Best served warm.\n3. Enjoy with tea.\n")["steps"]
    assert steps == ["Mix.", "Best served warm.", "Enjoy with tea."]


def test_prep_and_cook_times_from_recipe_scrapers():
    from app.ingestion.url_ingest import _try_recipe_scrapers
    html = (ROOT / "backend/tests/corpus/p001-en-blog-json-ld/input.html").read_text(encoding="utf-8")
    r = _try_recipe_scrapers("https://www.example.com/pancakes", html)
    assert (r.prep_time, r.cook_time, r.total_time) == ("15", "20", "35")


def test_a_missing_time_is_none_not_the_word():
    from app.ingestion.url_ingest import _try_recipe_scrapers
    html = ('<script type="application/ld+json">{"@type": "Recipe", "name": "Toast", "recipeIngredient": '
            '["1 slice bread"], "recipeInstructions": "Toast it."}</script>')
    r = _try_recipe_scrapers("https://www.example.com/toast", html)
    assert r is None or (r.prep_time, r.cook_time, r.total_time) == (None, None, None)
