"""0153: link imports identify as the current stable Chrome. CI looks the
version up at each image build (.github/scripts/chrome_major.py) and passes
it as RECIPE_APP_CHROME_MAJOR; the app falls back to DEFAULT_CHROME_MAJOR."""
import importlib.util
import io
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _script():
    spec = importlib.util.spec_from_file_location("chrome_major", ROOT / ".github/scripts/chrome_major.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_lookup_takes_the_highest_major():
    m = _script()
    payload = {"versions": [{"name": "chrome/platforms/win/channels/stable/versions/156.0.8078.80",
                             "version": "156.0.8078.80"},
                            {"version": "157.0.8103.27"}, {"version": "155.0.8000.1"}]}
    assert m.major_from(payload) == 157
    for bad in ({}, {"versions": None}, {"versions": [{"version": "1.2.3"}]}, {"versions": [{"version": "99.0.1.2"}]},
                {"versions": [{"version": "1560.0.0.0"}]}, {"versions": [None, {"version": "157;rm -rf /.0.0.0"}]}):
        assert m.major_from(bad) is None, bad


def test_the_lookup_prints_the_major_or_nothing(monkeypatch, capsys):
    m = _script()
    monkeypatch.setattr(m.urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(json.dumps({"versions": [{"version": "156.0.8078.80"}]}).encode()))
    assert m.main() == 0
    assert capsys.readouterr().out.strip() == "156"

    def down(url, timeout):
        raise OSError("no network")
    monkeypatch.setattr(m.urllib.request, "urlopen", down)
    assert m.main() == 0                   # never fails the build
    out = capsys.readouterr()
    assert out.out == "" and "::warning::" in out.err


@pytest.mark.parametrize("env,want", [(None, "default"), ("", "default"), ("170", 170), ("120", "default"),
                                      ("abc", "default"), ("1700", "default"), ("15 6", "default")])
def test_the_user_agent_follows_the_build(monkeypatch, env, want):
    from app.ingestion import url_ingest
    if env is None:
        monkeypatch.delenv("RECIPE_APP_CHROME_MAJOR", raising=False)
    else:
        monkeypatch.setenv("RECIPE_APP_CHROME_MAJOR", env)
    assert url_ingest.chrome_major() == (url_ingest.DEFAULT_CHROME_MAJOR if want == "default" else want)


def test_the_built_in_version_is_current_enough():
    from app.ingestion import url_ingest
    assert url_ingest.DEFAULT_CHROME_MAJOR >= 156
    assert f"Chrome/{url_ingest.chrome_major()}.0.0.0" in url_ingest.USER_AGENT


def test_ci_passes_it_to_both_builds():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    jobs = {"docker-build": ci[ci.index("\n  docker-build:"):ci.index("\n  publish:")],
            "publish": ci[ci.index("\n  publish:"):]}
    for job, text in jobs.items():
        lookup = text.index("id: chrome")
        build = text.index("docker/build-push-action@")
        assert lookup < build, job
        assert "build-args: CHROME_MAJOR=${{ steps.chrome.outputs.major }}" in text[build:], job
        assert "python3 .github/scripts/chrome_major.py" in text[lookup:build], job
    docker = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    assert "ARG CHROME_MAJOR=\n" in docker and "ENV RECIPE_APP_CHROME_MAJOR=${CHROME_MAJOR}" in docker
