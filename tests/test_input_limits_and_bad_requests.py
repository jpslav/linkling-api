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


@pytest.mark.parametrize(
    "key",
    [
        "clé-d-équipe",  # httpx will not encode a non-ASCII header value
        "team\nkey",  # h11 refuses to build a header carrying a newline
        "team\rkey",
    ],
)
def test_a_key_no_client_could_send_is_refused_at_startup(tmp_path, key):
    """A key that cannot go in a header is a service that refuses every write.

    It would look perfectly healthy while doing it, which is why this is a startup
    failure rather than a 401 nobody can explain.
    """
    from linkling.server.config import ConfigError, load_config

    with pytest.raises(ConfigError, match="printable ASCII"):
        load_config(
            {"LINKLING_API_KEY": key, "LINKLING_DB": str(tmp_path / "x.db")}
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


def test_a_chunked_body_over_the_ceiling_is_refused_whenever_the_excess_arrives(
    client, auth
):
    """A chunked body declares no length, so it is counted as it arrives.

    Cutting it off and letting the request through made the answer depend on packet
    timing: the same over-limit request was a 422 when the excess arrived in the first
    chunk and a **201**, with the link created, when it arrived after the handler had
    already read a valid JSON prefix. Both spellings below must be refused.
    """
    head = '{"url": "https://example.com/z", "name": "chunky"}'

    def excess_after_valid_json():
        yield head.encode()
        yield b" " * 100000

    late = client.post(
        "/-/api/links",
        content=excess_after_valid_json(),
        headers={**auth, "Content-Type": "application/json"},
    )
    assert late.status_code == 413, late.status_code
    assert client.get("/chunky").status_code == 404, "an over-limit request made a link"

    def excess_first():
        yield b" " * 100000
        yield head.encode()

    early = client.post(
        "/-/api/links",
        content=excess_first(),
        headers={**auth, "Content-Type": "application/json"},
    )
    assert early.status_code == 413, early.status_code
    assert client.get("/chunky").status_code == 404


def test_a_target_at_the_length_limit_is_still_accepted(client, auth):
    """The cap refuses what is over it, and nothing under it."""
    from linkling.server.app import _MAX_TARGET_LENGTH

    prefix = "https://example.com/"
    exact = prefix + "a" * (_MAX_TARGET_LENGTH - len(prefix))
    assert len(exact) == _MAX_TARGET_LENGTH

    accepted = client.post(
        "/-/api/links", json={"url": exact, "name": "at-the-limit"}, headers=auth
    )
    assert accepted.status_code == 201, accepted.text
    assert client.get("/at-the-limit").headers["location"] == exact

    refused = client.post(
        "/-/api/links", json={"url": exact + "a", "name": "over-the-limit"}, headers=auth
    )
    assert refused.status_code == 422, refused.status_code


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
