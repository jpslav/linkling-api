"""Fixtures. Every test drives the real application over HTTP, never a handler directly.

``follow_redirects=False`` on the client is load-bearing rather than tidy: httpx follows
redirects by default, and a test that lets it do so cannot see how many hops there were or
what headers the hop carried -- which is most of what LL-001 and LL-014 are about.

The last two fixtures run the app under uvicorn on a loopback port and stand a second
loopback server in for the long URL. Nothing here binds a fixed port or starts a
container: both ask for port 0 and the kernel chooses, so two sessions can run this suite
at the same time.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import uvicorn
from fastapi.testclient import TestClient

from linkling.server.app import create_app
from linkling.server.config import Config

API_KEY = "test-team-key-7fqj2"
TARGET = "https://example.com/a/very/long/tracking/url?utm_source=slide&id=12|34~56"

STARTUP_TIMEOUT_SECONDS = 20
LONG_URL_BODY = b"arrived at the long url"


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "linkling.db"


@pytest.fixture
def config(db_path) -> Config:
    return Config(api_key=API_KEY, db_path=str(db_path))


@pytest.fixture
def app(config):
    return create_app(config)


@pytest.fixture
def client(app):
    with TestClient(app, follow_redirects=False) as test_client:
        yield test_client


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def target() -> str:
    """A long URL carrying characters `RedirectResponse` would have percent-encoded."""
    return TARGET


@pytest.fixture
def make_link(client, auth):
    """Create a link through the API and return the response body."""

    def _make(url: str = TARGET, name: str | None = None, **extra):
        body: dict[str, object] = {"url": url}
        if name is not None:
            body["name"] = name
        body.update(extra)
        response = client.post("/-/api/links", json=body, headers=auth)
        assert response.status_code == 201, response.text
        return response.json()

    return _make


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
