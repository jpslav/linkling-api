# ADR-0010 — The first CI test asserts environment truth, not application behavior

- Status: Accepted
- Approver: claude
- Date: 2026-09-22

## Context

LL-002 (`R-018`) requires CI on an empty repo: pytest running on every PR, with branch
protection naming that run as a required check. `pytest` exits 5 on an empty test suite,
which is itself red — so a real test has to exist before there is any application code to
test, and LL-002's scope forbids writing that application code (no FastAPI, no routes, no
schema; that is LL-001). Whatever the first test asserts, it cannot be about behavior that
does not exist yet, and it cannot be `assert True` either — a test that cannot fail proves
nothing about the CI it sits inside, only that pytest can execute a file.

This also decides the packaging shape LL-001 builds on: ADR-0008 already fixed the
package name `linkling`, but not whether anything needs to exist on disk for CI purposes
before LL-001 lands.

## Decision

**Proposed.**

1. **`pyproject.toml` declares a real, installable, empty package** at `src/linkling/`
   (src-layout, `[tool.setuptools.packages.find] where = ["src"]`), with a `test` extra
   (`pytest>=8`). CI runs `pip install -e ".[test]"` — installing *the project's declared
   dependencies*, not a hardcoded pytest install — so when LL-001 adds FastAPI as a real
   dependency, CI does not need to change.
2. **The one real test has two parts, both asserting configuration truth rather than
   application behavior:**
   - `test_ci_pins_python_312` — `assert sys.version_info[:2] == (3, 12)`. Verified locally:
     passes under a `uv`-provisioned Python 3.12.14 venv, **fails** (`assert (3, 13) == (3,
     12)`) under a 3.13 venv. This is the test that would catch the workflow's
     `python-version` pin drifting, which is exactly the kind of misconfiguration a vacuous
     test would miss.
   - `test_linkling_package_installs` — `assert linkling.__name__ == "linkling"`. Verified
     locally: fails if the package is not installed (`ModuleNotFoundError`) or the
     `pyproject.toml`/src-layout wiring is wrong; passes once `pip install -e ".[test]"`
     actually installs it. This is the packaging self-test the "minimal packaging CI needs"
     scope line asks for.
3. **Rejected: `assert True` / a bare "collects one test" placeholder.** Satisfies pytest's
   exit-5 problem but is vacuous — it cannot fail under any misconfiguration, so a broken
   pin or a broken install would still show green.
4. **Rejected: a test that imports and exercises a stub FastAPI route.** That is application
   code under a different name, and pre-empts ADR-0001/0005/0007 the way LL-002's scope
   explicitly rules out.

## Consequences

- LL-001 imports into an already-installable `linkling` package and adds real dependencies
  to `pyproject.toml`'s `dependencies` list; the CI workflow (`pip install -e ".[test]"`)
  does not need to change.
- The two tests will very likely be superseded by real application tests once LL-001 lands
  — they exist to make CI meaningful before there is anything else to check, not as
  permanent fixtures. Deleting them once real tests exist is expected, not a regression.
- `linkling-web` has no installable Python package (ADR-0008: hand-written HTML/CSS, no
  build step) and needed its own, separate version of this decision — see the sibling ADR
  in that repo.

## Decision record

Accepted 2026-09-22 by `claude`, as the program manager `pm-2`, under the *build and release
pipeline shape* row of `products/linkling/decision-policy.md`, which is Claude's with an ADR.
The brief fixes the gate — "nothing merges red" — and this records what the first test asserts
while no application code exists: that CI runs the interpreter the brief names and that the
package installs.
