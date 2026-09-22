"""Fixtures. Every test drives the real application over HTTP, never a handler directly.

``follow_redirects=False`` on the client is load-bearing rather than tidy: httpx follows
redirects by default, and a test that lets it do so cannot see how many hops there were or
what headers the hop carried -- which is most of what LL-001 and LL-014 are about.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from linkling.server.app import create_app
from linkling.server.config import Config

API_KEY = "test-team-key-7fqj2"
TARGET = "https://example.com/a/very/long/tracking/url?utm_source=slide&id=12|34~56"


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


@pytest.fixture
def target() -> str:
    """A long URL carrying characters `RedirectResponse` would have percent-encoded."""
    return TARGET
