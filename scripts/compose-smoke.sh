#!/usr/bin/env bash
# Brings up the compose stack's service from nothing and checks the three things LL-005 is
# about. (1) It answers. (2) A link survives `docker compose down` and `up` again, and the
# documented backup produces a sound copy. (3) Its logs hold no per-request line and no
# client address (ADR-0004, ADR-0012).
#
# It uses its own compose project, host port and data directory, so it never touches a stack
# already running from this checkout, nor that stack's database:
#   LINKLING_SMOKE_PROJECT  compose project name   (default linkling-smoke)
#   LINKLING_SMOKE_PORT     host port for the api  (default 18000)
#   LINKLING_SMOKE_MAX_TIME seconds one request may take, connecting and reading the reply both
#                           counted (default 10): a service that accepts a connection and
#                           never answers is `blind` after this long, not a hang
# The data directory is a fresh .smoke-data/run-<random>/, which is left behind afterwards:
# on Linux it ends up owned by the container's uid, and removing it is not this script's to
# risk. The team key is random per run and never printed.
#
# Only the `api` service is built. The site needs the sibling linkling-web checkout, which
# CI cannot fetch (ADR-0015 consequences).
#
# Exit status: 0 `pass`, 1 `fail: <step>`, 2 `blind: <what was absent>`. Blind means the
# check could not look, which is different from looking and finding a problem.

set -euo pipefail

cd "$(dirname "$0")/.."

fail() { echo "fail: $*" >&2; exit 1; }
blind() { echo "blind: $*" >&2; exit 2; }

command -v docker >/dev/null 2>&1 || blind "docker is not on PATH"
command -v curl >/dev/null 2>&1 || blind "curl is not on PATH"
docker info >/dev/null 2>&1 || blind "the docker daemon is not reachable"

rand() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }

# shellcheck source=lib/port-check.sh
. scripts/lib/port-check.sh

project="${LINKLING_SMOKE_PROJECT:-linkling-smoke}"
port="${LINKLING_SMOKE_PORT:-18000}"
# curl's --max-time for every request to the service. Answers on loopback took 4 ms on average
# and 18 ms at the slowest in 60 requests on Docker Desktop (LL-025), so 10 s is hundreds of times
# what a healthy one needs, room for a loaded CI runner, and still a small fraction of the CI
# job's own timeout, which is what a silent service used to cost. `--max-time 0` means no limit
# at all, so a limit that is not a whole number above 0 is refused rather than passed on.
max_time="${LINKLING_SMOKE_MAX_TIME:-10}"
case "$max_time" in
    ""|*[!0-9]*) blind "LINKLING_SMOKE_MAX_TIME must be a whole number of seconds above 0, not '$max_time'" ;;
esac
[ "$max_time" -ge 1 ] || blind "LINKLING_SMOKE_MAX_TIME must be a whole number of seconds above 0, not '$max_time'"
data="$PWD/.smoke-data/run-$(rand 6)"
canary="smoke-$(rand 8)"
target="https://example.com/linkling-smoke/$canary?q=1"
base="http://127.0.0.1:$port"

if port_free "$port"; then
    :
else
    case $? in
        1) fail "port $port is already in use (set LINKLING_SMOKE_PORT to use another)" ;;
        *) blind "could not tell whether port $port is free: $PORT_CHECK_MSG" ;;
    esac
fi

# Exported, so that they override anything in a .env beside compose.yaml. LINKLING_BIND_ADDR
# (round-3 review) matters here too: port_free() and $base above both hardcode 127.0.0.1, so
# a stray LINKLING_BIND_ADDR in a developer's own .env -- unrelated to this script, left over
# from their own deployment work -- must not reach this run's compose stack.
export LINKLING_API_KEY="$(rand 24)"
export LINKLING_DATA_DIR="$data"
export LINKLING_PORT="$port"
export LINKLING_BIND_ADDR=127.0.0.1

dc() { docker compose -p "$project" "$@"; }

cleanup() { dc down >/dev/null 2>&1 || true; }
trap cleanup EXIT

start() {
    local state
    if ! dc up -d --build --wait --wait-timeout 120 api >/dev/null 2>&1; then
        echo "--- last api log lines:" >&2
        dc logs --no-color --tail 20 api >&2 || true
        # A service that started and then died, or runs but fails its healthcheck, was seen
        # failing. Anything else (a build that never finished, a container that never came
        # up) is the check not seeing.
        state="$(dc ps -a --format '{{.State}} {{.Health}}' api 2>/dev/null || true)"
        case "$state" in
            exited*|restarting*|dead*|*unhealthy) fail "the api service is $state ($1)" ;;
        esac
        blind "the api service never became healthy ($1; state '${state:-none}')"
    fi
}

# Asks the service one question and leaves what curl's -w format printed in $answer:
# ask <step> <-w format> <curl args...>. A stack that stops answering mid-run is `blind`, said
# here with curl's reason, not curl's own exit status ending the run under `set -e` (LL-023),
# and one that never answers is `blind` after $max_time seconds (LL-025), not after CI's own
# timeout. What curl printed decides it: curl prints 000 when no HTTP status came back. The exit
# status only names the reason, and nothing overwrites $answer when it is non-zero, because a
# status curl did print survives a non-zero exit (a body cut short, say) and `|| answer=000`
# would have discarded it.
ask() {
    local step="$1" fmt="$2" rc why
    shift 2
    if answer="$(curl -s --max-time "$max_time" -o /dev/null -w "$fmt" "$@")"; then rc=0; else rc=$?; fi
    case "$answer" in
        ""|000*)
            case "$rc" in
                7) why="curl could not connect" ;;
                28) why="curl timed out after ${max_time}s" ;;
                52) why="the connection closed with no reply" ;;
                56) why="receiving the reply failed" ;;
                *) why="curl exit status $rc" ;;
            esac
            blind "no answer from $base while $step ($why), so the run went no further" ;;
    esac
}

# Follows the canary once and leaves "<status> <Location>" in $answer.
follow() { ask "following the canary $1" '%{http_code} %{redirect_url}' "$base/$canary"; }

# The service's logs must show that it started (so an empty log cannot pass as a clean one),
# and must not mention the canary's path or any IPv4 address except the 0.0.0.0 it binds.
check_logs() {
    local logs addrs
    logs="$(dc logs --no-color api)"
    grep -q "Uvicorn running on" <<<"$logs" \
        || blind "no uvicorn startup line in the api logs ($1), so their silence proves nothing"
    if grep -qF "$canary" <<<"$logs"; then
        fail "the api logs name a requested path ($1): $(grep -F "$canary" <<<"$logs" | head -1)"
    fi
    addrs="$(grep -oE '([0-9]{1,3}\.){3}[0-9]{1,3}' <<<"$logs" | grep -vx '0\.0\.0\.0' || true)"
    [ -z "$addrs" ] || fail "the api logs hold an IPv4 address ($1): $(head -1 <<<"$addrs")"
}

echo "project $project, api on $base, data in $data"
mkdir -p "$(dirname "$data")"

start "first start"

ask "creating the canary link" '%{http_code}' -X POST "$base/-/api/links" \
    -H "Authorization: Bearer $LINKLING_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"url\": \"$target\", \"name\": \"$canary\"}"
status="$answer"
[ "$status" = 201 ] || fail "creating the canary link answered $status, not 201"

follow "before the restart"
got="$answer"
[ "$got" = "302 $target" ] || fail "the first follow gave '$got', not '302 $target'"
echo "follow before restart: $got"

[ -f "$data/linkling.db" ] || fail "no linkling.db in the bind-mounted $data"

# The README's backup command, verbatim apart from the file name.
dc exec -T -u linkling api sqlite3 /data/linkling.db ".backup '/data/backup-smoke.db'" \
    || fail "the documented backup command failed"
[ -f "$data/backup-smoke.db" ] || fail "the backup did not appear on the host at $data"
integrity="$(dc exec -T -u linkling api sqlite3 /data/backup-smoke.db 'PRAGMA integrity_check;')"
[ "$integrity" = ok ] || fail "the backup's integrity_check said '$integrity'"
rows="$(dc exec -T -u linkling api sqlite3 /data/backup-smoke.db \
    "SELECT count(*) FROM links WHERE name = '$canary' AND target = '$target';")"
[ "$rows" = 1 ] || fail "the backup holds $rows rows for the canary, not 1"

# A container's logs go with it at `down`, so read them before stopping it.
check_logs "before restart"

dc down >/dev/null 2>&1 || fail "docker compose down failed"
start "after down and up"

follow "after down and up"
got="$answer"
[ "$got" = "302 $target" ] || fail "after down and up the follow gave '$got', not '302 $target'"
echo "follow after restart:  $got"

check_logs "after restart"

echo "pass"
