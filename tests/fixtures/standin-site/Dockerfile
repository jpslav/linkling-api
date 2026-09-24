# The stand-in for linkling-web that scripts/no-third-party-check.sh is run against in CI
# (LL-028): server.py says what it is and what it is not. It is built by that check, from a
# directory it is pointed at with LINKLING_WEB_DIR, and nothing in the shipped stack uses it.
#
# The same digest as the service's own Dockerfile, for the same reason (ADR-0017, its
# alternatives table: a tag can move under a rebuild). That file says how to re-measure the
# digest; when it is moved, move this one too, because tests/test_ll028_standin_site.py fails
# if they differ.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

# `mode` picks what server.py serves. scripts/no-third-party-standin.sh builds its mutations by
# putting another word in it.
COPY server.py mode /
CMD ["python", "/server.py"]
