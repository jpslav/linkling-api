"""LL-034 -- CI's `no-third-party-site` job also crawls the real, public linkling-web.

The job checks out jpslav/linkling-web at its trunk with no credential, runs the whole
no-third-party check with LINKLING_WEB_DIR pointed at that checkout, and runs the check's blind arm
against an empty and an absent directory. Running it needs Docker and the network, so the job
itself is CI's. What does not is pinned here, in the fast job:

- the second checkout names linkling-web, its trunk and a path inside the workspace, carries no
  token, key or secret, and the workflows hold no reference to a secret or to the deploy key LL-034
  did without;
- the step that runs the check points LINKLING_WEB_DIR at exactly the directory that checkout
  writes to, is not `--api-only`, and comes after the checkout and after the stand-in's steps;
- the step that runs the check's blind arm is judged by what it does, not by how it reads: it is
  run here, in bash as GitHub runs it, against a fake check, and it passes only when the check
  ends `blind` (exit 2) naming the directory it was given, for the empty directory and for the
  absent one. A check that says `pass`, ends `fail`, is blind for another reason, or says `blind`
  in the wrong status turns it red, so neither half of what it reads is doing the other's work.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"


def _site_job() -> str:
    found = re.search(
        r"^  no-third-party-site:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", CI.read_text(), re.M | re.S
    )
    assert found, "ci.yml has no job `no-third-party-site`"
    return found.group(1)


def _steps() -> list[str]:
    """The site job's steps, each as its own text, in order."""
    job = _site_job()
    assert "\n    steps:\n" in job, "the site job has no `steps:`"
    parts = re.split(r"^      - ", job.split("\n    steps:\n", 1)[1], flags=re.M)
    steps = ["      - " + part for part in parts[1:]]
    assert steps, "the site job has no steps"
    return steps


def _real_site_checkout() -> tuple[int, str]:
    """The step that checks out another repository, and its index among the steps."""
    found = [(i, s) for i, s in enumerate(_steps()) if re.search(r"^\s+repository: ", s, re.M)]
    assert len(found) == 1, f"the site job has {len(found)} steps that check out another repository, not one"
    return found[0]


# --- the second checkout -------------------------------------------------------------------------


def test_the_site_job_checks_out_the_real_site_at_its_trunk_into_the_workspace():
    _, step = _real_site_checkout()
    assert re.search(r"^\s+(?:- )?uses: actions/checkout@", step, re.M), "the second checkout is not actions/checkout"
    assert re.search(r"^\s+repository: jpslav/linkling-web$", step, re.M)
    assert re.search(r"^\s+ref: main$", step, re.M), "the checkout does not name linkling-web's trunk"
    path = re.search(r"^\s+path: (\S+)$", step, re.M)
    assert path, "the checkout has no `path:`"
    # A checkout refuses a path outside the workspace, so the sibling `../linkling-web` that
    # LINKLING_WEB_DIR defaults to is not available to it.
    assert not path.group(1).startswith(("/", "..")), f"path {path.group(1)!r} is not inside the workspace"


def test_the_real_site_checkout_comes_after_the_one_of_this_repository():
    steps = _steps()
    index, _ = _real_site_checkout()
    plain = [i for i, s in enumerate(steps) if re.search(r"uses: actions/checkout@", s) and "repository:" not in s]
    assert plain, "the site job does not check out this repository"
    assert min(plain) < index, "the real site is checked out before this repository"


def test_the_real_site_is_read_with_no_credential():
    _, step = _real_site_checkout()
    assert not re.search(r"^\s+(?:token|ssh-key|ssh-known-hosts|ssh-user):", step, re.M), (
        "the real-site checkout passes a credential; linkling-web is public and needs none"
    )
    # The action's own token would otherwise stay configured in linkling-web/.git, which is the
    # web image's build context, and nothing after the checkout fetches or pushes.
    assert re.search(r"^\s+persist-credentials: false$", step, re.M), "the real-site checkout persists credentials"
    workflows = sorted(WORKFLOWS.glob("*.y*ml"))
    assert CI in workflows, "the walk found no ci.yml"
    for workflow in workflows:
        text = workflow.read_text()
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"\bsecrets\b", code), f"{workflow.name} refers to secrets"
        assert "DEPLOY_KEY" not in text, f"{workflow.name} names a deploy key"


def test_nothing_in_the_site_job_can_turn_a_step_into_a_no_op():
    # An `if:` that is false, `continue-on-error`, or a `working-directory` that moves the check
    # would leave the job green with the real site unchecked. None of them is in the job today, so
    # a later one has to be argued for here.
    found = re.findall(r"^\s+(?:if|continue-on-error|working-directory):.*$", _site_job(), re.M)
    assert not found, f"the site job carries {found}, which can make a step, or the job, a green no-op"


# --- the step that runs the check against it -----------------------------------------------------


def test_the_check_runs_against_the_directory_the_checkout_writes_to_after_the_stand_in_steps():
    steps = _steps()
    index, checkout = _real_site_checkout()
    path = re.search(r"^\s+path: (\S+)$", checkout, re.M).group(1)
    runs = [
        i for i, s in enumerate(steps) if re.search(rf"^\s+run: LINKLING_WEB_DIR={re.escape(path)} scripts/no-third-party-check\.sh$", s, re.M)
    ]
    assert len(runs) == 1, f"{len(runs)} steps run the check against {path!r}, the checkout's path, not one"
    assert runs[0] > index, "the check runs against the real site before it is checked out"
    standin = [i for i, s in enumerate(steps) if "no-third-party-standin.sh" in s or "tests/fixtures/standin-site" in s]
    assert standin, "the walk found no stand-in steps"
    assert runs[0] > max(standin), "the real-site check comes before a stand-in step, so a red there is not the site's"
    assert "--api-only" not in steps[runs[0]], "the real-site check is --api-only, which skips the site"
    blind = [i for i, s in enumerate(steps) if "no-third-party-check.sh" in s and "\n        run: |\n" in s]
    assert len(blind) == 1 and blind[0] > runs[0], "the blind arm does not run after the real-site check"


# --- the step that runs the check's blind arm ----------------------------------------------------

# What stands in for scripts/no-third-party-check.sh: it records the directory it was handed and
# whether it existed and was empty, then ends as FAKE_MODE says.
FAKE_CHECK = """#!/usr/bin/env bash
{
    echo "dir: $LINKLING_WEB_DIR"
    if [ -d "$LINKLING_WEB_DIR" ]; then
        echo "listing: $(ls -A "$LINKLING_WEB_DIR" | wc -l | tr -d ' ') entries"
    else
        echo "listing: absent"
    fi
} >>"$FAKE_RECORD"
case "$FAKE_MODE" in
    blind) echo "blind: no linkling-web checkout at $LINKLING_WEB_DIR, so the site cannot be checked (--api-only checks the service alone)" >&2; exit 2 ;;
    blind-elsewhere) echo "blind: no linkling-web checkout at /somewhere/else, so the site cannot be checked" >&2; exit 2 ;;
    wrong-for-the-empty-dir)
        if [ -d "$LINKLING_WEB_DIR" ]; then echo "blind: the stack never came up healthy" >&2; exit 2; fi
        echo "blind: no linkling-web checkout at $LINKLING_WEB_DIR, so the site cannot be checked" >&2; exit 2 ;;
    wrong-for-the-absent-dir)
        if [ ! -d "$LINKLING_WEB_DIR" ]; then echo "blind: the stack never came up healthy" >&2; exit 2; fi
        echo "blind: no linkling-web checkout at $LINKLING_WEB_DIR, so the site cannot be checked" >&2; exit 2 ;;
    passes-the-empty-dir)
        if [ -d "$LINKLING_WEB_DIR" ]; then echo "pass"; exit 0; fi
        echo "blind: no linkling-web checkout at $LINKLING_WEB_DIR, so the site cannot be checked" >&2; exit 2 ;;
    blind-words-exit-0) echo "blind: no linkling-web checkout at $LINKLING_WEB_DIR, so the site cannot be checked (--api-only checks the service alone)" >&2; exit 0 ;;
    blind-words-exit-1) echo "blind: no linkling-web checkout at $LINKLING_WEB_DIR, so the site cannot be checked (--api-only checks the service alone)" >&2; exit 1 ;;
    blind-other-reason) echo "blind: the stack never came up healthy, so nothing was exercised" >&2; exit 2 ;;
    fail) echo "fail: web serves / with a third-party reference" >&2; exit 1 ;;
    pass) echo pass; exit 0 ;;
esac
exit 64
"""


def _blind_step_script() -> str:
    found = [s for s in _steps() if "no-third-party-check.sh" in s and "\n        run: |\n" in s]
    assert len(found) == 1, f"{len(found)} multi-line steps run the check, expected the one for the blind arm"
    body = found[0].split("\n        run: |\n", 1)[1]
    return textwrap.dedent(body)


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "scripts").mkdir()
    check = tmp_path / "scripts" / "no-third-party-check.sh"
    check.write_text(FAKE_CHECK)
    check.chmod(check.stat().st_mode | stat.S_IXUSR)
    return tmp_path


def _run_step(tree: Path, mode: str):
    record = tree / "record.txt"
    env = {**os.environ, "FAKE_MODE": mode, "FAKE_RECORD": str(record), "TMPDIR": str(tree)}
    # GitHub runs a step with no `shell:` as `bash -e {0}`.
    done = subprocess.run(
        ["bash", "-e", "-c", _blind_step_script()], cwd=tree, env=env, capture_output=True, text=True, timeout=60
    )
    return done, (record.read_text() if record.exists() else "")


def test_the_blind_step_passes_when_the_check_is_blind_about_each_directory_it_is_given(tree):
    done, record = _run_step(tree, "blind")
    assert done.returncode == 0, done.stdout + done.stderr
    # It handed the check two directories: one that exists with nothing in it, and one that does not.
    dirs = re.findall(r"^dir: (.*)$", record, re.M)
    listings = re.findall(r"^listing: (.*)$", record, re.M)
    assert len(dirs) == 2 and dirs[0] != dirs[1], record
    assert listings == ["0 entries", "absent"], record
    for line in re.findall(r"^blind: .*$", done.stdout, re.M):
        assert line.startswith("blind: no linkling-web checkout at "), line


@pytest.mark.parametrize(
    "mode",
    [
        "pass",
        "fail",
        "blind-elsewhere",
        "blind-other-reason",
        "blind-words-exit-0",
        "blind-words-exit-1",
        "wrong-for-the-empty-dir",
        "wrong-for-the-absent-dir",
        "passes-the-empty-dir",
    ],
)
def test_the_blind_step_is_red_when_the_check_is_not_blind_about_the_directory_it_was_given(tree, mode):
    done, record = _run_step(tree, mode)
    assert done.returncode != 0, f"the step passed a check that ended as '{mode}':\n" + done.stdout + done.stderr
    assert "dir: " in record, "the step never ran the check, so a red here says nothing"
