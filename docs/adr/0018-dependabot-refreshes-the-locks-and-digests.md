# ADR-0018 — Dependabot refreshes the locks and base-image digests, with .in files as the lock inputs

- Status: Proposed
- Approver: (pending)
- Date: 2026-09-25

## Context

ADR-0017 ii pinned the image: hash-locked `requirements.lock.txt` and `requirements-build.lock.txt`,
and `python:3.12-slim` by digest. It named the price: a pin goes stale, so a security release of
`fastapi`, `uvicorn` or `setuptools`, or a rebuilt base image, reaches no deployer until something
re-locks and re-pins. LL-024 asks that something do that without anybody remembering, and that a
human hear of it only as a PR to merge. Three Dockerfiles pin the same digest: the service's, the
stand-in site's (which `tests/test_ll028_standin_site.py` requires to match), and the observer
image `scripts/no-third-party-check.sh` builds, which was unpinned.

The obvious free mechanism is Dependabot, and the question was whether it keeps `--require-hashes`
valid on files compiled with `uv pip compile`. It was measured, not assumed: Dependabot CLI v1.93.0
(`dependabot update <ecosystem> <repo> --local <clone>`) runs the real updater images against a
checkout, on 2026-09-25, against this repo and against a copy of it whose locks were re-locked as of
2026-06-01 (`uv pip compile --exclude-newer`), so that a release wave was pending.

- **The `pip` updater on the locks as they were** treats each pinned line as a top-level
  dependency, rewrites that line and regenerates that package's hashes, and never re-resolves. The
  hashes stayed well-formed. The set stopped being installable: on today's `main` its one proposal
  moved `pydantic-core` from 2.46.5 to 2.49.0 while `pydantic` 2.13.5 requires exactly 2.46.5, and
  `pip install --dry-run --require-hashes` with the Dockerfile's own options exited 1
  (`ResolutionImpossible`). On the stale copy it proposed 13 PRs, 3 of which exit 1 (`anyio` needs a
  newer `typing-extensions`; `pydantic` and `pydantic-core` each need the other), and a single group
  of all 13 exited 1 as well.
- **With a `requirements.lock.in` beside the main lock**, Dependabot hands it to its pip-compile
  updater instead, which re-runs `pip-compile -P <name>` and keeps the file's header as it was. On
  the stale copy one group PR moved 12 of the lock's 13 pins, the coupled ones together
  (`pydantic` with `pydantic-core` 2.46.4 to 2.46.5, not 2.49.0), and left the file with all 13
  pins, 144 hash lines and uv's header. `tests/test_ll024_pins_refresh.py` passed on the tree the
  PR would leave, and so did `docker build` of the service image from the tree the lock PR and the
  digest PR would leave together: the `--require-hashes` install of both locks, the
  `--no-build-isolation` install of the package, and the websockets/wsproto guard.
- **With a `.in` beside the build lock too, it is not usable.** A first pass of this measurement
  said every PR installed; it had read only the exit status of `pip install --dry-run
  --require-hashes`, which is 0 on an empty file. Reading the PR's files showed
  `requirements-build.lock.txt` returned as its header alone, the `setuptools` pin gone, in every
  run that had the `.in`. Dependabot did pass `--allow-unsafe`, and pip-compile run by hand with the
  same flags keeps the pin (pip-tools files setuptools under "unsafe packages"), so the loss is
  downstream of pip-compile, in the updater; where was not located. Without the `.in`, the plain
  updater moved that pin (82.0.1 to 84.0.0) and kept its two hashes.
- **The `docker` updater unconfigured** proposes `python:3.12-slim` to `3.14-slim` in each of the
  three Dockerfiles, as three PRs. With the three `directories`, one group using
  `group-by: dependency-name`, and `python`'s minor and major moves ignored, it opened one PR that
  moved all three pins from a stale digest to the digest Docker Hub serves for `3.12-slim`.

## Decision

**Proposed.** `.github/dependabot.yml`, and nothing that needs a GitHub setting.

- A `pip` block on `/`, weekly on Mondays, one group of everything, so a release wave is one PR.
- A `docker` block on the three Dockerfile directories, weekly on Mondays, one group across them,
  with `python`'s `semver-minor` and `semver-major` ignored: it refreshes the digest of the tag we
  ship and never proposes another Python. Moving to another Python is its own piece of work.
- `requirements.lock.in` beside `requirements.lock.txt`, which is otherwise unchanged. It says what
  `pyproject.toml`'s `[project] dependencies` says, and `tests/test_ll024_pins_refresh.py` fails if
  they differ. `requirements-build.lock.txt` gets no `.in`, on purpose, and the test fails if one
  appears.
- The observer image is pinned by the service's digest, and that test also requires all three digests
  to be equal. It also fails on a Dockerfile no `directories` entry covers, and on a lock that is
  missing a pin it exists for or has a pin without a hash, so a refresh PR that damages a lock is
  red in the `test` job and not only in the image build.

| Option | What was measured, or why not | Recurring |
|---|---|---|
| `pip` updater on the locks as they were | Keeps hashes valid, breaks installability (above). Rejected | R4, and false-red PRs |
| **Dependabot with `.in` files, plus the docker group** | Every PR installed; no owner action; Dependabot's own runs do not count toward included Actions minutes (docs.github.com, `about-dependabot-on-github-actions-runners`) | A green PR to merge, which the manager does |
| Scheduled Actions workflow running `uv pip compile --upgrade` | Consistent by construction, but `can_approve_pull_request_reviews` is false on this repo, so it cannot open a PR without an owner setting. Not measured | The same PR, after the setting |
| Scheduled Claude routine | Would work, and spends usage on every run, which is the owner's decision (`company/DECISIONS.md` 2026-09-22) | The same PR, plus spend |
| Renovate | Needs the owner to install a GitHub App. Whether it reads uv headers was not measured | The same PR, after the install |
| `uv.lock` project mode | Rejected in ADR-0017 ii option C for the runtime toolchain it adds | n/a |

## Consequences

- A refresh arrives as at most two PRs a week, only when something moved, and CI runs on it. The
  image build in the `compose` and `no-third-party` jobs is what runs the websockets/wsproto guard
  against a new lock (`Dockerfile`), because the `test` and `no-forbidden-imports` jobs install
  `pyproject.toml`'s ranges, not the lock.
- pytest never runs against the locked versions. A lock PR is tested by the image build, the smoke
  script and the no-third-party check, not by the test suite. That gap is recorded, not closed here.
- The `pydantic` and `pydantic-core` style of coupling is handled by the pip-compile updater, but
  it lags the newest releases by whatever cooldown Dependabot applies (a full re-lock on the day it
  was measured was one release ahead on two packages).
- Dependabot reads its config from the default branch, and this file has not been through GitHub's
  own validation until it lands there: the first run after the merge is the first real one, and
  the repository's Dependabot page is where a parse error would be expected to show.
- The list of top-level requirements now lives in two places for the main lock (`pyproject.toml`
  and the `.in`), held together by a test rather than by construction. The build lock is refreshed
  by a different Dependabot updater from the main one, which is why it is a pin, not a set.
