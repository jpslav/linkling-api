# The Linkling service's image. How it is assembled is ADR-0015; the topology it sits in is
# ADR-0007; the base-image pin and the locked install are ADR-0017. There is deliberately no
# VOLUME instruction: that would give /data an anonymous volume whenever nothing is mounted
# there, which is one `docker compose down -v` from gone.
#
# Digest measured 2026-09-23 (`docker pull python:3.12-slim && docker inspect python:3.12-slim
# --format '{{index .RepoDigests 0}}'`); re-measure and re-pin the same way to move it forward,
# rather than dropping back to the floating tag.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# sqlite3 is here for the backup command the README documents (ADR-0007b). It runs inside this
# container, on the same host as the service, because the service opens the database in WAL
# mode and WAL requires that (https://sqlite.org/wal.html). Not pinned: LL-022's "Done when"
# is about the Python dependencies and the base image, not apt packages -- out of scope here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 linkling \
    && useradd --system --uid 10001 --gid 10001 --no-create-home \
       --home-dir /nonexistent --shell /usr/sbin/nologin linkling

WORKDIR /app
# The hashed dependency install is copied and run before the source, so a source-only change
# invalidates only the cheap layers below it, not this one -- pip re-verifying 13 packages'
# worth of hashes on every one-line src/ edit would make the build slow for no reason.
# requirements-build.lock.txt (round-2 review) locks the PEP-517 build backend itself
# (setuptools, from pyproject.toml's [build-system] requires): without it and without
# --no-build-isolation below, pip fetches setuptools fresh, unhashed, into a throwaway build
# env on every image build -- a real gap in the reproducibility ADR-0017 exists to close,
# confirmed by running this install with --network=none and watching it fail needing network.
COPY pyproject.toml requirements.lock.txt requirements-build.lock.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock.txt -r requirements-build.lock.txt

COPY src ./src
COPY scripts/no-forbidden-imports-check.sh /tmp/no-forbidden-imports-check.sh
# requirements.lock.txt above already pins everything pyproject.toml declares, so this install
# takes no deps of its own -- it is --require-hashes on the first install that forces the two
# installs apart (ADR-0017 ii): it refuses an unhashed local source install in the same
# invocation. --no-build-isolation makes this use the setuptools already installed above
# (hash-verified) instead of pip fetching its own unpinned copy into an isolated env. The guard
# (LL-022) runs last, against the image as it will ship, and fails the build if it finds
# websockets or wsproto.
RUN pip install --no-cache-dir --no-deps --no-build-isolation . \
    && bash /tmp/no-forbidden-imports-check.sh \
    && rm /tmp/no-forbidden-imports-check.sh

COPY --chmod=755 deploy/entrypoint.sh /usr/local/bin/linkling-entrypoint

# stdout is a pipe here, so Python would block-buffer it, and uvicorn re-raises the SIGTERM it
# caught, so a buffer is never flushed on stop. Anything written there would then never reach
# `docker compose logs`, and a check that those logs hold no client address would pass
# without having seen it (tests/test_ll004_no_trace.py measured the same thing).
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT ["linkling-entrypoint"]
# The README's own invocation, with only the host changed. --no-access-log is ADR-0012's
# requirement: uvicorn's access log writes every clicker's IP address.
CMD ["uvicorn", "--factory", "linkling.server.app:create_app", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
