"""LL-004 -- a request carrying identifying headers leaves no trace of them.

The service runs here as a **separate process**, started with the flags of the README's
"Run it locally" command and `--port 0` so the kernel picks the port. Its `cwd`, `HOME`,
`TMPDIR` and `LINKLING_DB` are all inside a fresh temporary directory, and its stdout and
stderr are each captured to a file. A link is followed once with an `X-Forwarded-For`, a
`User-Agent`, a `Referer` and a cookie, each carrying a marker unique to this run. Then,
in this order, and only in this order:

1. **Arrival.** The `sqlite3` CLI, from outside the process, shows today's count at 1.
2. **The right, current database.** Its `.dump` contains the link *and* that count.
3. **The capture works.** Captured stderr holds uvicorn's `Uvicorn running on` line.
4. **Only then, the search.** No marker appears in the dump, in the raw bytes of any file
   under the temporary directory (the database and its `-wal`/`-shm`, freed pages
   included), or in the captured stdout or stderr.

Pass is a green test. Fail is an assertion naming the marker and where it was found.
**Blind is a failure whose message begins ``NO-TRACE BLIND:``** -- steps 1-3 failing, the
`sqlite3` CLI being absent, or the file walk not seeing the database -- because an empty
search passes exactly as a clean one does.

``test_the_scanner_finds_the_address_when_the_access_log_is_on`` is the permanent positive
control: the identical run without ``--no-access-log`` must *find* the forwarded address,
which is the leak ADR-0004c names. It is what proves the scanner reads the output it
claims to.

Stated limit: this sees what the service writes under its own `cwd`, `HOME`, `TMPDIR` and
database directory, and its stdout and stderr. A write to another absolute path would not
be seen. The container's log driver and any reverse proxy are LL-005's and ADR-0007's.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from linkling.server import counts

API_KEY = "no-trace-team-key-5c1d"
STARTUP_TIMEOUT_SECONDS = 20
_RUNNING = re.compile(r"Uvicorn running on http://127\.0\.0\.1:(\d+)")


def _blind(reason: str) -> None:
    pytest.fail(f"NO-TRACE BLIND: {reason}", pytrace=False)


@dataclass
class Observation:
    root: Path
    db: Path
    stdout: str
    stderr: str
    markers: dict[str, str]


def _sqlite3_cli() -> str:
    found = shutil.which("sqlite3")
    if found is None:
        _blind("no sqlite3 CLI on PATH -- nothing could be read from outside the process")
    return found


def _sqlite3(db: Path, sql: str) -> str:
    result = subprocess.run(
        [_sqlite3_cli(), str(db), sql], capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        _blind(f"sqlite3 could not read {db}: {result.stderr.strip()}")
    return result.stdout


def _follow_once_with_identifying_headers(tmp_path: Path, access_log: bool) -> Observation:
    """Start the service, make a link, follow it once carrying the markers, stop it."""
    root = tmp_path / "service"
    home, tmp, data, capture = (root / "home", root / "tmp", root / "data", root / "capture")
    for directory in (home, tmp, data, capture):
        directory.mkdir(parents=True)
    db = data / "linkling.db"

    tag = secrets.token_hex(6)
    markers = {
        "X-Forwarded-For": f"203.0.113.{secrets.randbelow(254) + 1}",
        "User-Agent": f"ua-canary-{tag}",
        "Referer": f"https://ref-canary-{tag}.example/",
        "Cookie": f"sid=cookie-canary-{tag}",
    }

    command = [
        sys.executable, "-m", "uvicorn", "--factory", "linkling.server.app:create_app",
        "--host", "127.0.0.1", "--port", "0",
    ]
    if not access_log:
        command.append("--no-access-log")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "LINKLING_API_KEY": API_KEY,
        "LINKLING_DB": str(db),
        # Every write reaches its capture file as it is made. Without this a `print` sits
        # in a block buffer that is never flushed: uvicorn re-raises the SIGTERM it caught
        # once its shutdown finishes (`uvicorn/server.py`, `signal.raise_signal`), so the
        # process dies by the signal and a leak in that buffer was measured passing.
        "PYTHONUNBUFFERED": "1",
    }
    stdout_path, stderr_path = capture / "stdout.log", capture / "stderr.log"
    with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
        process = subprocess.Popen(command, cwd=root, env=env, stdout=out, stderr=err)
    try:
        port = None
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while port is None:
            match = _RUNNING.search(stderr_path.read_text(errors="replace"))
            if match:
                port = int(match.group(1))
            elif process.poll() is not None or time.monotonic() > deadline:
                _blind(
                    "the service did not start -- nothing below was examined. stderr: "
                    + stderr_path.read_text(errors="replace")[-2000:]
                )
            else:
                time.sleep(0.05)

        base = f"http://127.0.0.1:{port}"
        created = httpx.post(
            f"{base}/-/api/links",
            json={"url": "https://example.com/landing", "name": "q3-plan"},
            headers={"Authorization": f"Bearer {API_KEY}"},
            timeout=10,
        )
        assert created.status_code == 201, created.text
        followed = httpx.get(
            f"{base}/q3-plan", headers=markers, follow_redirects=False, timeout=10
        )
        assert followed.status_code == 302, followed.text
    finally:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    return Observation(
        root=root,
        db=db,
        stdout=stdout_path.read_text(errors="replace"),
        stderr=stderr_path.read_text(errors="replace"),
        markers=markers,
    )


def _establish_the_request_arrived(seen: Observation) -> str:
    """Steps 1-3. Returns the dump. Any failure here is blind, never a pass."""
    today = counts.utc_day(time.time())
    arrived = _sqlite3(
        seen.db,
        "SELECT daily_counts.count FROM daily_counts JOIN links "
        f"ON links.id = daily_counts.link_id WHERE links.name = 'q3-plan' AND day = '{today}'",
    ).strip()
    if arrived != "1":
        _blind(
            f"the follow did not arrive (count read {arrived!r}, expected '1') -- "
            "nothing below was examined"
        )

    dump = _sqlite3(seen.db, ".dump")
    if "q3-plan" not in dump or not re.search(rf"'{today}',1\)", dump):
        _blind("dump is not the service's database -- it lacks the link or its count")

    if "Uvicorn running on" not in seen.stderr:
        _blind("no log lines captured")
    return dump


def _where_markers_appear(seen: Observation, dump: str) -> list[tuple[str, str]]:
    """Every (header, place) where a marker was found. Also blind-checks the file walk."""
    found: list[tuple[str, str]] = []
    places: dict[str, bytes] = {"sqlite3 .dump": dump.encode()}
    walked_bytes = 0
    for path in sorted(seen.root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            walked_bytes += len(data)
            places[str(path.relative_to(seen.root))] = data
    if str(seen.db.relative_to(seen.root)) not in places:
        _blind("the file walk did not see the database")
    print(
        f"no-trace: examined {len(places) - 1} files ({walked_bytes} bytes) under "
        f"{seen.root} plus the sqlite3 dump ({len(dump)} chars)"
    )
    for header, marker in seen.markers.items():
        for place, data in places.items():
            if marker.encode() in data:
                found.append((header, place))
    return found


def test_identifying_headers_leave_no_trace(tmp_path):
    seen = _follow_once_with_identifying_headers(tmp_path, access_log=False)
    dump = _establish_the_request_arrived(seen)
    found = _where_markers_appear(seen, dump)
    assert found == [], f"identifying header values were written: {found}"


def test_the_scanner_finds_the_address_when_the_access_log_is_on(tmp_path):
    """The positive control: uvicorn's default access log prints the forwarded address."""
    seen = _follow_once_with_identifying_headers(tmp_path, access_log=True)
    dump = _establish_the_request_arrived(seen)
    found = _where_markers_appear(seen, dump)
    assert ("X-Forwarded-For", "capture/stdout.log") in found, (
        f"the scanner did not find the address the access log writes: {found}; stdout was "
        f"{seen.stdout[-1000:]!r}"
    )
