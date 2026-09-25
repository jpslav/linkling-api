"""LL-035 -- every job runs on a versioned Ubuntu image (`ubuntu-NN.NN`), not on an alias.

The comment at the top of .github/workflows/ci.yml says why the jobs are pinned (the `ubuntu-latest`
alias is about to move to Ubuntu 26.04) and why the pin is moved by hand. Nothing else keeps it there.
This reads every workflow and fails on a job whose `runs-on` is anything but a versioned Ubuntu label:
an alias (`ubuntu-latest`, `ubuntu-slim`), an expression that hides what it resolves to
(`${{ matrix.os }}`, `${{ vars.RUNNER }}`) and a block list all move or hide the image, so going back
to one of them, or adding a job that uses it, is red here and not silently a change of image on a
date GitHub chose. Moving to `ubuntu-26.04` on purpose is a change to the `runs-on` lines of the
workflow and none to this test (run against a copy of ci.yml with every label at `ubuntu-26.04`, it passes).
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _runs_on() -> list[tuple[str, str]]:
    """(workflow file, the value of each `runs-on:` outside a comment), in order. A block list, whose
    items are on the following lines, has the value ''."""
    found = []
    for workflow in sorted(WORKFLOWS.glob("*.y*ml")):
        for line in workflow.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = re.match(r"^\s*(?:- )?runs-on:\s*(.*?)\s*(?:#.*)?$", line)
            if match:
                found.append((workflow.name, match.group(1)))
    return found


def test_the_walk_finds_the_jobs_it_is_about_to_judge():
    # The population is fixed: ci.yml has jobs, each with a `runs-on`. A walk that found none would
    # judge nothing and pass.
    found = _runs_on()
    assert any(name == "ci.yml" for name, _ in found), f"no `runs-on:` found in {WORKFLOWS}: {found}"


def test_every_job_runs_on_a_versioned_ubuntu_image():
    unpinned = [(name, value) for name, value in _runs_on() if not re.fullmatch(r"ubuntu-\d{2}\.\d{2}", value)]
    assert not unpinned, (
        "these jobs do not name a versioned Ubuntu image (an alias such as ubuntu-latest or ubuntu-slim, an "
        "expression, or a list, moves on GitHub's schedule or hides what it resolves to; README, 'Keeping "
        f"the pins fresh'): {unpinned}"
    )
