"""LL-028 -- the site half of the no-third-party check runs in CI against a stand-in site.

CI's job `no-third-party-site` runs the whole check against tests/fixtures/standin-site, and then
scripts/no-third-party-standin.sh three times, each with a deliberate defect in the stand-in that
the check must catch. All of that needs Docker. What does not is pinned here, in the fast job:

- the stand-in serves what each of its modes says it does, so a fixture cannot go red, or stay
  green, for a reason other than the one it names (the cut-short reply really is cut short, the
  stalled one really is held open), and the server resolves no name (the check fails any DNS
  query it captures);
- the stand-in's base image is pinned by the same digest as the service's (ADR-0017);
- every defect mode of the stand-in (all but `none`, the clean site, which is the job's first
  step) has a fixture in the script and a step in ci.yml, and the site job runs the check without
  --api-only. Each comparison is between sets read out of a file, and an empty read must not look
  like agreement, so each one is asserted against the set it must be;
- the script judges the check's exit status AND its last line, on a stand-in check that ends as
  told: it passes when the check ends the way the fixture requires, and not when the check says
  `pass`, ends red in another way, or is blind for a reason that is not the fixture's.
"""

from __future__ import annotations

import http.client
import http.server
import importlib.util
import os
import re
import shutil
import socket
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STANDIN = ROOT / "tests" / "fixtures" / "standin-site"
SCRIPT = ROOT / "scripts" / "no-third-party-standin.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"

MUTATIONS = {"external-stylesheet", "cut-short-reply", "stalled-reply"}
MODES = MUTATIONS | {"none"}


def _load_server():
    spec = importlib.util.spec_from_file_location("standin_server", STANDIN / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


server_module = _load_server()


@contextmanager
def serving(mode: str):
    server = server_module.make_server(mode, ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _get(port: int, path: str, timeout: float = 5):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    conn.request("GET", path)
    return conn, conn.getresponse()


def _body(port: int, path: str) -> tuple[int, str, bytes]:
    conn, resp = _get(port, path)
    try:
        return resp.status, resp.getheader("Content-Type"), resp.read()
    finally:
        conn.close()


# --- what the stand-in serves ------------------------------------------------------------------


def test_the_clean_stand_in_serves_what_the_crawl_needs():
    with serving("none") as port:
        status, ctype, home = _body(port, "/")
        assert (status, ctype.split(";")[0]) == (200, "text/html")
        assert b'href="/style.css"' in home and b'href="/second.css"' in home
        assert b'href="/privacy.html"' in home
        status, ctype, privacy = _body(port, "/privacy.html")
        assert (status, ctype.split(";")[0]) == (200, "text/html")
        for sheet in ("/style.css", "/second.css"):
            status, ctype, css = _body(port, sheet)
            assert (status, ctype.split(";")[0]) == (200, "text/css"), sheet
            assert css, sheet
        assert _body(port, "/nope")[0] == 404
        # Nothing absolute, or protocol-relative, in a page: the check fails on either.
        for page in (home, privacy):
            assert b"://" not in page and b'"//' not in page


@pytest.mark.parametrize("mode", sorted(MODES))
def test_only_the_external_stylesheet_mode_links_another_origin(mode):
    with serving(mode) as port:
        _, _, home = _body(port, "/")
        others = [_body(port, p)[2] for p in ("/privacy.html", "/style.css")]
    linked = server_module.EXTERNAL_STYLESHEET.encode() in home
    assert linked == (mode == "external-stylesheet")
    assert server_module.EXTERNAL_STYLESHEET.startswith("https://") and ".invalid/" in server_module.EXTERNAL_STYLESHEET
    assert all(b"invalid" not in page for page in others)


@pytest.mark.parametrize("mode", ["none", "external-stylesheet"])
def test_the_stylesheet_arrives_whole_unless_a_mode_cuts_it_short(mode):
    with serving(mode) as port:
        assert _body(port, "/second.css")[2] == b"main{margin:0}"


def test_the_cut_short_reply_promises_more_than_it_sends_and_closes():
    with serving("cut-short-reply") as port:
        conn, resp = _get(port, "/second.css")
        assert resp.status == 200
        assert int(resp.getheader("Content-Length")) == len(b"main{margin:0}") + server_module.SHORT_BY
        with pytest.raises(http.client.IncompleteRead) as cut:
            resp.read()
        assert cut.value.partial == b"main{margin:0}"
        conn.close()
        # Only that one stylesheet: the pages and the other stylesheet arrive whole.
        for path in ("/", "/privacy.html", "/style.css"):
            assert _body(port, path)[0] == 200


def test_the_stalled_reply_sends_its_headers_and_then_holds_the_connection_open():
    with serving("stalled-reply") as port:
        # The headers get `_get`'s 5 s, so a slow runner is unlikely to end this before it starts;
        # only the read of the body that never comes is bounded tightly.
        conn, resp = _get(port, "/second.css")
        assert resp.status == 200
        conn.sock.settimeout(1)
        with pytest.raises(TimeoutError):
            resp.read()
        conn.close()
        for path in ("/", "/privacy.html", "/style.css"):
            assert _body(port, path)[0] == 200


def test_the_server_resolves_no_name(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a name was looked up")

    monkeypatch.setattr(socket, "getfqdn", refuse)
    monkeypatch.setattr(socket, "gethostbyaddr", refuse)
    # The control: with the lookups refused, the stock server does not even bind, so this test
    # is wired to the thing it names, not to an unused double.
    with pytest.raises(AssertionError, match="a name was looked up"):
        http.server.ThreadingHTTPServer(("127.0.0.1", 0), server_module.handler_for("none"))
    with serving("none") as port:
        assert _body(port, "/")[0] == 200


def test_an_unknown_mode_is_refused_rather_than_served_clean():
    with pytest.raises(ValueError, match="unknown mode"):
        server_module.handler_for("cut-shortt-reply")


def test_the_checked_in_mode_is_the_clean_one():
    assert (STANDIN / "mode").read_text().strip() == "none"


# --- the pin, and the coverage ------------------------------------------------------------------

DIGEST = re.compile(r"^FROM python:3\.12-slim@(sha256:[0-9a-f]{64})[ \t]*$", re.M)


def test_the_stand_in_base_image_is_pinned_by_the_services_digest():
    service = DIGEST.findall((ROOT / "Dockerfile").read_text())
    standin = DIGEST.findall((STANDIN / "Dockerfile").read_text())
    # One digest on each side: a FROM line that moved or changed shape reads as none, not as equal.
    assert len(service) == 1, "the service's Dockerfile has no digest-pinned python:3.12-slim FROM"
    assert len(standin) == 1, "the stand-in's Dockerfile has no digest-pinned python:3.12-slim FROM"
    assert standin == service


def test_every_mode_has_a_fixture_in_the_script_and_a_step_in_ci():
    assert set(server_module.MODES) == MODES, "server.py's modes are not the four this test names"
    arms = re.findall(r"^    ([a-z]+(?:-[a-z]+)*)\)$", SCRIPT.read_text(), re.M)
    assert sorted(arms) == sorted(MUTATIONS), "the script's fixtures are not server.py's mutations"
    steps = re.findall(r"scripts/no-third-party-standin\.sh (\S+)", _site_job())
    assert sorted(steps) == sorted(MUTATIONS), "ci.yml's site job does not run each fixture exactly once"


def _site_job() -> str:
    found = re.search(
        r"^  no-third-party-site:\n(.*?)(?=^  [A-Za-z0-9_-]+:\n|\Z)", CI.read_text(), re.M | re.S
    )
    assert found, "ci.yml has no job `no-third-party-site`"
    return found.group(1)


def test_the_site_job_runs_the_whole_check_against_the_stand_in():
    runs = re.findall(r"^\s+run: (.*)$", _site_job(), re.M)
    assert runs, "the site job has no `run:` lines"
    assert not [r for r in runs if "--api-only" in r], "the site job must not run --api-only"
    assert "LINKLING_WEB_DIR=tests/fixtures/standin-site scripts/no-third-party-check.sh" in runs


# --- the script, against a check that ends as told ---------------------------------------------

FAKE_CHECK = """#!/usr/bin/env bash
# Records what it was given, then ends as told.
{
    echo "args: $*"
    echo "mode: $(cat "$LINKLING_WEB_DIR/mode")"
    echo "has_server: $([ -f "$LINKLING_WEB_DIR/server.py" ] && echo yes || echo no)"
    echo "max_time: ${LINKLING_NO3P_MAX_TIME:-unset}"
} >"$FAKE_RECORD"
printf '%s\\n' "$FAKE_OUTPUT" >&2
exit "$FAKE_STATUS"
"""

EXTERNAL_RED = "fail: web serves / with a third-party reference: in its body https://styles.standin.invalid/site.css"
CUT_RED = (
    "blind: the site's /second.css answered '200' but its reply did not arrive whole "
    "(the reply stopped before its end), so what it serves there was not seen"
)
STALL_RED = (
    "blind: the site's /second.css answered '200' but its reply did not arrive whole "
    "(curl timed out after 5s), so what it serves there was not seen"
)


@pytest.fixture
def tree(tmp_path):
    """A copy of the script and the stand-in, with a fake check where the real one would be."""
    (tmp_path / "scripts").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    (tmp_path / "scripts" / "no-third-party-check.sh").write_text(FAKE_CHECK)
    shutil.copytree(STANDIN, tmp_path / "tests" / "fixtures" / "standin-site")
    return tmp_path


def _run(tree: Path, fixture: str | None, output: str, status: int):
    record = tree / "record.txt"
    env = {**os.environ, "FAKE_OUTPUT": output, "FAKE_STATUS": str(status), "FAKE_RECORD": str(record)}
    env.pop("LINKLING_NO3P_MAX_TIME", None)
    argv = ["bash", str(tree / "scripts" / SCRIPT.name)] + ([fixture] if fixture else [])
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
    return done, (record.read_text() if record.exists() else "")


@pytest.mark.parametrize(
    "fixture, output, status",
    [
        ("external-stylesheet", "site fetched: /\n" + EXTERNAL_RED, 1),
        ("cut-short-reply", "site fetched: /\n" + CUT_RED, 2),
        ("stalled-reply", "site fetched: /\n" + STALL_RED, 2),
    ],
)
def test_the_script_passes_when_the_check_ends_the_way_the_fixture_requires(tree, fixture, output, status):
    done, record = _run(tree, fixture, output, status)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "pass:" in done.stdout
    # The whole check, with nothing narrowing it, against the stand-in in the fixture's mode.
    assert "args: \n" in record
    assert f"mode: {fixture}\n" in record and "has_server: yes\n" in record


def test_the_time_bound_is_forced_for_the_stalled_fixture_only(tree):
    _, record = _run(tree, "stalled-reply", "x\n" + STALL_RED, 2)
    assert "max_time: 5\n" in record
    _, record = _run(tree, "cut-short-reply", "x\n" + CUT_RED, 2)
    assert "max_time: unset\n" in record


@pytest.mark.parametrize(
    "fixture, output, status, expected_exit",
    [
        # The check went green: the mutation was not caught (for the cut-short reply, that is the
        # defect LL-025 fixed).
        ("external-stylesheet", "pass", 0, 1),
        ("cut-short-reply", "pass", 0, 1),
        ("stalled-reply", "pass", 0, 1),
        # Red, but not this fixture's red.
        ("external-stylesheet", "fail: api sent a packet that is not a reply", 1, 1),
        ("cut-short-reply", "fail: web serves / with a third-party reference: in its body http://x", 1, 1),
        # `fail` where `blind` is required: the crawl saw something instead of missing it.
        ("cut-short-reply", "fail: " + CUT_RED.removeprefix("blind: "), 1, 1),
        # Blind, for a reason the fixture is not about: it looked at nothing, so it proved nothing.
        ("cut-short-reply", "blind: docker is not on PATH", 2, 2),
        ("stalled-reply", "blind: the stack never came up healthy, so nothing was exercised", 2, 2),
        ("external-stylesheet", "blind: docker is not on PATH", 2, 2),
        # The other blind that the crawl can end on is not the fixture's either.
        ("cut-short-reply", CUT_RED.replace("stopped before its end", "timed out after 10s").replace("the reply ", "curl "), 2, 2),
        # Right message, wrong status: the message alone is not the verdict.
        ("cut-short-reply", CUT_RED, 1, 1),
        ("external-stylesheet", EXTERNAL_RED, 2, 2),
        # A usage error from the check is not a red either.
        ("external-stylesheet", "usage: scripts/no-third-party-check.sh", 64, 1),
    ],
)
def test_the_script_is_not_satisfied_by_any_other_way_of_ending(tree, fixture, output, status, expected_exit):
    done, _ = _run(tree, fixture, "x\n" + output, status)
    assert done.returncode == expected_exit, done.stdout + done.stderr
    assert "pass:" not in done.stdout
    assert done.stderr.startswith(("fail:", "blind:")) or "\nfail:" in done.stderr or "\nblind:" in done.stderr


@pytest.mark.parametrize("argv", [None, "external-stylesheet extra", "none", "cut-short"])
def test_the_script_refuses_anything_but_one_fixture_name(tree, argv):
    record = tree / "record.txt"
    env = {**os.environ, "FAKE_OUTPUT": "", "FAKE_STATUS": "0", "FAKE_RECORD": str(record)}
    args = [] if argv is None else argv.split()
    done = subprocess.run(
        ["bash", str(tree / "scripts" / SCRIPT.name), *args], env=env, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 64, done.stdout + done.stderr
    assert not record.exists(), "the check ran on a usage error"


def test_the_script_is_blind_when_it_cannot_make_its_temporary_copy(tree, tmp_path):
    # A `mktemp` that fails: an unusable TMPDIR is not portable for this, since macOS's falls back.
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "mktemp").write_text("#!/bin/sh\nexit 1\n")
    (shim / "mktemp").chmod(0o755)
    record = tree / "record.txt"
    env = {
        **os.environ,
        "FAKE_OUTPUT": "",
        "FAKE_STATUS": "0",
        "FAKE_RECORD": str(record),
        "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}",
    }
    done = subprocess.run(
        ["bash", str(tree / "scripts" / SCRIPT.name), "cut-short-reply"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "blind: could not make a temporary directory" in done.stderr
    assert not record.exists(), "the check ran with no mutated stand-in"


def test_the_script_is_blind_when_there_is_no_stand_in_to_mutate(tree):
    shutil.rmtree(tree / "tests" / "fixtures" / "standin-site")
    done, record = _run(tree, "cut-short-reply", "x\n" + CUT_RED, 2)
    assert done.returncode == 2 and "blind: no stand-in site" in done.stderr
    assert record == "", "the check ran with nothing to check"
