# The observer for scripts/no-third-party-check.sh: tcpdump, and the classifier that judges what
# it captured. The same base image the service already uses, so the check pulls nothing new.
# It is built and run only by the check; nothing in the shipped stack uses it.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends tcpdump \
    && rm -rf /var/lib/apt/lists/*

COPY classify.py refs.py /usr/local/bin/
COPY observe.sh /usr/local/bin/observe
