# The observer for scripts/no-third-party-check.sh: tcpdump, and the classifier that judges what
# it captured, refs.py, which lists what the site's pages pull in, and loads.py, which lists what
# the service's own stats page would make a browser load. The same base image, pinned by the
# same digest, as the service's Dockerfile, so the check pulls no new image and the observer
# is refreshed with the service's pin (.github/dependabot.yml; tests/test_ll024_pins_refresh.py
# fails if the two digests differ). tcpdump comes from Debian's archive when this is built.
# It is built and run only by the check; nothing in the shipped stack uses it.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9

RUN apt-get update \
    && apt-get install -y --no-install-recommends tcpdump \
    && rm -rf /var/lib/apt/lists/*

COPY classify.py refs.py loads.py /usr/local/bin/
COPY observe.sh /usr/local/bin/observe
