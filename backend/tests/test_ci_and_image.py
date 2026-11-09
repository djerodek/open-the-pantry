"""CI and the Docker image: the workflow, the Dockerfile, the entrypoint and the build context.

Read as text: these can't be run in a test."""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _ci():
    return (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


BACKEND = os.path.join(os.path.dirname(__file__), "..")


def _backup_block() -> str:
    with open(os.path.join(BACKEND, "docker-entrypoint.sh"), encoding="utf-8") as f:
        script = f.read()
    start = script.index('if [ -n "$RECIPE_APP_BACKUP_DIR" ]')
    end = script.index("\nfi\n", start) + len("\nfi\n")
    return script[start:end]


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


def test_secrets_stay_out_of_the_docker_build():
    ignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert {".env", "*.env", "letsencrypt/", "data/"} <= set(ignore)


def test_ci_token_reads_only_and_the_audit_is_pinned():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert re.search(r"^permissions:\n  contents: read$", ci, re.M)
    assert re.search(r"pip install pip-audit==\d+\.\d+\.\d+", ci)


def test_the_entrypoint_fixes_root_owned_files_inside(tmp_path):
    script = (ROOT / "backend/docker-entrypoint.sh").read_text(encoding="utf-8")
    m = re.search(r'\$\(find /app/data (.+?) -print -quit 2>/dev/null\)', script)
    assert m, "the ownership check looks inside the folder"
    (tmp_path / "uploads").mkdir()
    (tmp_path / "recipes.db").write_bytes(b"x")
    os.chown(tmp_path / "recipes.db", 4321, 4321)       # as an unzip run with sudo leaves it, for another uid
    cmd = f'PUID={os.getuid()} PGID={os.getgid()}; find {tmp_path} {m.group(1)} -print -quit'
    assert subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout.strip().endswith("recipes.db")
    os.chown(tmp_path / "recipes.db", os.getuid(), os.getgid())
    assert subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout.strip() == ""


def test_ci_starts_the_image():
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    job = ci[ci.index("\n  docker-build:"):ci.index("\n  publish:")]
    assert "load: true" in job and "docker run -d --name otp-ci" in job and "/healthz" in job
    assert "p005-en-pdf-text-layer/input.pdf" in job and (ROOT / "backend/tests/corpus/p005-en-pdf-text-layer/input.pdf").is_file()


def test_the_image_is_published_only_after_ci():
    """Read as text (no YAML library in the test requirements)."""
    workflows = {p.name: p.read_text(encoding="utf-8") for p in (ROOT / ".github/workflows").glob("*.yml")}
    assert [name for name, text in workflows.items() if "push: true" in text] == ["ci.yml"]
    ci = workflows["ci.yml"]
    publish = ci[ci.index("\n  publish:"):]
    assert "needs: [test, browser, docker-build]" in publish
    assert "if: github.event_name != 'pull_request' && github.ref == 'refs/heads/main'" in publish
    assert publish.index("push: true") > publish.index("needs:")
    # Every action pinned to a commit.
    for name, text in workflows.items():
        for ref in re.findall(r"uses:\s*\S+@(\S+)", text):
            assert re.fullmatch(r"[0-9a-f]{40}", ref), (name, ref)


def test_the_image_installs_the_newest_trusted_roots():
    """Follow-up (0127): the roots the inbox login checks against come from
    Debian's ca-certificates. Named in the install line, every build takes
    the newest (the eMudhra roots removed in October 2026), not the copy the
    base image happened to be built with."""
    dockerfile = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")
    install = dockerfile[dockerfile.index("apt-get install"):dockerfile.index("rm -rf /var/lib/apt/lists/*")]
    assert re.search(r"^\s*ca-certificates \\$", install, re.M)
    assert "apt-get update" in dockerfile[:dockerfile.index("apt-get install")]


@pytest.mark.skipif(not (ROOT / "docker-compose.yml").exists(), reason="repo files not present")
def test_host_port_conflict_is_explained():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"9000:8091"' in compose and "RECIPE_APP_PICKER_PUBLIC_PORT" in compose
    assert "RECIPE_APP_PICKER_PUBLIC_PORT" in (ROOT / "docker-compose.build.yml").read_text(encoding="utf-8")
    # No version tags are published (0077): the compose file says to pin a digest.
    assert ":1.0.0" not in compose and "@sha256:" in compose


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("sh"),
                    reason="needs root to change owners")
def test_entrypoint_leaves_other_files_in_the_backup_folder_alone(tmp_path):
    """It ran chown -R over the whole folder, which can be a share."""
    folder = tmp_path / "share"
    (folder / "photos").mkdir(parents=True)
    other = folder / "photos" / "holiday.jpg"
    other.write_text("x")
    notes = folder / "notes.txt"
    notes.write_text("x")
    ours = folder / "open-the-pantry-backup-20261023-130000.zip"
    ours.write_text("x")
    for p in (folder, folder / "photos", other, notes, ours):
        os.chown(p, 0, 0)
    block = _backup_block().replace("appuser:appuser", "4321:4321")
    env = {"PATH": os.environ["PATH"], "PUID": "4321", "PGID": "4321", "RECIPE_APP_BACKUP_DIR": str(folder)}
    r = subprocess.run(["sh", "-e", "-c", block + "\necho reached\n"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "reached" in r.stdout, r.stderr
    assert os.stat(folder).st_uid == 4321 and os.stat(ours).st_uid == 4321
    assert os.stat(other).st_uid == 0 and os.stat(notes).st_uid == 0 and os.stat(folder / "photos").st_uid == 0


@pytest.mark.skipif(not shutil.which("sh"), reason="needs sh")
def test_entrypoint_starts_when_the_owner_cannot_be_changed(tmp_path):
    """With set -e, a share refusing chown stopped the whole app."""
    folder = tmp_path / "share"
    folder.mkdir()
    block = _backup_block().replace("appuser:appuser", "no-such-user-0104:no-such-user-0104")
    env = {"PATH": os.environ["PATH"], "PUID": "4321", "PGID": "4321", "RECIPE_APP_BACKUP_DIR": str(folder)}
    r = subprocess.run(["sh", "-e", "-c", block + "\necho reached\n"], env=env, capture_output=True, text=True)
    assert r.returncode == 0 and "reached" in r.stdout
    assert "couldn't change the owner" in r.stderr


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0 or not shutil.which("sh"),
                    reason="needs root to change owners")
def test_entrypoint_takes_only_the_apps_backup_names(tmp_path):
    folder = tmp_path / "share"
    folder.mkdir()
    ours = ["open-the-pantry-backup-20261023-130000.zip",
            "open-the-pantry-backup-20261023-130000-2.zip",
            "open-the-pantry-backup-20261023-130000.zip.partial"]
    alike = ["open-the-pantry-backup-notes.zip.bak",
             "open-the-pantry-backup-archive.zip.asc",
             "open-the-pantry-backup-20261023-130000.zip.bak",
             "open-the-pantry-backup-old.zip"]
    for n in ours + alike:
        (folder / n).write_text("x")
        os.chown(folder / n, 0, 0)
    with open(os.path.join(BACKEND, "docker-entrypoint.sh"), encoding="utf-8") as f:
        script = f.read()
    start = script.index('if [ -n "$RECIPE_APP_BACKUP_DIR" ]')
    block = script[start:script.index("\nfi\n", start) + 4].replace("appuser:appuser", "4321:4321")
    env = {"PATH": os.environ["PATH"], "PUID": "4321", "PGID": "4321", "RECIPE_APP_BACKUP_DIR": str(folder)}
    r = subprocess.run(["sh", "-e", "-c", block], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert {n: os.stat(folder / n).st_uid for n in ours} == {n: 4321 for n in ours}
    assert {n: os.stat(folder / n).st_uid for n in alike} == {n: 0 for n in alike}
