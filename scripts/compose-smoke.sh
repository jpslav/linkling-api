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

project="${LINKLING_SMOKE_PROJECT:-linkling-smoke}"
port="${LINKLING_SMOKE_PORT:-18000}"
data="$PWD/.smoke-data/run-$(rand 6)"
canary="smoke-$(rand 8)"
target="https://example.com/linkling-smoke/$canary?q=1"
base="http://127.0.0.1:$port"

# Exported, so that they override anything in a .env beside compose.yaml.
export LINKLING_API_KEY="$(rand 24)"
export LINKLING_DATA_DIR="$data"
export LINKLING_PORT="$port"

dc() { docker compose -p "$project" "$@"; }

cleanup() { dc down >/dev/null 2>&1 || true; }
trap cleanup EXIT

start() {
    local state
    if ! dc up -d --build --wait --wait-timeout 120 api >/dev/null 2>&1; then
        echo "--- last api log lines:" >&2
        dc logs --no-color --tail 20 api >&2 || true
        # A service that started and then died was seen failing; anything else (a build
        # that never finished, a container that never came up) is the check not seeing.
        state="$(dc ps -a --format '{{.State}}' api 2>/dev/null || true)"
        case "$state" in
            exited|restarting|dead) fail "the api service is $state ($1)" ;;
        esac
        blind "the api service never became healthy ($1; state '${state:-none}')"
    fi
}

# Follows the canary once and prints "<status> <Location>".
follow() {
    curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "$base/$canary"
}

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

status="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$base/-/api/links" \
    -H "Authorization: Bearer $LINKLING_API_KEY" -H 'Content-Type: application/json' \
    -d "{\"url\": \"$target\", \"name\": \"$canary\"}")"
[ "$status" = 201 ] || fail "creating the canary link answered $status, not 201"

got="$(follow)"
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

got="$(follow)"
[ "$got" = "302 $target" ] || fail "after down and up the follow gave '$got', not '302 $target'"
echo "follow after restart:  $got"

check_logs "after restart"

echo "pass"
