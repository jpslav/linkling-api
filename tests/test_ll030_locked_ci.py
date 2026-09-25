"""LL-030 -- CI tests what ships: pytest runs against the hash-locked dependencies, and the GitHub
Actions are pinned by commit SHA.

`.github/workflows/ci.yml` and `.github/dependabot.yml` are what do this; nothing here runs a
workflow. What these tests hold is what would let either quietly stop being true
(docs/adr/0019-ci-tests-the-locks-and-pins-its-actions.md):

- every `uses:` in every workflow and local composite action is a 40-hex commit SHA with its tag
  in a trailing comment (a `docker://` image, a sha256 digest), so an action added by tag is red
  here and not a floating reference in CI;
- the `test` job installs the three locks in one `pip install --require-hashes`, the image lock
  first, and then the package with no dependencies and no build isolation, and no `pip install` in a
  workflow or local composite action fetches from an index without hashes except the one that is
  the red path of `no-forbidden-imports`. It reads the spellings `pip`, `pip3`, `pip3.12`,
  `python -m pip` and `uv pip`, with options before `install`, and fails on an install named in a
  `run:` it cannot parse; other ways to install (`pipx`, `pip download`, a piped script) it does not
  read;
- requirements-test.lock.in says what pyproject.toml says (the dependencies and the `test` extra),
  because that list now lives in two places more than it did;
- requirements-test.lock.txt pins what the `test` extra names, every pin with a hash, repeats
  every pin of requirements.lock.txt at the same version with the same hashes, and pins nothing the
  image's forbidden-imports guard refuses, so the packages pytest runs against are the ones the
  image ships;
- the config's `github-actions` block points at the workflows and is weekly on Mondays, like the
  other two.

Each comparison is between values read out of a file, and an empty read must not look like
agreement, so each one is asserted against a value it must have.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
CI = WORKFLOWS / "ci.yml"
CONFIG = ROOT / ".github" / "dependabot.yml"
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "requirements.lock.txt"
TEST_LOCK = ROOT / "requirements-test.lock.txt"
TEST_LOCK_IN = ROOT / "requirements-test.lock.in"
BUILD_LOCK = ROOT / "requirements-build.lock.txt"

FORBIDDEN_IMPORTS_CHECK = ROOT / "scripts" / "no-forbidden-imports-check.sh"

# The one `pip install` in any workflow that is not hash-checked on purpose: no-forbidden-imports
# puts `websockets` into the environment to show its check goes red. Anything else that installs
# from an index without hashes is a way back to testing something the image does not ship.
UNHASHED_INSTALLS = {"pip install --no-cache-dir websockets >/dev/null"}

# `pip`, `pip3`, `pip3.12`, `python -m pip` and `uv pip`, with options before `install`. `pipx`,
# `pip download` and a piped install script are not read.
PIP_INSTALL = re.compile(r"\bpip3?(?:\.\d+)?(?:\s+-\S+)*\s+install\b")
# The package itself, with no dependencies of its own and nothing fetched to build it.
PACKAGE_INSTALL = re.compile(r"pip install --no-deps --no-build-isolation (?:-e )?\.")

SHA_PIN = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
DOCKER_PIN = re.compile(r"^docker://\S+@sha256:[0-9a-f]{64}$")
VERSION_COMMENT = re.compile(r"^v\d+(\.\d+)*$")


def _workflow_files() -> list[Path]:
    """The workflows, and the local composite actions they could call: both hold `run:` and `uses:`."""
    return sorted(WORKFLOWS.glob("*.y*ml")) + sorted((ROOT / ".github" / "actions").glob("**/action.y*ml"))


def _uses() -> list[tuple[Path, str, str]]:
    """Each `uses:` of each workflow as (file, reference, trailing comment), local ones left out."""
    found = []
    for workflow in _workflow_files():
        for line in workflow.read_text().splitlines():
            match = re.match(r"^\s*-?\s*uses:\s*(\S+)[ \t]*(?:#[ \t]*(.*?))?[ \t]*$", line)
            if match and not match.group(1).startswith("./"):
                found.append((workflow, match.group(1), match.group(2) or ""))
    return found


def _jobs(text: str) -> dict[str, str]:
    """ci.yml's jobs by id, each with the lines up to the next job's id."""
    body = re.split(r"^jobs:[ \t]*\n", text, flags=re.M)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):[ \t]*\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def _commands(text: str) -> list[str]:
    """The statements of every `run:` in the text, in order: a one-liner, or each line of a `|` or
    `>` block, split at `&&`, `||` and `;`, with blank lines and comments left out."""
    lines = text.splitlines()
    statements: list[str] = []
    number = 0
    while number < len(lines):
        match = re.match(r"^(\s*)(-\s+)?run:[ \t]*(.*)$", lines[number])
        number += 1
        if not match:
            continue
        key_column = len(match.group(1)) + len(match.group(2) or "")
        rest = match.group(3).strip()
        raw = [rest] if rest and rest[0] not in "|>" else []
        if rest[:1] in ("|", ">"):
            while number < len(lines) and (
                not lines[number].strip() or len(lines[number]) - len(lines[number].lstrip()) > key_column
            ):
                raw.append(lines[number])
                number += 1
        for line in raw:
            for statement in re.split(r"\s*(?:&&|\|\||;)\s*", line.strip()):
                if statement and not statement.startswith("#"):
                    statements.append(statement)
    return statements


def _requirement_lines(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _locked(path: Path) -> dict[str, tuple[str, set[str]]]:
    """Each `name==version` pin of a hashed requirements file, with the hashes that follow it."""
    pins: dict[str, tuple[str, set[str]]] = {}
    current = None
    for line in path.read_text().splitlines():
        pin = re.match(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)", line)
        if pin:
            current = pin.group(1).lower().replace("_", "-")
            pins[current] = (pin.group(2), set())
        elif current and (digest := re.match(r"^\s+--hash=sha256:([0-9a-f]{64})\b", line)):
            pins[current][1].add(digest.group(1))
        elif line.strip() and not line.startswith((" ", "\t", "#")):
            current = None
    return pins


# --- the actions are pinned ----------------------------------------------------------------------


def test_every_action_is_pinned_by_commit_sha_with_its_tag_beside_it():
    uses = _uses()
    # ci.yml uses actions, so finding none means this read nothing.
    assert any(path == CI for path, _, _ in uses), "the walk found no `uses:` in ci.yml"
    for path, reference, comment in uses:
        where = f"{path.relative_to(ROOT)}: uses: {reference}"
        if reference.startswith("docker://"):
            assert DOCKER_PIN.match(reference), f"{where} is not pinned by a sha256 image digest"
            continue
        assert SHA_PIN.match(reference), f"{where} is not pinned by a 40-hex commit SHA"
        assert VERSION_COMMENT.match(comment), (
            f"{where} has no `# vX.Y.Z` comment naming the tag it was resolved from "
            f"(found {comment!r}), which is what Dependabot keeps in step with the SHA"
        )


# --- CI installs from the locks ------------------------------------------------------------------


def test_the_test_job_installs_the_locks_under_require_hashes_then_the_package_then_runs_pytest():
    jobs = _jobs(CI.read_text())
    assert "test" in jobs, "ci.yml has no `test` job"
    commands = _commands(jobs["test"])
    hashed = [i for i, c in enumerate(commands) if PIP_INSTALL.search(c) and "--require-hashes" in c]
    assert len(hashed) == 1, f"the test job has {len(hashed)} `pip install --require-hashes` steps, not one"
    install = commands[hashed[0]]
    for lock in (LOCK.name, TEST_LOCK.name, BUILD_LOCK.name):
        assert f"-r {lock}" in install, f"the test job's hashed install does not take {lock}"
    # The image lock goes first. Given the same version at different hashes in two files, pip
    # enforces the first file's (measured: with the test lock second, wrong hashes in it installed,
    # exit 0; first, exit 1), so the image's hashes are the ones that count only in this order.
    assert install.index(f"-r {LOCK.name}") < install.index(f"-r {TEST_LOCK.name}"), (
        f"the test job's hashed install does not take {LOCK.name} before {TEST_LOCK.name}"
    )
    package = [i for i, c in enumerate(commands) if PACKAGE_INSTALL.fullmatch(c)]
    assert len(package) == 1, f"the test job has {len(package)} package installs with --no-deps --no-build-isolation"
    run = [i for i, c in enumerate(commands) if c.startswith("pytest")]
    assert len(run) == 1, f"the test job has {len(run)} pytest steps, not one"
    assert hashed[0] < package[0] < run[0], "the test job does not install, then install the package, then run pytest"


def test_no_forbidden_imports_installs_what_the_image_ships_the_way_the_dockerfile_does():
    jobs = _jobs(CI.read_text())
    assert "no-forbidden-imports" in jobs, "ci.yml has no `no-forbidden-imports` job"
    commands = _commands(jobs["no-forbidden-imports"])
    assert (
        "pip install --require-hashes -r requirements.lock.txt -r requirements-build.lock.txt" in commands
    ), "no-forbidden-imports does not install the two image locks under --require-hashes"
    assert "pip install --no-deps --no-build-isolation ." in commands


def test_no_pip_install_in_any_workflow_reads_pyproject_ranges_or_skips_hashes():
    workflows = _workflow_files()
    assert CI in workflows, "the walk found no ci.yml"
    installs = []
    for workflow in workflows:
        text = workflow.read_text()
        assert ".[test]" not in text, f"{workflow.name} installs the `test` extra from pyproject.toml's ranges"
        # Backstop for a `run:` written in a form `_commands` does not read (a value on the next
        # line, a continued one-liner, a flow mapping): every install named outside a comment line
        # has to be one of the statements read above, or the file is not being read as it is written.
        named = sum(len(PIP_INSTALL.findall(line)) for line in text.splitlines() if not line.lstrip().startswith("#"))
        read = sum(len(PIP_INSTALL.findall(command)) for command in _commands(text))
        assert named == read, (
            f"{workflow.name} names `pip install` {named} times outside comment lines and only {read} of "
            "them are in a `run:` this reads, so one is written in a form this does not parse"
        )
        for command in _commands(text):
            if not PIP_INSTALL.search(command):
                continue
            installs.append(command)
            assert "--require-hashes" in command or PACKAGE_INSTALL.fullmatch(command) or command in UNHASHED_INSTALLS, (
                f"{workflow.name}: `{command}` installs from an index with no hashes, so CI would test "
                "versions the image does not ship (docs/adr/0019)"
            )
    # The workflow installs things, and finding none would make the loop above pass over nothing.
    assert len(installs) >= 4, f"found {len(installs)} `pip install` statements in the workflows, expected at least four"
    # The exception has to be one that is there: a red path that was deleted leaves an allowance for
    # the next unhashed install to hide behind.
    assert UNHASHED_INSTALLS <= set(installs), "the one allowed unhashed install is not in any workflow"


# --- the test lock says what pyproject says and repeats what the image ships ---------------------


def test_the_test_lock_in_says_what_pyproject_says():
    project = tomllib.loads(PYPROJECT.read_text())["project"]
    declared = project["dependencies"] + project["optional-dependencies"]["test"]
    assert len(declared) >= 4, "pyproject.toml declares fewer than the two dependencies and two test ones"
    assert sorted(_requirement_lines(TEST_LOCK_IN)) == sorted(declared)


def test_the_test_lock_pins_what_the_test_extra_names_and_every_pin_carries_a_hash():
    extra = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]["test"]
    locked = _locked(TEST_LOCK)
    assert len(extra) >= 2 and len(locked) >= 10
    for requirement in extra:
        assert _name(requirement) in locked, f"{TEST_LOCK.name} does not pin {_name(requirement)}"
    unhashed = sorted(name for name, (_, hashes) in locked.items() if not hashes)
    assert not unhashed, f"{TEST_LOCK.name} pins {unhashed} with no hash, and the install is --require-hashes"


def test_the_test_lock_repeats_every_pin_of_the_image_lock_at_the_same_version_and_hashes():
    image = _locked(LOCK)
    test = _locked(TEST_LOCK)
    assert len(image) >= 10, f"{LOCK.name} pins {len(image)} packages, so there is nothing to compare"
    for name, (version, hashes) in sorted(image.items()):
        assert name in test, f"{TEST_LOCK.name} does not pin {name}, which the image lock pins"
        assert test[name][0] == version, (
            f"{name} is {version} in {LOCK.name} and {test[name][0]} in {TEST_LOCK.name}, "
            "so pytest would run against a version the image does not ship, and pip refuses the pair"
        )
        assert test[name][1] == hashes, f"{name}'s hashes differ between {LOCK.name} and {TEST_LOCK.name}"


def test_the_test_lock_pins_nothing_the_image_is_forbidden_to_import():
    guard = FORBIDDEN_IMPORTS_CHECK.read_text()
    listed = re.search(r"for m in \(([^)]*)\) if importlib", guard)
    assert listed, f"{FORBIDDEN_IMPORTS_CHECK.name} no longer lists its modules in the form this reads"
    # Module names, compared as package names as _locked writes them: right for websockets and
    # wsproto, where the two are the same word.
    forbidden = [name.lower().replace("_", "-") for name in re.findall(r'"([A-Za-z0-9_.-]+)"', listed.group(1))]
    assert len(forbidden) >= 2, f"read {forbidden} out of {FORBIDDEN_IMPORTS_CHECK.name}"
    # pytest runs uvicorn in-process (tests/conftest.py): with one of these importable it would log a
    # WebSocket client's address, the leak the guard exists to keep out of the image.
    present = sorted(set(forbidden) & set(_locked(TEST_LOCK)))
    assert not present, f"{TEST_LOCK.name} pins {present}, which the image's guard refuses"


# --- the config points at the workflows ----------------------------------------------------------


def _blocks() -> dict[str, str]:
    text = re.sub(r"^[ \t]*#.*\n", "", CONFIG.read_text(), flags=re.M)
    parts = re.split(r"^  - package-ecosystem:[ \t]*", text, flags=re.M)[1:]
    return {part.split("\n", 1)[0].strip(): part for part in parts}


def test_the_github_actions_block_reads_the_workflows_weekly_on_mondays():
    assert list(WORKFLOWS.glob("*.y*ml")), "there are no workflows, so there is nothing for the block to read"
    blocks = _blocks()
    assert "github-actions" in blocks, "the config has no `github-actions` block"
    block = blocks["github-actions"]
    assert re.search(r"^    directory:[ \t]*[\"']?/[\"']?[ \t]*$", block, re.M), "the block does not read `/`"
    assert re.search(r"^      interval:[ \t]*weekly[ \t]*$", block, re.M)
    assert re.search(r"^      day:[ \t]*monday[ \t]*$", block, re.M)
