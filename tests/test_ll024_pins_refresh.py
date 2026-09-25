"""LL-024 -- the locks and base-image digests are refreshed by Dependabot, and nothing that
needs refreshing is left out of it.

`.github/dependabot.yml` is what makes a PR arrive; nothing here runs Dependabot. What these tests
hold is what the config depends on and what would leave a pin outside it, or damage a lock, without
anyone noticing (docs/adr/0018-dependabot-refreshes-the-locks-and-digests.md):

- every Dockerfile in the repository is under a docker `directories` entry (the walk skips only the
  checkout's own metadata, other checkouts of it, virtualenvs and tool caches: `NOT_SOURCE` below);
- requirements.lock.txt has a requirements.lock.in beside it, which is what makes Dependabot
  re-resolve the whole set, and says what pyproject.toml says, because that list now lives in two
  places. requirements-build.lock.txt has NO `.in`: with one, Dependabot's pip-compile updater
  returned the file with its pin gone (where in the updater was not located; its header says more);
- each lock still pins what it exists for, and every pin still carries a hash, so a refresh PR
  that empties or unhashes a lock fails here, in the `test` job, and not only in the image build;
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
LOCK = ROOT / "requirements.lock.txt"
BUILD_LOCK = ROOT / "requirements-build.lock.txt"

DIGEST = re.compile(r"^FROM python:3\.12-slim@(sha256:[0-9a-f]{64})[ \t]*$", re.M)
PINNED_DOCKERFILES = [
    ROOT / "Dockerfile",
    ROOT / "scripts" / "no-third-party" / "Dockerfile",
    ROOT / "tests" / "fixtures" / "standin-site" / "Dockerfile",
]


def _blocks() -> dict[str, str]:
    """The config's `updates` entries by package ecosystem, with comment lines taken out whole."""
    text = re.sub(r"^[ \t]*#.*\n", "", CONFIG.read_text(), flags=re.M)
    parts = re.split(r"^  - package-ecosystem:[ \t]*", text, flags=re.M)[1:]
    return {part.split("\n", 1)[0].strip(): part for part in parts}


def _directories(block: str) -> set[str]:
    found = set()
    lines = block.splitlines()
    for number, line in enumerate(lines):
        if line == "    directories:":
            for item in lines[number + 1 :]:
                if item.startswith("      - "):
                    found.add(item[len("      - ") :].strip().strip("\"'"))
                elif item.strip():
                    break
    single = re.search(r"^    directory:[ \t]*(\S+)", block, re.M)
    if single:
        found.add(single.group(1).strip("\"'"))
    return found


# Not source: the repository's own metadata, other checkouts of it (linked worktrees live under
# .claude/), the virtualenvs, and what the smoke scripts and pytest leave behind. Any other
# dot-directory is walked: a Docker-based action's Dockerfile under .github/ is still a Dockerfile.
NOT_SOURCE = {".git", ".claude", ".smoke-data", ".pytest_cache", "__pycache__", "node_modules"}


def _dockerfile_directories() -> set[str]:
    found = set()
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in NOT_SOURCE and not d.startswith(".venv")]
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


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _pins(path: Path) -> dict[str, int]:
    """Each `name==version` pin in a hashed requirements file, with how many `--hash=` lines follow."""
    pins: dict[str, int] = {}
    current = None
    for line in path.read_text().splitlines():
        pin = re.match(r"^([A-Za-z0-9_.-]+)==\S+", line)
        if pin:
            current = pin.group(1).lower().replace("_", "-")
            pins[current] = 0
        elif current and re.match(r"^\s+--hash=sha256:[0-9a-f]{64}\b", line):
            pins[current] += 1
        elif line.strip() and not line.startswith((" ", "\t", "#")):
            current = None
    return pins


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


def test_the_docker_block_ignores_python_minor_and_major_moves():
    block = _blocks()["docker"]
    ignore = re.search(r"^    ignore:\n((?:      .*\n|[ \t]*\n)+)", block, re.M)
    assert ignore, "the docker block has no `ignore`"
    # The entry that names python, not the block as a whole: another package's entry that ignores
    # major and minor moves must not stand in for it.
    entries = [entry for entry in re.split(r"^      - ", ignore.group(1), flags=re.M) if entry.strip()]
    python = [entry for entry in entries if re.match(r"dependency-name:\s*python\s*$", entry.splitlines()[0])]
    assert len(python) == 1, f"the docker block's `ignore` has {len(python)} entries for python, not one"
    for kind in ("major", "minor"):
        assert f"version-update:semver-{kind}" in python[0], (
            f"python's semver-{kind} moves are not ignored, so Dependabot would propose another Python"
        )


# --- the locks, and the .in file that makes one of them re-resolve -------------------------------


def test_the_main_lock_has_its_in_file_and_the_build_lock_has_none_and_the_pip_block_covers_both():
    locks = sorted(ROOT.glob("requirements*.lock.txt"))
    # Both known locks, at least: finding none must not read as "each is as it should be".
    assert {p.name for p in locks} >= {LOCK.name, BUILD_LOCK.name}
    for lock in locks:
        in_file = lock.with_suffix(".in")
        if lock == BUILD_LOCK:
            assert not in_file.exists(), (
                f"{in_file.name} exists, so Dependabot would hand {lock.name} to its pip-compile updater, "
                "which returned it with its setuptools pin gone (docs/adr/0018)"
            )
        else:
            assert in_file.is_file(), (
                f"{lock.name} has no {in_file.name} beside it, so Dependabot would edit its pins one line "
                "at a time and never re-resolve them (docs/adr/0018)"
            )
    assert _directories(_blocks()["pip"]) >= {"/"}


def test_requirements_lock_in_says_what_pyproject_says():
    declared = tomllib.loads(PYPROJECT.read_text())["project"]["dependencies"]
    assert declared, "pyproject.toml declares no dependencies, so there is nothing to compare"
    assert sorted(_requirement_lines(ROOT / "requirements.lock.in")) == sorted(declared)


def test_each_lock_pins_what_it_is_for_and_every_pin_carries_a_hash():
    project = tomllib.loads(PYPROJECT.read_text())
    lock = _pins(LOCK)
    build = _pins(BUILD_LOCK)
    # What each must pin: the project's own dependencies (and whatever they pull in), and the build
    # backend. Empty would be the failure this exists for, so it is asserted against these names.
    for requirement in project["project"]["dependencies"]:
        assert _name(requirement) in lock, f"{LOCK.name} does not pin {_name(requirement)}"
    assert set(build) == {_name(r) for r in project["build-system"]["requires"]}, (
        f"{BUILD_LOCK.name} pins {sorted(build)}, not what [build-system] requires names"
    )
    assert len(lock) >= 2 and len(build) >= 1
    for path, pins in ((LOCK, lock), (BUILD_LOCK, build)):
        unhashed = sorted(name for name, hashes in pins.items() if hashes == 0)
        assert not unhashed, f"{path.name} pins {unhashed} with no hash, and the install is --require-hashes"


# --- the three digests move together ------------------------------------------------------------


def test_the_service_the_observer_and_the_stand_in_carry_one_digest():
    found = {path: DIGEST.findall(path.read_text()) for path in PINNED_DOCKERFILES}
    for path, digests in found.items():
        # Exactly one each: a FROM line that moved or changed shape reads as none, not as equal.
        assert len(digests) == 1, f"{path.relative_to(ROOT)} has no digest-pinned python:3.12-slim FROM"
    assert len({digests[0] for digests in found.values()}) == 1, "the three Dockerfiles carry different digests"
