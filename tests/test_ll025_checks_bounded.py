"""LL-025 -- neither compose check can hang on a service that accepts and never answers.

scripts/compose-smoke.sh and scripts/no-third-party-check.sh both need Docker, and CI runs them
in their own jobs. What they do when the service goes silent is never exercised there, because
CI's service always answers. So this runs each one unchanged, with a stand-in `docker` on PATH
whose `compose up` starts a listener that accepts every connection and never replies, and asks
for a verdict inside a bound: `blind`, exit 2, naming the step that got no answer. Without
the limit neither comes back on its own, and the bound below kills it.

The scripts are copied to a temporary tree first, because they create `.smoke-data/` beside
themselves and the repository should not collect it.
"""

from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# A bound on the whole run. The scripts get LINKLING_*_MAX_TIME=2, so a fixed script answers in a
# few seconds; a script that ignores the limit is killed here and the test fails on the timeout.
BOUND_SECONDS = 40

FAKE_DOCKER = """#!/usr/bin/env bash
# `info` succeeds; `compose ... up` starts the silent listener on $LINKLING_PORT and waits until
# it is listening; `compose ... down` stops it; anything else succeeds and prints nothing.
state="${FAKE_DOCKER_STATE:?}"
[ "$1" = info ] && exit 0
[ "$1" = compose ] || exit 0
shift
[ "$1" = -p ] && shift 2
while [ "$1" = -f ]; do shift 2; done
case "$1" in
    up)
        rm -f "$state/ready"
        "$FAKE_DOCKER_PYTHON" "$state/silent.py" "$LINKLING_PORT" "$state/ready" >/dev/null 2>&1 &
        echo $! >"$state/pid"
        for _ in $(seq 50); do [ -f "$state/ready" ] && exit 0; sleep 0.1; done
        exit 1 ;;
    down)
        [ -f "$state/pid" ] && kill "$(cat "$state/pid")" 2>/dev/null
        exit 0 ;;
    *) exit 0 ;;
esac
"""

SILENT = """import pathlib, socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(16)
pathlib.Path(sys.argv[2]).write_text("ready\\n")
held = []
while True:
    conn, _ = s.accept()
    held.append(conn)  # held open, never read from, never written to
"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def silent_stack(tmp_path):
    """A copy of scripts/, a stand-in docker, and the environment that points them at each other."""
    shutil.copytree(ROOT / "scripts", tmp_path / "scripts")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "state"
    state.mkdir()
    (state / "silent.py").write_text(SILENT)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_DOCKER_STATE": str(state),
        "FAKE_DOCKER_PYTHON": sys.executable,
        "LINKLING_SMOKE_PORT": str(_free_port()),
        "LINKLING_NO3P_PORT": str(_free_port()),
        "LINKLING_SMOKE_MAX_TIME": "2",
        "LINKLING_NO3P_MAX_TIME": "2",
    }
    yield tmp_path, env
    pid = state / "pid"
    if pid.exists():
        subprocess.run(["kill", pid.read_text().strip()], capture_output=True)


def _run(tree: Path, env: dict, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(tree / "scripts" / argv[0]), *argv[1:]],
        cwd=tree,
        env=env,
        capture_output=True,
        text=True,
        timeout=BOUND_SECONDS,
    )


def test_compose_smoke_answers_blind_when_the_service_is_silent(silent_stack):
    tree, env = silent_stack
    done = _run(tree, env, "compose-smoke.sh")
    assert done.returncode == 2, done.stderr
    assert done.stderr.startswith("blind:") or "\nblind:" in done.stderr
    assert "creating the canary link" in done.stderr
    assert "timed out" in done.stderr


def test_no_third_party_check_answers_blind_when_the_service_is_silent(silent_stack):
    tree, env = silent_stack
    done = _run(tree, env, "no-third-party-check.sh", "--api-only")
    assert done.returncode == 2, done.stderr
    assert "\nblind:" in "\n" + done.stderr
    assert "creating the link" in done.stderr
    assert "timed out" in done.stderr


@pytest.mark.parametrize(
    "script, variable",
    [
        ("compose-smoke.sh", "LINKLING_SMOKE_MAX_TIME"),
        ("no-third-party-check.sh", "LINKLING_NO3P_MAX_TIME"),
    ],
)
@pytest.mark.parametrize("bad", ["0", "00", "ten", "-1", "1.5"])
def test_a_limit_that_is_not_a_positive_whole_number_is_refused(silent_stack, script, variable, bad):
    """`curl --max-time 0` means no limit at all, which is the defect. Refuse it up front."""
    tree, env = silent_stack
    env[variable] = bad
    argv = [script, "--api-only"] if script.startswith("no-third") else [script]
    done = _run(tree, env, *argv)
    assert done.returncode in (2, 64), (done.returncode, done.stderr)
    assert variable in done.stderr
