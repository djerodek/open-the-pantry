import os
import sys

import pytest


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("recipe-app-test-data")
    os.environ["RECIPE_APP_DATA_DIR"] = str(d)
    # The suite fires far more requests per minute from a single client
    # than any real user would, so the production rate-limit default
    # (120/min per IP) would throttle later tests with 429s that have
    # nothing to do with what they're actually testing. Raised here
    # rather than weakened in the app itself. Must be set before
    # app.main is imported -- the limit is read at import time.
    os.environ.setdefault("RECIPE_APP_RATE_LIMIT", "100000")
    return d


@pytest.fixture(scope="session")
def client(data_dir):
    """Session-scoped: app.main's single-instance lock and DB init are
    process-wide side effects at import time, so all tests in this session
    share one app instance and one (isolated, temp-dir) database. Tests
    should use distinctive titles/tags or clean up what they create rather
    than assume a pristine DB per test."""
    from fastapi.testclient import TestClient
    from app.main import app

    # X-Requested-With: every write from the app's own pages carries it (see
    # cross_site_guard); tests act as the app.
    with TestClient(app, headers={"X-Requested-With": "OpenThePantry"}) as c:  # context-manager form actually runs lifespan
        yield c


@pytest.fixture
def sample_recipe_payload():
    return {
        "title": "Pytest Sample Recipe",
        "source_type": "manual",
        "ingredients": [
            {"raw_line": "2 cups flour", "quantity": "2", "unit": "cup", "name": "flour"},
            {"raw_line": "1 tsp salt", "quantity": "1", "unit": "tsp", "name": "salt"},
        ],
        "steps": ["Mix ingredients", "Bake at 350F"],
        "tags": [],
    }


def text_font(size, bold=False):
    """A TrueType font for drawing test images that OCR reads back.

    The tests used to hard-code the DejaVu path, which only exists on
    Debian/Ubuntu with fonts-dejavu installed (Gemini review). Tries the
    common Linux and macOS locations, then Pillow's built-in scalable font
    (Pillow 10.1+), so the suite runs anywhere."""
    from PIL import ImageFont
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415478da6360000002000155075ce9c30000000049454e44ae426082"
)
