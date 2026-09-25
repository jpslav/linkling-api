#!/usr/bin/env bash
# Brings up the compose stack and shows that it sends nothing to anyone but the client
# (ADR-0009, LL-018). Every packet the `api` and `web` containers send is captured, from
# before each one starts, while the check creates a link, follows it, follows a name that does
# not exist, opens the stats page with and without the team key, reads the link's counts with
# and without it, fetches the privacy manifest, deletes the link, follows it and reads its
# counts again, and loads the public site. A sent packet that is not a reply to the check's own
# requests fails the run, and so does any DNS query.
#
# A capture that sees nothing looks exactly like a clean one, so every run plants its own
# positive controls: from inside each captured network namespace, a TCP connection to
# 192.0.2.1:9 (TEST-NET-1, RFC 5737: it reaches nobody) and a lookup of a random
# `linkling-control-<random>.invalid`. Both must show up in the capture, as must the service's
# replies to the check, or the run is blind. scripts/no-third-party/classify.py has the rules.
#
# The site is also judged by what it serves: `/`, `/privacy.html`, and every same-origin
# stylesheet they pull in through a <link> or an @import, followed to any depth (up to 50
# answers), are fetched. Their headers and bodies must hold no absolute or protocol-relative
# URL, no slash spelt as an entity or a CSS escape, no <script> and no inline event handler.
# That is deliberately stricter than "loads nothing": an outbound link a browser would not
# fetch fails too, until someone adds a reviewed exception here.
#
# The service's own `/-/stats` page is judged by what it serves too, but not by that scan: it
# prints every link's target as text, and a target is a URL. It is read as HTML instead
# (scripts/no-third-party/loads.py, whose docstring says what it flags and what it does not
# model), and fails on markup that names another origin or runs script: a <script>, an inline
# event handler, another origin's URL in an attribute that fetches (a src, a srcset, an href
# other than an <a>'s) or in CSS. A target shown as text, and a link a reader has to follow, pass.
# Its response headers fail on any absolute or protocol-relative URL, as the site's do. The scan
# is proved before it is trusted: it must flag a load the check plants in a page of its own, or
# the run is blind.
#
# What this cannot see, so that nobody reads more into a pass than it holds: routes and paths
# it does not exercise; anything the services would do after the run's window (on a timer, say);
# what a browser does with the pages, since no browser runs; what the host or Docker Desktop
# does outside the containers (the image build, the published-port proxy); and a proxy a
# deployer puts in front. The README's "Run it with Docker" names the same limits.
#
# Usage: scripts/no-third-party-check.sh [--api-only] [--mutate api|api-page|web-net|web-page]
#   --api-only  check only the service. CI's `no-third-party` job uses it, because CI's
#               `no-third-party-site` job runs the whole check instead, with LINKLING_WEB_DIR
#               pointed at a stand-in (tests/fixtures/standin-site, LL-028): that shows the
#               crawl runs and goes red, not that the real site is clean; and then at a checkout
#               of the public linkling-web (LL-034), which is the check of the real site.
#               Without --api-only, a missing checkout is blind, never a silent skip.
#   --mutate    layer in a deliberate leak from scripts/no-third-party/mutations/, to show the
#               check going red. Each must fail and name what it saw.
#
# Like scripts/compose-smoke.sh, it uses its own compose project, ports and data directory:
#   LINKLING_NO3P_PROJECT   compose project name   (default linkling-no3p)
#   LINKLING_NO3P_PORT      host port for the api  (default 18100)
#   LINKLING_NO3P_WEB_PORT  host port for the site (default 18180)
#   LINKLING_NO3P_MAX_TIME  seconds one whole request may take, curl's --max-time (default 10):
#                           a service or site that accepts a connection and never answers is
#                           `blind` after this long, not a hang
#   LINKLING_WEB_DIR        the linkling-web checkout (default ../linkling-web, as compose.yaml);
#                           any directory whose Dockerfile builds a container that serves `/`,
#                           `/privacy.html` and their stylesheets on port 80 will do, which is how
#                           CI runs the stand-in and a checkout of the real site
#   LINKLING_NO3P_FIXTURE   the name of the deliberate defect the site was built with, set by the
#                           wrapper that put one there (scripts/no-third-party-standin.sh). It is
#                           only printed, in the banner, so a log of a red run says which defect it
#                           was for; the check judges what it sees, not what it is told (default none)
# Each run gets a fresh .smoke-data/no3p-run-<random>/, holding the service's database and the
# captures as tcpdump prints them. It is left behind, as compose-smoke.sh leaves its own: on
# Linux the database directory ends up owned by the service's uid, 10001, which the entrypoint
# chowns it to (deploy/entrypoint.sh). The team key is random per run and never printed, but
# the kept captures hold it, in the requests they recorded; it is good only for that run's
# stack, which is gone when the script ends.
# Two more exist only so the blind states can be shown: LINKLING_NO3P_CAPTURE_FILTER gives
# tcpdump a filter (one that matches nothing makes the controls go missing), and
# LINKLING_NO3P_STOP_OBSERVER=api|web stops that observer before its capture is read. A run
# with a filter set is blind at best, never `pass`: a filter can keep the controls and drop a leak.
#
# Exit status: 0 `pass`, 1 `fail: <what was sent or served>`, 2 `blind: <what was absent>`,
# 64 for a usage error.

set -euo pipefail

cd "$(dirname "$0")/.."

fail() { echo "fail: $*" >&2; exit 1; }
blind() { echo "blind: $*" >&2; exit 2; }
usage() { echo "usage: $0 [--api-only] [--mutate api|api-page|web-net|web-page]" >&2; exit 64; }

api_only=0
mutate=""
while [ $# -gt 0 ]; do
    case "$1" in
        --api-only) api_only=1 ;;
        --mutate) [ $# -ge 2 ] || usage; mutate="$2"; shift ;;
        *) usage ;;
    esac
    shift
done
case "$mutate" in
    ""|api|api-page) ;;
    web-net|web-page) [ "$api_only" = 0 ] || usage ;;
    *) usage ;;
esac

# curl's --max-time for every request the check makes. In one measurement (30 creates and 30
# follows against the api container on Docker Desktop, over loopback) a request took 4 ms on
# average and 18.5 ms at the slowest, so 10 s is over five hundred times what a healthy one
# needed, room for a loaded CI runner, and still far below a CI job's own timeout, which is what
# a silent service used to run into. `--max-time 0` means no limit at all, so a limit that is
# not a whole number above 0 is a usage error, not passed on.
max_time="${LINKLING_NO3P_MAX_TIME:-10}"
case "$max_time" in
    ""|*[!0-9]*) echo "LINKLING_NO3P_MAX_TIME must be a whole number of seconds above 0, not '$max_time'" >&2; exit 64 ;;
esac
[ "$max_time" -ge 1 ] || { echo "LINKLING_NO3P_MAX_TIME must be a whole number of seconds above 0, not '$max_time'" >&2; exit 64; }

command -v docker >/dev/null 2>&1 || blind "docker is not on PATH"
command -v curl >/dev/null 2>&1 || blind "curl is not on PATH"
docker info >/dev/null 2>&1 || blind "the docker daemon is not reachable"

services=(api)
if [ "$api_only" = 0 ]; then
    web_dir="${LINKLING_WEB_DIR:-../linkling-web}"
    [ -f "$web_dir/Dockerfile" ] \
        || blind "no linkling-web checkout at $web_dir, so the site cannot be checked (--api-only checks the service alone)"
    services+=(web)
fi

rand() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

# shellcheck source=lib/port-check.sh
. scripts/lib/port-check.sh

project="${LINKLING_NO3P_PROJECT:-linkling-no3p}"
port="${LINKLING_NO3P_PORT:-18100}"
web_port="${LINKLING_NO3P_WEB_PORT:-18180}"
run="$PWD/.smoke-data/no3p-run-$(rand 6)"
data="$run/data"
# Each capture, as tcpdump prints it, is kept here after the run, so a red one can be read.
captures="$run/captures"
canary="no3p-$(rand 8)"
# A host name, not an address: a fetcher that guards against reserved addresses would skip a
# TEST-NET target and send nothing, while any fetcher has to look a name up first. It is under
# example.com, a name with an ordinary public suffix, because a guard can also refuse
# special-use names such as `.invalid` before looking them up. example.com is reserved for
# documentation (RFC 2606) and a random label under it does not resolve (measured: getaddrinfo
# answers "not known"), so even a fetcher that looked it up would reach nobody.
target_host="$canary.example.com"
target="http://$target_host/linkling-no3p?q=1"
base="http://127.0.0.1:$port"
site="http://127.0.0.1:$web_port"
control_addr="192.0.2.1"
control_port=9
control_name="linkling-control-$(rand 8).invalid"
work="$(mktemp -d)"

# files/dc/cleanup/trap set up here, before the port pre-checks below, not after: those checks
# can fail/blind (immediate exit), and a trap registered later would leak this $work directory
# on that exit -- round 2 review caught this once already introduced.
files=(-f compose.yaml -f scripts/no-third-party/compose.observe.yaml)
[ -z "$mutate" ] || files+=(-f "scripts/no-third-party/mutations/compose.$mutate.yaml")

dc() { docker compose -p "$project" "${files[@]}" "$@"; }

cleanup() { dc down >/dev/null 2>&1 || true; rm -rf "$work"; }
trap cleanup EXIT

if port_free "$port"; then
    :
else
    case $? in
        1) fail "port $port is already in use (set LINKLING_NO3P_PORT to use another)" ;;
        *) blind "could not tell whether port $port is free: $PORT_CHECK_MSG" ;;
    esac
fi
if [ "$api_only" = 0 ]; then
    if port_free "$web_port"; then
        :
    else
        case $? in
            1) fail "port $web_port is already in use (set LINKLING_NO3P_WEB_PORT to use another)" ;;
            *) blind "could not tell whether port $web_port is free: $PORT_CHECK_MSG" ;;
        esac
    fi
fi

# Exported, so that they override anything in a .env beside compose.yaml. LINKLING_BIND_ADDR
# (round-3 review) matters here too: port_free() and $base/$site above all hardcode 127.0.0.1,
# so a stray LINKLING_BIND_ADDR in a developer's own .env -- unrelated to this script, left
# over from their own deployment work -- must not reach this run's compose stack.
export LINKLING_API_KEY="$(rand 24)"
export LINKLING_DATA_DIR="$data"
export LINKLING_PORT="$port"
export LINKLING_WEB_PORT="$web_port"
export LINKLING_BIND_ADDR=127.0.0.1
export COMPOSE_PROJECT_NAME="$project"
# compose.observe.yaml reads this, and compose would otherwise take it from a .env file.
export LINKLING_NO3P_CAPTURE_FILTER="${LINKLING_NO3P_CAPTURE_FILTER:-}"

# `compose mutation` is --mutate's overlay and nothing else. It used to be the banner's only word on
# the subject, and it read `mutation none` on a run whose site had a defect built into it. The site
# fixture is named separately, from the wrapper that put the defect there (LL-029).
site_fixture="${LINKLING_NO3P_FIXTURE:-none}"
echo "project $project, api on $base$([ "$api_only" = 1 ] || echo ", site on $site (built from $web_dir), site fixture $site_fixture"), compose mutation ${mutate:-none}"
mkdir -p "$captures"
echo "captures will be kept in $captures"

if ! dc up -d --build --wait --wait-timeout 180 "${services[@]}" >"$work/up.log" 2>&1; then
    tail -20 "$work/up.log" >&2
    for s in "${services[@]}"; do dc logs --no-color --tail 10 "obs-$s" "$s" >&2 || true; done
    blind "the stack never came up healthy, so nothing was exercised"
fi

# --- exercise -------------------------------------------------------------------------------

# Why curl gave up, by its exit status. compose-smoke.sh's ask() has its own, shorter table of
# these; the only thing the two scripts share is scripts/lib/port-check.sh.
why_curl() {
    case "$1" in
        7) echo "curl could not connect" ;;
        18) echo "the reply stopped before its end" ;;
        28) echo "curl timed out after ${max_time}s" ;;
        52) echo "the connection closed with no reply" ;;
        56) echo "receiving the reply failed" ;;
        *) echo "curl exit status $1" ;;
    esac
}

# call <name> <what> <wanted> <-w format> <curl args...>: one request to the service, judged on
# what curl printed, which is left in $answer. Its reply body is kept in $work/<name>.body, one
# file per step, and a step whose reply is the wrong one prints its own body (and no other
# step's) before the verdict, so what the service said is in the log (LL-023, LL-025). Every
# way of not getting the wanted reply is `blind`, and names the step: curl prints 000 when no
# status came back, whatever became of the connection (LL-025 bounds the wait), and a status that
# arrived with a body cut short is not the whole reply. The capture is bracketed with `set +e`
# and trusts what curl printed: a `|| answer=000` on the substitution would overwrite a status
# curl did print whenever its exit status is non-zero for any other reason
# (observations/2026-09-23-set-minus-e-fallback-clobbers-a-captured-value.md in the program repo).
call() {
    local name="$1" what="$2" wanted="$3" fmt="$4" rc
    shift 4
    set +e
    answer="$(curl -s --max-time "$max_time" -o "$work/$name.body" -w "$fmt" "$@")"
    rc=$?
    set -e
    case "$answer" in
        ""|000*) blind "no answer from $base while $what ($(why_curl "$rc")), so the run did not exercise what it claims" ;;
    esac
    if [ "$answer" != "$wanted" ]; then
        if [ -s "$work/$name.body" ]; then
            echo "--- the reply to $what:" >&2
            head -c 2000 "$work/$name.body" >&2
            echo >&2
        fi
        blind "$what answered '$answer', not '$wanted', so the run did not exercise what it claims"
    fi
    [ "$rc" = 0 ] || blind "$what answered '$answer' but its reply did not arrive whole ($(why_curl "$rc")), so what it said was not seen"
}
auth=(-H "Authorization: Bearer $LINKLING_API_KEY")

# Built outside the command below: macOS's bash 3.2 mangles \" inside a quoted substitution.
body="{\"url\": \"$target\", \"name\": \"$canary\"}"
call create "creating the link" 201 '%{http_code}' -X POST "$base/-/api/links" \
    "${auth[@]}" -H 'Content-Type: application/json' -d "$body"
call follow "following the link" "302 $target" '%{http_code} %{redirect_url}' "$base/$canary"
call absent "following a name that does not exist" 404 '%{http_code}' "$base/$canary-absent"
# HTTP Basic with an empty user name, as R-010's verify line sends it. It runs while the link
# still exists, so a page that answered 200 without listing the link is blind, not a pass. Its
# headers are kept too, for the markup scan below.
call stats "the stats page with the team key" 200 '%{http_code}' -D "$work/stats.head" \
    -u ":$LINKLING_API_KEY" "$base/-/stats"
grep -qF "$canary" "$work/stats.body" \
    || blind "the stats page answered 200 without the link the check made, so it was not exercised"
call stats-anon "the stats page without a credential" 401 '%{http_code}' "$base/-/stats"
# The link's counts, while it still exists and has been followed exactly once above: an answer
# that is 200 without that one follow in it is blind, not a pass. The date is matched as a
# shape, not read off the clock, so a run that straddles midnight UTC cannot fail on it.
call counts "the link's counts with the team key" 200 '%{http_code}' "$base/-/api/links/$canary/stats" "${auth[@]}"
grep -qE '"days": *\{"[0-9]{4}-[0-9]{2}-[0-9]{2}": *1\}' "$work/counts.body" \
    || blind "the link's counts answered 200 without the one follow the check made, so it was not exercised"
call counts-anon "the link's counts without a credential" 401 '%{http_code}' "$base/-/api/links/$canary/stats"
call privacy "the privacy manifest" 200 '%{http_code}' "$base/-/privacy.json"
call delete "deleting the link" 204 '%{http_code}' -X DELETE "$base/-/api/links/$canary" "${auth[@]}"
call gone "following the deleted link" 410 '%{http_code}' "$base/$canary"
call gone-counts "the deleted link's counts" 410 '%{http_code}' "$base/-/api/links/$canary/stats" "${auth[@]}"
echo "api exercised: create, follow, follow of a missing name, /-/stats with and without the team key, /-/api/links/<name>/stats with and without the team key, /-/privacy.json, delete, follow of the deleted link, /-/api/links/<name>/stats of the deleted link"

findings=()

# The stats page's markup and headers, fetched by the exercise above. Not by the site's grep
# below: the page prints every link's target as text, and loads.py says what it flags instead.
# A scan that finds nothing looks like a clean page, so it is shown first that it can find
# something: it gets a page of its own that loads a stylesheet from a .invalid host, and must
# name that host.
loads() { dc exec -T obs-api python3 /usr/local/bin/loads.py "$@"; }
planted='<!doctype html><title>control</title><link rel="stylesheet" href="https://loads-control.invalid/x.css">'
seen="$(printf '%s\n' "$planted" | loads html)" \
    || blind "could not run the markup scan in the api observer, so the stats page was not scanned"
case "$seen" in
    *loads-control.invalid*) ;;
    *) blind "the markup scan did not flag the load it plants in a page of its own, so a clean stats page proves nothing" ;;
esac
[ -s "$work/stats.head" ] || blind "no response headers were kept for /-/stats, so they were not scanned"
page_hits="$(loads html <"$work/stats.body")" \
    || blind "could not run the markup scan on the stats page"
head_hits="$(loads headers <"$work/stats.head")" \
    || blind "could not run the header scan on the stats page"
hits="$(printf '%s\n%s\n' "$page_hits" "$head_hits" | sed -n '/./p' | sed -n '1,3p')"
[ -z "$hits" ] || findings+=("api serves /-/stats with a third-party load: $(tr '\n' ' ' <<<"$hits")")
echo "stats page scanned: markup and headers"

if [ "$api_only" = 0 ]; then
    # Breadth-first from the two pages, following every same-origin stylesheet a page links and
    # every @import a stylesheet or a <style> makes, relative or absolute (refs.py). A remote one is not fetched: the
    # scan below reports it. Every answer is kept and scanned, a redirect's headers included.
    queue=(/ /privacy.html)
    fetched=0
    pages=""
    sheets=0
    while [ "$fetched" -lt "${#queue[@]}" ] && [ "$fetched" -lt 50 ]; do
        path="${queue[$fetched]}"
        f="$work/site-$fetched"
        fetched=$((fetched + 1))
        printf '%s\n' "$path" >"$f.path"
        # -g: a path is a path, not one of curl's globs. A fetch that did not finish is `blind`
        # here, never skipped (LL-025): 000 is no status at all, and a status with a non-zero curl
        # exit is a reply that stopped short, whose end nobody read. It used to become a 000 and
        # be passed over, so a stylesheet cut short was not followed for its @imports, what never
        # arrived of it was never seen, and the run said `pass`. The capture trusts what curl
        # printed, as `call` does.
        set +e
        status="$(curl -g -s --max-time "$max_time" -D "$f.head" -o "$f.body" -w '%{http_code} %{content_type}' "$site$path")"
        rc=$?
        set -e
        case "$status" in
            ""|000*) blind "no answer from $site for $path ($(why_curl "$rc")), so what the site serves there was not seen" ;;
        esac
        [ "$rc" = 0 ] || blind "the site's $path answered '${status%% *}' but its reply did not arrive whole ($(why_curl "$rc")), so what it serves there was not seen"
        case "$status" in
            "200 text/html"*) pages="$pages $path"; kind=html ;;
            "200 text/css"*) sheets=$((sheets + 1)); kind=css ;;
            *) continue ;;
        esac
        # An HTML parser, in the observer, which has Python; the host needs only curl.
        refs="$(dc exec -T obs-web python3 /usr/local/bin/refs.py "$kind" "$site$path" <"$f.body")" \
            || blind "could not list the references in $path, so the crawl may have stopped short"
        while IFS= read -r next; do
            [ -n "$next" ] || continue
            case " ${queue[*]} " in *" $next "*) ;; *) queue+=("$next") ;; esac
        done <<<"$refs"
    done
    for path in / /privacy.html; do
        case "$pages " in
            *" $path "*) ;;
            *) blind "the site's $path did not answer 200 with HTML, so its content was not seen" ;;
        esac
    done
    [ "$sheets" -ge 1 ] || blind "no stylesheet linked from the site answered 200, so the crawl did not reach it"
    for head in "$work"/site-*.head; do
        n="${head%.head}"
        # A browser can read `https:/host`, `\\host` and `//host` as another origin too (the
        # WHATWG URL standard treats \ as / in http(s) URLs), so any run of slashes or
        # backslashes counts, after a scheme or on its own. A slash spelt as an
        # entity or a CSS escape, a <script>, and an inline event handler (which can fetch()
        # with no <script> at all) count as well.
        hits="$( { grep -oiE '(https?:[/\\]*|[/\\][/\\])[a-z0-9][^"'"'"' )<>]*' "$n.head" "$n.body" || true; \
                   grep -oiE '&#0*47;|&#x0*2f;|&sol;|\\0*2f|<script|[[:space:]]on[a-z]+[[:space:]]*=' "$n.body" /dev/null || true; } \
                 | sed -e "s|^$n\.head:|in its headers |" -e "s|^$n\.body:|in its body |")"
        # Trimmed only after the pipeline: `head` closing it early would kill it with SIGPIPE,
        # and pipefail would end the whole run with neither a verdict nor its captures read.
        hits="$(head -3 <<<"$hits")"
        [ -z "$hits" ] || findings+=("web serves $(cat "$n.path") with a third-party reference: $(tr '\n' ' ' <<<"$hits")")
    done
    echo "site fetched:$pages and $sheets stylesheet(s), $fetched answer(s) in all, headers and bodies scanned"
fi

# --- controls, then judgement ---------------------------------------------------------------

for s in "${services[@]}"; do
    # Run from the observer, which shares the service's network namespace: the same packets.
    dc exec -T "obs-$s" python3 - "$control_addr" "$control_port" "$control_name" <<'PY' \
        || blind "could not plant the positive controls in the $s namespace"
import socket, sys
addr, port, name = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    socket.create_connection((addr, port), timeout=2).close()
except OSError:
    pass
try:
    socket.getaddrinfo(name, 80)
except OSError:
    pass
PY
done
sleep 2  # let the last packets land before the captures stop

if [ -n "${LINKLING_NO3P_STOP_OBSERVER:-}" ]; then
    dc stop "obs-$LINKLING_NO3P_STOP_OBSERVER" >/dev/null 2>&1 || true
fi

# A leak seen in one service is an observation even when another service's capture was blind,
# so every service is judged before either verdict is given, and fail outranks blind.
blinds=()
judge() { # <service> <its port>; appends to findings or blinds, or returns 0 when clean
    local s="$1" svc_port="$2" dropped verdict status
    # SIGINT makes tcpdump flush and report what the kernel dropped.
    dc exec -T "obs-$s" sh -c 'kill -INT "$(cat /cap/tcpdump.pid)" && \
        for i in $(seq 50); do kill -0 "$(cat /cap/tcpdump.pid)" 2>/dev/null || exit 0; sleep 0.2; done; exit 1' \
        >/dev/null 2>&1 \
        || { blinds+=("the $s observer is not running, or its capture would not stop, so nothing it saw can be read"); return; }
    dropped="$(dc exec -T "obs-$s" sed -n 's/^\([0-9]*\) packets\{0,1\} dropped by kernel$/\1/p' /cap/tcpdump.err || true)"
    [ -n "$dropped" ] || { blinds+=("the $s capture never reported its kernel drops, so it did not end cleanly"); return; }
    [ "$dropped" = 0 ] || { blinds+=("the kernel dropped $dropped packet(s) from the $s capture, so it is incomplete"); return; }
    dc exec -T "obs-$s" tcpdump -nn -A -r /cap/capture.pcap >"$captures/$s.txt" 2>/dev/null || true
    set +e
    verdict="$(dc exec -T "obs-$s" python3 /usr/local/bin/classify.py --service "$s" --service-port "$svc_port" \
        --control-addr "$control_addr.$control_port" --control-name "$control_name" \
        --target-host "$target_host" <"$captures/$s.txt")"
    status=$?
    set -e
    echo "$verdict"
    case "$status" in
        0) ;;
        1) findings+=("$(tail -1 <<<"$verdict" | sed 's/^fail: //')") ;;
        *) blinds+=("$(tail -1 <<<"$verdict" | sed 's/^blind: //')") ;;
    esac
}
judge api 8000
[ "$api_only" = 1 ] || judge web 80

join() { printf '%s; ' "$@" | sed 's/; $//'; }
[ "${#findings[@]}" = 0 ] || fail "$(join "${findings[@]}")"
[ "${#blinds[@]}" = 0 ] || blind "$(join "${blinds[@]}")"

[ -z "$LINKLING_NO3P_CAPTURE_FILTER" ] \
    || blind "the capture was filtered ('$LINKLING_NO3P_CAPTURE_FILTER'), so what the filter dropped was never judged"

if [ "$api_only" = 1 ]; then
    echo "pass (api only: web not checked)"
else
    echo "pass"
fi
