"""What the service does with requests that are hostile, malformed or merely enormous.

Every case here was a 500, an unbounded read or a silent acceptance before it was a test.
A 500 matters beyond tidiness: it is the shape in which an unauthenticated caller learns
that something inside the service crashed, and on the redirect path it is a link that
stops working for a reason no log of ours explains.
"""

from __future__ import annotations

import pytest


def test_a_non_ascii_bearer_token_is_refused_not_a_crash(client, target):
    """`hmac.compare_digest` on `str` raises for non-ASCII; headers decode as latin-1."""
    response = client.post(
        "/-/api/links",
        json={"url": target, "name": "q3-plan"},
        headers={"Authorization": b"Bearer caf\xe9"},
    )
    assert response.status_code == 401, response.text
    assert client.get("/q3-plan").status_code == 404


def test_a_non_ascii_configured_key_is_refused_at_startup(tmp_path):
    """The mirror of the test above, and the reason it is a startup failure.

    An HTTP header value cannot carry a non-ASCII character -- httpx raises
    `UnicodeEncodeError` rather than send one -- so a service configured with such a key
    would refuse every write while looking perfectly healthy. Better to not start.
    """
    from linkling.server.config import ConfigError, load_config

    with pytest.raises(ConfigError, match="must be ASCII"):
        load_config(
            {"LINKLING_API_KEY": "clé-d-équipe", "LINKLING_DB": str(tmp_path / "x.db")}
        )


@pytest.mark.parametrize("url", ["http://[", "http://[::1", "http://[::1]x/", "http://a]b/"])
def test_an_unparseable_target_is_refused_not_a_crash(client, auth, url):
    """`urlsplit` raises ValueError on a malformed IPv6 host; that must not be a 500."""
    response = client.post(
        "/-/api/links", json={"url": url, "name": "bad-host"}, headers=auth
    )
    assert response.status_code == 422, (url, response.status_code, response.text)
    assert client.get("/bad-host").status_code == 404


def test_a_valid_ipv6_target_is_still_accepted(client, auth):
    response = client.post(
        "/-/api/links", json={"url": "https://[::1]:8443/x", "name": "v6"}, headers=auth
    )
    assert response.status_code == 201, response.text
    assert client.get("/v6").headers["location"] == "https://[::1]:8443/x"


def test_an_enormous_target_is_refused(client, auth):
    """Stored, a 1 MB target is a `Location` no client or proxy will carry."""
    response = client.post(
        "/-/api/links",
        json={"url": "https://example.com/" + "a" * 20000, "name": "enormous"},
        headers=auth,
    )
    assert response.status_code == 422, response.status_code
    assert client.get("/enormous").status_code == 404


def test_an_enormous_body_is_refused_before_it_is_read(client, auth):
    """And refused whether or not the caller holds the key, since nothing has read it."""
    huge = '{"url": "https://example.com/", "name": "x", "created_by": "' + "a" * 200000 + '"}'

    with_key = client.post(
        "/-/api/links",
        content=huge,
        headers={**auth, "Content-Type": "application/json"},
    )
    assert with_key.status_code == 413, with_key.status_code

    keyless = client.post(
        "/-/api/links", content=huge, headers={"Content-Type": "application/json"}
    )
    assert keyless.status_code == 413, keyless.status_code
    assert keyless.headers["cache-control"] == "no-store"


def test_created_by_rejects_control_characters(client, auth, target):
    response = client.post(
        "/-/api/links",
        json={"url": target, "name": "q3-plan", "created_by": "jp\r\nX-Injected: 1"},
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert client.get("/q3-plan").status_code == 404


def test_created_by_is_recorded_when_it_is_ordinary(client, auth, target, db_path):
    import sqlite3

    created = client.post(
        "/-/api/links",
        json={"url": target, "name": "q3-plan", "created_by": "jp"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            "SELECT created_by FROM links WHERE name = 'q3-plan'"
        ).fetchone()[0]
    assert stored == "jp"


def test_a_doubled_trailing_slash_is_not_a_link_and_does_not_bounce(client, make_link):
    """Starlette's slash redirect built a second hop out of the `Host` header."""
    make_link(name="q3-plan")
    response = client.get("/q3-plan//", headers={"Host": "evil.example"})
    assert response.status_code == 404, response.status_code
    assert "location" not in response.headers
    assert response.headers["cache-control"] == "no-store"
