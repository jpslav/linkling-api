#!/usr/bin/env bash
# Shows that the site half of scripts/no-third-party-check.sh goes red when it should (LL-028).
# CI cannot read the private linkling-web, so the check's crawl of the site runs there against a
# stand-in (tests/fixtures/standin-site, whose server.py says what it is and what it is not).
# This runs the WHOLE check, not --api-only, against that stand-in with one deliberate defect put
# in it, and succeeds only if the check ends red the way that defect must turn it:
#
#   external-stylesheet  `fail`, naming the stylesheet on another origin that the home page links
#   cut-short-reply      `blind`, because a stylesheet's reply stopped before its end (before
#                        LL-025, the check said `pass` here)
#   stalled-reply        `blind`, because a stylesheet's reply never ended and only the check's
#                        own time bound (LINKLING_NO3P_MAX_TIME, forced to 5 here) stopped it
#
# It judges the exit status AND the check's last line, not the status alone: `blind` is exit 2
# whether the crawl saw a cut-short reply or the stack never came up, and only the first is what
# the cut-short fixture is for. The unmutated stand-in has no fixture here: it is the check itself,
# `LINKLING_WEB_DIR=tests/fixtures/standin-site scripts/no-third-party-check.sh`, which must `pass`.
#
# The check's own environment applies (LINKLING_NO3P_PROJECT, LINKLING_NO3P_PORT,
# LINKLING_NO3P_WEB_PORT, LINKLING_NO3P_MAX_TIME, all in that script's header). Nothing here
# bounds a check that hangs: CI's `timeout-minutes` does, and macOS has no timeout(1).
#
# Usage: scripts/no-third-party-standin.sh external-stylesheet|cut-short-reply|stalled-reply
# Exit status: 0 `pass` (the check ended red, as this fixture requires), 1 `fail` (it ended any
# other way: green, or red in another way), 2 `blind` (it was blind for a reason that is not this
# fixture's, or there is no stand-in to mutate), 64 for a usage error.

set -euo pipefail

cd "$(dirname "$0")/.."

fail() { echo "fail: $*" >&2; exit 1; }
blind() { echo "blind: $*" >&2; exit 2; }
usage() { echo "usage: $0 external-stylesheet|cut-short-reply|stalled-reply" >&2; exit 64; }

[ $# = 1 ] || usage
fixture="$1"
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
    *) usage ;;
esac

standin=tests/fixtures/standin-site
check=scripts/no-third-party-check.sh
[ -f "$standin/Dockerfile" ] && [ -f "$standin/server.py" ] \
    || blind "no stand-in site at $standin, so there is nothing to mutate"
[ -f "$check" ] || blind "no check at $check, so there is nothing to run"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir "$work/site"
cp -R "$standin/." "$work/site/"
printf '%s\n' "$fixture" >"$work/site/mode"

echo "running $check in full against the stand-in with the '$fixture' defect: it must end $want_status ('${want_text%%:*}')"
# Its output is shown as it comes and kept, so that the last line can be read. The status is the
# check's, not tee's.
set +e
LINKLING_WEB_DIR="$work/site" bash "$check" 2>&1 | tee "$work/out"
status="${PIPESTATUS[0]}"
set -e
last="$(tail -n 1 "$work/out")"

case "$last" in
    "$want_text"*) text_ok=1 ;;
    *) text_ok=0 ;;
esac
if [ "$status" = "$want_status" ] && [ "$text_ok" = 1 ]; then
    echo "pass: the check ended $status on the '$fixture' defect, as it must"
elif [ "$status" = 2 ]; then
    blind "the check was blind on '$fixture' for another reason, so the fixture showed nothing: $last"
else
    fail "the check must end $want_status ('${want_text%%:*}') on '$fixture'; it ended $status: $last"
fi
