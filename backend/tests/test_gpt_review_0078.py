"""ChatGPT review of 0078.

- What to eat?: a chip change or Clear while a pick was loading or shuffling
  still showed that pick afterwards (and undid Clear). A pick now carries the
  generation it started in and is dropped if a chip change or Clear bumped it.
- Settings -> What to eat? says, when RECIPE_APP_API_KEY is set, that the key
  doesn't cover the page. Whether a key is set is reported on the main port
  only (where reading it already takes the key).
- Compose files and README: what to do when host port 8091 is taken; README:
  the no-repeat promise holds up to 500 matches.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
PICKER_JS = ROOT / "frontend-picker" / "picker.js"


@pytest.fixture(autouse=True)
def _data_dir(data_dir):
    """The app reads its data folder when first imported; set it first even
    when this file runs on its own."""


@pytest.mark.skipif(not PICKER_JS.exists(), reason="frontend not present")
def test_out_of_date_pick_is_dropped():
    js = PICKER_JS.read_text(encoding="utf-8")
    toggle = js[js.index("function toggleChip("):js.index("// Picking")]
    clear = js[js.index("function clearAll("):js.index('pickButton.addEventListener("click"')]
    pick = js[js.index("async function pick("):js.index("function clearAll(")]
    assert "state.generation += 1" in toggle
    assert "state.generation += 1" in clear and "state.cleared = true" in clear
    assert "const generation = state.generation;" in pick
    # Checked after every wait: each request, the shuffle, and a failure.
    waits = [i for i in range(len(pick)) if pick.startswith("await ", i)]
    assert len(waits) == 3
    for i in waits:
        assert "if (stale())" in pick[i:i + 200], pick[i:i + 80]
    assert "catch (err) {\n      if (stale()) return;" in pick
    # Nothing about the round or the recipe on screen changes until the pick
    # has survived the shuffle.
    after_shuffle = pick.index("await shuffle(")
    assert pick.index("state.current = recipe;") > after_shuffle
    assert pick.index("state.shown = shown.filter(") > after_shuffle
    # A chip change puts back what the shuffle replaced; Clear's empty area stays.
    assert "if (!state.cleared) restore();" in pick


def test_api_key_note_is_reported_on_the_main_port_only(client, monkeypatch):
    from app import main
    from app.main import picker_app
    assert client.get("/api/picker/status").json()["api_key_set"] is False
    monkeypatch.setattr(main, "API_KEY", "a-long-test-key")
    st = client.get("/api/picker/status", headers={"X-API-Key": "a-long-test-key"}).json()
    assert st["api_key_set"] is True
    st = client.put("/api/picker/settings", json={"enabled": False},
                    headers={"X-API-Key": "a-long-test-key"}).json()
    assert st["api_key_set"] is True
    # The open port never says whether the main app has a key.
    assert "api_key_set" not in TestClient(picker_app).get("/api/picker/status").json()


def test_api_key_note_in_settings():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "frontend" / "js" / "app.js").read_text(encoding="utf-8")
    assert 'id="picker-key-note" hidden>' in html
    assert '$("#picker-key-note").hidden = !st.api_key_set;' in js


@pytest.mark.skipif(not (ROOT / "docker-compose.yml").exists(), reason="repo files not present")
def test_host_port_conflict_is_explained():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"9000:8091"' in compose and "RECIPE_APP_PICKER_PUBLIC_PORT" in compose
    assert "RECIPE_APP_PICKER_PUBLIC_PORT" in (ROOT / "docker-compose.build.yml").read_text(encoding="utf-8")
    # No version tags are published (0077): the compose file says to pin a digest.
    assert ":1.0.0" not in compose and "@sha256:" in compose
    for readme in ("README.md", "README.fr.md"):
        text = (ROOT / readme).read_text(encoding="utf-8")
        assert 'RECIPE_APP_PICKER_PUBLIC_PORT: "9000"' in text, readme
        assert "500" in text[text.index("8091"):text.index("8091") + 1500], readme
