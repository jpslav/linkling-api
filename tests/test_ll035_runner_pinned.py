"""LL-035 -- every job runs on a named runner image, not on the `ubuntu-latest` alias.

The alias moves to Ubuntu 26 from 2026-10-19 (actions/runner-images#14748), and the Docker jobs run on
whatever Docker and kernel the image ships. Dependabot does not move a `runs-on` label, so a pin is
moved by hand and nothing else keeps it there. This reads every workflow and fails on a job whose
`runs-on` names an alias, so going back to `ubuntu-latest` (or adding a job that uses it) is red here,
not silently a change of image on a date GitHub chose.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"


def _runs_on() -> list[tuple[str, str]]:
    """(workflow file, the value of each `runs-on:` outside a comment), in order."""
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


def test_no_job_runs_on_a_moving_alias():
    aliases = [(name, value) for name, value in _runs_on() if "latest" in value.lower()]
    assert not aliases, (
        "these jobs run on an alias that moves on GitHub's schedule, not a named image "
        f"(README, 'Keeping the pins fresh'): {aliases}"
    )
