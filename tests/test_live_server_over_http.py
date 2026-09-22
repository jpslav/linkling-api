"""The same promises, over a real socket, with a real HTTP client.

`TestClient` speaks ASGI in-process. This one runs the app under uvicorn on a loopback
port and follows the redirect to a second loopback server standing in for the long URL, so
"one hop" is counted by the client rather than asserted about a response object. It is the
HTTP half of LL-014; the real-browser-cache half is the manager's, and `README.md` says
how to run the service for it.

No Docker and no fixed ports: both servers bind port 0 and the kernel chooses, so nothing
here can collide with another session's work.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import uvicorn

STARTUP_TIMEOUT_SECONDS = 20
LONG_URL_BODY = b"arrived at the long url"


class _LongUrlHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - http.server's spelling
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(LONG_URL_BODY)))
        self.end_headers()
        self.wfile.write(LONG_URL_BODY)

    def log_message(self, *args):
        """Silence: this stand-in must not print client addresses either (ADR-0004c)."""


@pytest.fixture
def long_url_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LongUrlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/landing/page"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def live_base_url(app):
    """The app under uvicorn, with the access log off, on a port the kernel picks."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning", access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            server.should_exit = True
            thread.join(timeout=5)
            pytest.fail(
                f"blind: uvicorn did not start within {STARTUP_TIMEOUT_SECONDS}s -- "
                "nothing below was measured"
            )
        time.sleep(0.05)

    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_a_real_client_reaches_the_long_url_in_exactly_one_hop(
    live_base_url, long_url_server, auth
):
    created = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": long_url_server, "name": "q3-plan"},
        headers=auth,
        timeout=10,
    )
    assert created.status_code == 201, created.text

    # No Authorization header and no cookie jar: this is the clicker, not the team.
    with httpx.Client(follow_redirects=True, timeout=10) as client:
        arrived = client.get(f"{live_base_url}/q3-plan")

    assert len(arrived.history) == 1, [r.status_code for r in arrived.history]
    hop = arrived.history[0]
    assert hop.status_code == 302
    assert hop.headers["location"] == long_url_server
    assert hop.headers["cache-control"] == "no-store"
    assert "set-cookie" not in hop.headers
    assert arrived.status_code == 200
    assert arrived.content == LONG_URL_BODY


def test_a_deleted_link_is_gone_over_the_wire_too(live_base_url, long_url_server, auth):
    created = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": long_url_server, "name": "q3-plan"},
        headers=auth,
        timeout=10,
    )
    assert created.status_code == 201, created.text

    deleted = httpx.delete(
        f"{live_base_url}/-/api/links/q3-plan", headers=auth, timeout=10
    )
    assert deleted.status_code == 204, deleted.text

    gone = httpx.get(f"{live_base_url}/q3-plan", follow_redirects=False, timeout=10)
    assert gone.status_code == 410
    assert gone.headers["cache-control"] == "no-store"
    assert "location" not in gone.headers


def test_a_keyless_write_is_refused_over_the_wire(live_base_url, long_url_server):
    refused = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": long_url_server, "name": "sneak"},
        timeout=10,
    )
    assert refused.status_code == 401, refused.text
    assert httpx.get(f"{live_base_url}/sneak", timeout=10).status_code == 404
