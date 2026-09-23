#!/usr/bin/env bash
# LL-022: fails if `websockets` or `wsproto` is importable by this Python. Either one makes
# uvicorn pick a WebSocket protocol that logs a rejected connection's client address to its
# error logger -- `'%s - "WebSocket %s" 403'` on `uvicorn.error` -- and `--no-access-log` does
# not touch that logger, only `uvicorn.access`. Neither package is a declared dependency
# today; this is the guard against a future transitive one pulling either in silently, run
# as a Docker build step (Dockerfile) so it fails the build, not just a CI job.
#
# Exit status: 0 pass, 1 fail: <module(s)>, 2 blind: <what was absent>.
set -euo pipefail

PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || { echo "blind: $PYTHON is not on PATH" >&2; exit 2; }

"$PYTHON" - <<'PY'
import importlib.util, sys
blocked = [m for m in ("websockets", "wsproto") if importlib.util.find_spec(m) is not None]
if blocked:
    print("fail: importable: " + ", ".join(blocked), file=sys.stderr)
    sys.exit(1)
print("pass")
PY
