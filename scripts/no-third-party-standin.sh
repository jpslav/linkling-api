#!/usr/bin/env bash
# Shows that the site half of scripts/no-third-party-check.sh goes red when it should (LL-028).
# CI crawls the real linkling-web too (LL-034), but a clean site cannot show the crawl going red
# or blind, so those paths are shown against a stand-in (tests/fixtures/standin-site, whose
# server.py says what it is and what it is not).
# This runs the WHOLE check, not --api-only, against that stand-in with one deliberate defect put
# in it, and succeeds only if the check ends red the way that defect must turn it:
#
#   external-stylesheet  `fail`, naming the stylesheet on another origin that the home page links
#   cut-short-reply      `blind`, because a stylesheet's reply stopped before its end (before
#                        LL-025, the check said `pass` here)
#   stalled-reply        `blind`, because a stylesheet's reply never ended and the check's own time
#                        bound (LINKLING_NO3P_MAX_TIME, forced to 5 here) stopped it
#   never-listens        `blind`, because the stack never came up healthy: the site's container
#                        runs and binds nothing, and compose.yaml's `web` healthcheck must keep
#                        `docker compose up --wait` from returning (LL-029). The check's output
#                        must also hold a line saying the `web` container is unhealthy (not the api,
#                        not the observer in front of the site), or the stack failed to come up for
#                        some other reason and this proved nothing about the healthcheck
#
# It judges the exit status AND the check's last line, not the status alone: `blind` is exit 2
# whether the crawl saw a cut-short reply or the stack never came up, and only the first is what
# the cut-short fixture is for. It also requires the check's banner to name the fixture
# (`site fixture <name>`, from LINKLING_NO3P_FIXTURE, which this sets), so a CI log that shows a
# red run says which defect that run was put there for. The unmutated stand-in has no fixture
# here: it is the check itself,
# `LINKLING_WEB_DIR=tests/fixtures/standin-site scripts/no-third-party-check.sh`, which must `pass`.
#
# The check's own environment applies (LINKLING_NO3P_PROJECT, LINKLING_NO3P_PORT,
# LINKLING_NO3P_WEB_PORT, LINKLING_NO3P_MAX_TIME, all in that script's header). Nothing here
# bounds a check that hangs: CI's `timeout-minutes` does, and macOS has no timeout(1).
#
# Usage: scripts/no-third-party-standin.sh external-stylesheet|cut-short-reply|stalled-reply|never-listens
# Exit status: 0 `pass` (the check ended red, as this fixture requires, and its banner named the
# fixture), 1 `fail` (it ended any other way: green, or red in another way, or right but with a
# banner that does not name the fixture), 2 `blind` (the check was blind for a reason that is not
# this fixture's, or there is no stand-in to mutate, or the mutated copy could not be built), 64
# for a usage error.

set -euo pipefail

cd "$(dirname "$0")/.."

fail() { echo "fail: $*" >&2; exit 1; }
blind() { echo "blind: $*" >&2; exit 2; }
usage() { echo "usage: $0 external-stylesheet|cut-short-reply|stalled-reply|never-listens" >&2; exit 64; }

[ $# = 1 ] || usage
fixture="$1"
# What the check's output must also hold, beyond its last line: `want_web_unhealthy` says whether a
# line must report the `web` container unhealthy, and `want_also` is how the messages below put it.
want_web_unhealthy=0
want_also=""
case "$fixture" in
    external-stylesheet)
        want_status=1
        want_text="fail: web serves / with a third-party reference: in its body https://styles.standin.invalid/site.css" ;;
    cut-short-reply)
        want_status=2
        want_text="blind: the site's /second.css answered '200' but its reply did not arrive whole (the reply stopped before its end)" ;;
    stalled-reply)
        want_status=2
        export LINKLING_NO3P_MAX_TIME=5
        want_text="blind: the site's /second.css answered '200' but its reply did not arrive whole (curl timed out after 5s)" ;;
    never-listens)
        want_status=2
        want_text="blind: the stack never came up healthy, so nothing was exercised"
        want_web_unhealthy=1
        want_also="a line saying the web container is unhealthy" ;;
    *) usage ;;
esac

standin=tests/fixtures/standin-site
check=scripts/no-third-party-check.sh
[ -f "$standin/Dockerfile" ] && [ -f "$standin/server.py" ] \
    || blind "no stand-in site at $standin, so there is nothing to mutate"
[ -f "$check" ] || blind "no check at $check, so there is nothing to run"

work="$(mktemp -d)" || blind "could not make a temporary directory, so there is no mutated stand-in to build"
trap 'rm -rf "$work"' EXIT
{ mkdir "$work/site" && cp -R "$standin/." "$work/site/" && printf '%s\n' "$fixture" >"$work/site/mode"; } \
    || blind "could not make the mutated copy of the stand-in in $work, so there is nothing to check"

echo "running $check in full against the stand-in with the '$fixture' defect: it must end $want_status ('${want_text%%:*}')"
# Its output is shown as it comes and kept, so that the last line can be read. The status is the
# check's, not tee's.
set +e
LINKLING_WEB_DIR="$work/site" LINKLING_NO3P_FIXTURE="$fixture" bash "$check" 2>&1 | tee "$work/out"
status="${PIPESTATUS[0]}"
set -e
last="$(tail -n 1 "$work/out")"

case "$last" in
    "$want_text"*) text_ok=1 ;;
    *) text_ok=0 ;;
esac
also_ok=1
# Compose names the site's container `<project>-web-1` and the observer in front of it
# `<project>-obs-web-1`, and prints `container <name> is unhealthy` for the one that failed. One awk,
# not a pipeline of greps: `grep -q` at the end of one would leave the others a SIGPIPE under pipefail.
if [ "$want_web_unhealthy" = 1 ] \
    && ! awk '/ is unhealthy/ && /-web-1 is unhealthy/ && !/obs-web-1/ { found = 1 } END { exit !found }' "$work/out"; then
    also_ok=0
fi
if [ "$status" = "$want_status" ] && [ "$text_ok" = 1 ] && [ "$also_ok" = 1 ]; then
    # The banner is the first thing the check prints that a reader of CI's log will look for, and
    # it must say this run is not the clean site (`mutation none` used to be all it said).
    grep -qF "site fixture $fixture," "$work/out" \
        || fail "the check ended as the '$fixture' defect requires, but its banner does not name the fixture ('site fixture $fixture,'), so the log would not say which defect this run was put there for"
    echo "pass: the check ended $status on the '$fixture' defect, as it must, and its banner named the fixture"
elif [ "$status" = 2 ]; then
    blind "the check was blind on '$fixture' in a way that is not this fixture's, so it showed nothing. It must end: $want_text${want_also:+, and its output must hold $want_also} ... It ended: $last"
else
    fail "the check must end $want_status ('${want_text%%:*}') on '$fixture'. It must end: $want_text${want_also:+, and its output must hold $want_also} ... It ended $status: $last"
fi
