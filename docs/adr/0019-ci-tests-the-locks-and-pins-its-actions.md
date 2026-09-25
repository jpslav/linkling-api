# ADR-0019 — CI runs pytest against the hash-locked environment, and pins its actions by commit SHA

- Status: Proposed
- Approver: (pending)
- Date: 2026-09-25

## Context

ADR-0017 ii locked the image's dependencies and ADR-0018 keeps the locks fresh with Dependabot.
Both left `ci.yml` as it was: its `test` and `no-forbidden-imports` jobs ran
`pip install -e ".[test]"`, which resolves `pyproject.toml`'s ranges on the day, so a Dependabot
lock PR was exercised by the image build, the smoke script and the no-third-party check, and by no
pytest run. ADR-0018's Consequences say so and leave it recorded, not closed (LL-030 closes it).
The same file pinned `actions/checkout@v4` and `actions/setup-python@v5` by tag, which is neither a
lock nor a digest: the code a workflow runs moves when the tag does.

Measured on 2026-09-25, before choosing:

- `requirements.lock.txt` holds no test package: `pytest` and `httpx` are not in it.
- Compiling a lock from `fastapi`, `uvicorn`, `pytest` and `httpx` with no constraint gave
  `uvicorn` 0.54.0 where `requirements.lock.txt` has 0.53.0, so an independent second lock is not
  the same environment as the image's.
- Given both locks in one `pip install --require-hashes`, pip installs them when they agree
  (exit 0). It refuses two versions of one package: the test lock's contents re-compiled with
  `--exclude-newer 2026-06-01` (`anyio` 4.13.0, `starlette` 1.2.1, `uvicorn` 0.48.0) beside the
  current image lock gave `ResolutionImpossible`, exit 1. Given one version at two sets of hashes it
  enforces only the first file's: with `fastapi`'s hashes replaced in the test lock, `pip install
  --dry-run --ignore-installed` exited 0 with the test lock second and exit 1
  (`THESE PACKAGES DO NOT MATCH THE HASHES`) with it first.
- In a Python 3.12 virtualenv on macOS built the way the `test` job now builds its own, `pytest -q`
  gave 432 passed and one warning, on the suite as it stood on `main`. The warning is starlette
  1.7.0's: using `httpx` with `starlette.testclient` is deprecated in favour of `httpx2`.

## Decision

**Proposed.**

### i. A test lock that repeats the image lock

| Option | Costs, or what was measured | Recurring |
|---|---|---|
| A. Put `pytest` and `httpx` in `requirements.lock.txt` | They ship in the image | R1, and a larger image |
| B. Keep `pip install -e ".[test]"` (status quo) | pytest never sees the locked versions | R1 |
| **C. `requirements-test.lock.txt`, compiled from its own `requirements-test.lock.in` (the dependencies and the `test` extra) with `-c requirements.lock.txt`, installed in one `pip install --require-hashes` with the image lock and the build lock** | A third lock. It repeats every image pin, and pip refuses the pair if they name two versions (measured, above); the hashes are held by a test | R4: re-lock it after the image lock |
| D. A lock of the test-only packages, without the image's | Its `.in` would carry `-c requirements.lock.txt`, and how Dependabot's pip-compile updater treats a constraint that it re-resolves in the same run was not measured. C's `.in` is the plain shape ADR-0018 measured for the image lock | R4 |
| E. `uv sync --frozen` or `uv pip install` in CI | A `uv.lock` and a toolchain CI does not otherwise need; ADR-0017 ii rejected project mode | R4 |

**C.** The `test` job installs the three locks, then the package with `--no-deps
--no-build-isolation`, as the Dockerfile does, so none of what those installs fetch is unhashed.
The image lock goes first, for the reason measured above. `pytest` runs against the image's
versions. `no-forbidden-imports` installs only the image lock and the build lock, then the package,
because it looks at what ships and needs no test package. Its deliberate `pip install websockets`,
the red path of its own check, is the one unhashed install left in `ci.yml`.

### ii. The actions are pinned by commit SHA

Each `uses:` is a 40-hex commit with its tag in a trailing comment. `actions/checkout` is pinned at
the commit `v4` pointed at on 2026-09-25 (v4.4.0) and `actions/setup-python` at the one `v5` pointed
at (v5.6.0): the majors CI already ran on, resolved with `git ls-remote --tags` against each action's
repository. Both actions have a newer major tag today (checkout v7.0.1, setup-python v7.0.0), and
Dependabot is expected to propose them: a move of a major is a PR to read, and none is taken here.
`.github/dependabot.yml` gains a `github-actions` block, weekly on Mondays with one group, the shape
of the other two. Dependabot's own run was not made. Reading dependabot-core
(`github_actions/lib/dependabot/github_actions/file_updater/workflow_updater/version_commenter.rb`,
`updated_comment`), a trailing comment that ends in the version of the old SHA is rewritten to the
version of the new one; the comment is not a check that the SHA is that tag.

### iii. Tests hold what would quietly undo it

`tests/test_ll030_locked_ci.py` fails on a `uses:` in a workflow or local composite action that is not
a SHA with a `# vX.Y.Z` comment (a `docker://` image needs a sha256 digest); on a `pip install` named
in one of them that reads ranges or skips hashes, in the spellings `pip`, `pip3`, `python -m pip` and
`uv pip` with options before `install`, and on one written in a `run:` form it cannot parse; on the
`test` job losing its `--require-hashes` install, naming the test lock before the image lock, or
losing the order install, package, pytest; on `requirements-test.lock.in` differing from
`pyproject.toml`; on the test lock lacking a `test` package or a hash, repeating an image pin at
another version or with other hashes, or pinning a module the forbidden-imports guard refuses; and on
the config losing its `github-actions` block.

## Consequences

- pytest runs on the locked versions, so a Dependabot lock PR is exercised by the test suite too.
  This closes the gap ADR-0018's Consequences record, and supersedes the sentence there that the
  `test` and `no-forbidden-imports` jobs install `pyproject.toml`'s ranges. It also changes the CI
  line ADR-0010 quotes (`pip install -e ".[test]"`). The README's local command is unchanged: a
  developer's virtualenv still resolves the ranges.
- Re-locking by hand is now three files: the image lock, then the test lock (its header command takes
  the image lock as a constraint), and the build lock. Dependabot's pip-compile updater re-resolves
  each lock that has a `.in`, the image lock and the test lock (the build lock has none, ADR-0018),
  from its own. Whether it moves a package shared by the image lock and the test lock in one PR was
  not measured. If the two ever disagree, `pip` goes red on a version and the test above on a version
  or a hash, so a disagreement is a red PR and not a silent one.
- A refresh is now up to three PRs a week (locks, digests, actions), each only when something moved.
- The locked `starlette` warns about `httpx`. Nothing here acts on it; the `test` extra still names
  `httpx>=0.27`.
- Branch protection on `main` requires only `test` (`gh api repos/jpslav/linkling-api/branches/main/protection`,
  2026-09-25). Nothing here changes a repository setting.
