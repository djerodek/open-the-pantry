"""Browser tests: the pages in headless Chromium against a real server.

The server tests (tests/) check the API; these check what the pages do
with it, which is where several bugs got through: "null" above a recipe
without a photo (0082), "Change password" shown before any password
existed (0098), a duplicate warning skipped when the title changed during
the check (0094). Every test also fails on any JavaScript error or
Content-Security-Policy violation on the page.

Run from backend/: python -m pytest tests_browser
Needs requirements-browser.txt and a Chromium: `python -m playwright install
chromium`, or CHROMIUM_PATH pointing at one. Skipped when Playwright isn't
installed, and not collected by a plain `pytest` (testpaths = tests).
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
H = {"X-Requested-With": "OpenThePantry"}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    """A real app server (main and What to eat? ports) on a fresh data folder."""
    static = BACKEND / "static"
    made_link = False
    if not static.exists():
        static.symlink_to(ROOT / "frontend", target_is_directory=True)   # the Dockerfile copies it there
        made_link = True
    data = tempfile.mkdtemp(prefix="pantry-browser-")
    port, picker_port = _free_port(), _free_port()
    env = {**os.environ, "RECIPE_APP_DATA_DIR": data, "RECIPE_APP_LEARNING_AUTOTRAIN": "0",
           "RECIPE_APP_PICKER_PORT": str(picker_port), "RECIPE_APP_PICKER_PUBLIC_PORT": str(picker_port),
           "RECIPE_APP_MAIN_PUBLIC_PORT": str(port),
           # Every page load fetches a dozen things; the tests load many.
           "RECIPE_APP_RATE_LIMIT": "100000"}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                             "--port", str(port)], cwd=BACKEND, env=env,
                            stdout=subprocess.DEVNULL, stderr=open(os.path.join(data, "server.log"), "w"))
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                urllib.request.urlopen(base + "/healthz", timeout=1)
                break
            except OSError:
                time.sleep(0.25)
        else:
            raise RuntimeError("the server didn't start; see " + os.path.join(data, "server.log"))
        yield {"url": base, "picker_url": f"http://127.0.0.1:{picker_port}", "data": data}
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        if made_link:
            static.unlink()
        shutil.rmtree(data, ignore_errors=True)


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        path = os.environ.get("CHROMIUM_PATH")
        b = p.chromium.launch(**({"executable_path": path} if path else {}))
        yield b
        b.close()


_WATCH = """window.__cspViolations = [];
document.addEventListener('securitypolicyviolation', e => window.__cspViolations.push(e.violatedDirective + ' ' + e.blockedURI));"""


def wait_until(page, js: str, timeout: float = 10.0):
    """Polls `js` (an expression) until true. Not page.wait_for_function,
    which evaluates a string and so trips the app's own CSP (no eval)."""
    import time as _t
    end = _t.monotonic() + timeout
    while _t.monotonic() < end:
        if page.evaluate(js):
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"timed out waiting for: {js}")


class Page:
    """A phone-sized page with the checks every test gets."""

    def __init__(self, browser, locale="en", color_scheme="light", width=390):
        self.context = browser.new_context(viewport={"width": width, "height": 844}, locale=locale,
                                           color_scheme=color_scheme, service_workers="block")
        self.context.add_init_script(_WATCH)
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("dialog", lambda d: d.accept())

    def check_clean(self):
        violations = self.page.evaluate("window.__cspViolations || []")
        assert self.errors == [], f"JavaScript errors: {self.errors}"
        assert violations == [], f"CSP violations: {violations}"


@pytest.fixture
def page(browser):
    pages = []

    def make(**kw):
        p = Page(browser, **kw)
        pages.append(p)
        return p
    yield make
    try:
        for p in pages:
            p.check_clean()
    finally:
        for p in pages:
            p.context.close()


@pytest.fixture
def api(server):
    """Requests to the server outside the browser (seeding data)."""
    import httpx
    with httpx.Client(base_url=server["url"], headers=H, timeout=30) as c:
        yield c


def add_recipe(api, title, **extra):
    body = {"title": title, "source_type": "manual", "ingredients": [{"raw_line": "1 cup flour"}, {"raw_line": "2 eggs"}],
            "steps": ["Mix.", "Bake 20 minutes."], "tags": [], **extra}
    r = api.post("/api/recipes", json=body)
    assert r.status_code == 200, r.text
    return r.json()
