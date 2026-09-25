"""LL-024 -- the locks and base-image digests are refreshed by Dependabot, and nothing that
needs refreshing is left out of it.

`.github/dependabot.yml` is what makes a PR arrive; nothing here runs Dependabot. What these tests
hold is what the config depends on and what would leave a pin outside it without anyone noticing
(docs/adr/0018-dependabot-refreshes-the-locks-and-digests.md):

- every Dockerfile in the tree is under a docker `directories` entry, and every lock file has a
  `.in` file of the same basename beside it (the pip-compile updater is chosen by that file);
- each `.in` says what pyproject.toml says, because those lists now live in two places;
- the docker block ignores python's minor and major moves, so it refreshes the digest of the tag
  that ships and never proposes another Python;
- the three Dockerfiles that pin python:3.12-slim carry one digest, the one Dependabot's group
  moves them to together.

Each comparison is between values read out of a file, and an empty read must not look like
agreement, so each one is asserted against a value it must have.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".github" / "dependabot.yml"
PYPROJECT = ROOT / "pyproject.toml"

DIGEST = re.compile(r"^FROM python:3\.12-slim@(sha256:[0-9a-f]{64})[ \t]*$", re.M)
PINNED_DOCKERFILES = [
    ROOT / "Dockerfile",
    ROOT / "scripts" / "no-third-party" / "Dockerfile",
    ROOT / "tests" / "fixtures" / "standin-site" / "Dockerfile",
]


def _blocks() -> dict[str, str]:
    """The config's `updates` entries by package ecosystem, with comments taken out."""
    text = re.sub(r"^[ \t]*#.*$", "", CONFIG.read_text(), flags=re.M)
    parts = re.split(r"^  - package-ecosystem:[ \t]*", text, flags=re.M)[1:]
    return {part.split("\n", 1)[0].strip(): part for part in parts}


def _directories(block: str) -> set[str]:
    listed = re.search(r"^    directories:\n((?:      - .*\n)+)", block, re.M)
    single = re.search(r"^    directory:[ \t]*(\S+)", block, re.M)
    found = {line.split("- ", 1)[1].strip().strip("\"'") for line in listed.group(1).splitlines()} if listed else set()
    if single:
        found.add(single.group(1).strip("\"'"))
    return found


def _dockerfile_directories() -> set[str]:
    found = set()
    for base, dirs, files in os.walk(ROOT):
        # Linked worktrees, caches and the smoke scripts' data directories are all dot-directories.
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for name in files:
            if name == "Dockerfile" or name.startswith("Dockerfile."):
                relative = Path(base).relative_to(ROOT).as_posix()
                found.add("/" if relative == "." else "/" + relative)
    return found


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


# --- the config covers what exists --------------------------------------------------------------


def test_the_config_has_the_two_blocks_it_is_read_for():
    assert re.search(r"^version: 2$", CONFIG.read_text(), re.M)
    assert set(_blocks()) == {"pip", "docker"}


def test_every_dockerfile_is_under_a_docker_directories_entry():
    found = _dockerfile_directories()
    # The service's own Dockerfile is always there: finding none means this read nothing.
    assert "/" in found, "the walk found no Dockerfile at the root, so it read nothing"
    covered = _directories(_blocks()["docker"])
    assert covered, ".github/dependabot.yml's docker block names no directory"
    assert found <= covered, (
        f"Dockerfiles in {sorted(found - covered)} are under no `directories` entry of the docker "
        "block in .github/dependabot.yml, so their base image would go stale without a PR"
    )


def test_every_lock_file_has_an_in_file_beside_it_and_the_pip_block_covers_its_directory():
    locks = sorted(ROOT.glob("requirements*.lock.txt"))
    # Both known locks, at least: finding none must not read as "every lock has its .in".
    assert {p.name for p in locks} >= {"requirements.lock.txt", "requirements-build.lock.txt"}
    for lock in locks:
        assert lock.with_suffix(".in").is_file(), (
            f"{lock.name} has no {lock.with_suffix('.in').name} beside it, so Dependabot would edit its "
            "pins one line at a time and never re-resolve them (docs/adr/0018)"
        )
    assert _directories(_blocks()["pip"]) >= {"/"}


def test_the_docker_block_ignores_python_minor_and_major_moves():
    block = _blocks()["docker"]
    ignore = re.search(r"^    ignore:\n((?:      .*\n)+)", block, re.M)
    assert ignore, "the docker block has no `ignore`"
    assert "dependency-name: python" in ignore.group(1)
    for kind in ("major", "minor"):
        assert f"version-update:semver-{kind}" in ignore.group(1), (
            f"python's semver-{kind} moves are not ignored, so Dependabot would propose another Python"
        )


# --- the .in files say what pyproject.toml says --------------------------------------------------


def test_requirements_lock_in_says_what_pyproject_says():
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    assert declared, "pyproject.toml declares no dependencies, so there is nothing to compare"
    assert sorted(_requirement_lines(ROOT / "requirements.lock.in")) == sorted(declared)


def test_requirements_build_lock_in_says_what_the_build_system_requires():
    required = tomllib.loads(PYPROJECT.read_text())["build-system"]["requires"]
    assert required, "pyproject.toml's [build-system] requires nothing, so there is nothing to compare"
    assert sorted(_requirement_lines(ROOT / "requirements-build.lock.in")) == sorted(required)


# --- the three digests move together ------------------------------------------------------------


def test_the_service_the_observer_and_the_stand_in_carry_one_digest():
    found = {path: DIGEST.findall(path.read_text()) for path in PINNED_DOCKERFILES}
    for path, digests in found.items():
        # Exactly one each: a FROM line that moved or changed shape reads as none, not as equal.
        assert len(digests) == 1, f"{path.relative_to(ROOT)} has no digest-pinned python:3.12-slim FROM"
    assert len({digests[0] for digests in found.values()}) == 1, "the three Dockerfiles carry different digests"
