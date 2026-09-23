"""LL-001, and LL-014's HTTP half -- one hop, the agreed status and headers, and gone
when deleted.

ADR-0003's record settles the three statuses and puts ``Cache-Control: no-store`` on all
of them. The reason the header assertions are here rather than left to a reviewer's eye:
the entire point of that ADR is that a browser which cached a redirect never asks again,
so a deletion nobody's browser hears about is a broken promise rather than a stale cache.
"""

from __future__ import annotations

import pytest

NO_STORE = "no-store"


def test_following_a_link_is_one_hop_to_the_long_url(client, make_link, target):
    make_link(name="q3-plan")

    response = client.get("/q3-plan")

    assert response.status_code == 302
    assert response.headers["location"] == target
    assert response.headers["cache-control"] == NO_STORE
    assert response.history == [], "more than one response arrived"
    assert response.content == b"", "the short URL served a body"
    assert b"<html" not in response.content.lower()


def test_the_location_is_the_target_byte_for_byte(client, auth):
    """`RedirectResponse` would have percent-encoded this; ADR-0005 stores it byte-exact."""
    exact = "https://example.com/p|q~r?a=1&b=2#frag"
    created = client.post(
        "/-/api/links", json={"url": exact, "name": "byte-exact"}, headers=auth
    )
    assert created.status_code == 201, created.text
    assert created.json()["url"] == exact

    response = client.get("/byte-exact")
    assert response.headers["location"] == exact


def test_a_head_request_gets_the_same_redirect(client, make_link, target):
    make_link(name="q3-plan")
    response = client.head("/q3-plan")
    assert response.status_code == 302
    assert response.headers["location"] == target
    assert response.headers["cache-control"] == NO_STORE


def test_a_trailing_slash_is_the_same_link_and_still_one_hop(client, make_link, target):
    make_link(name="q3-plan")
    response = client.get("/q3-plan/")
    assert response.status_code == 302, "a trailing slash bounced through another hop"
    assert response.headers["location"] == target
    assert response.headers["cache-control"] == NO_STORE


def test_a_query_string_on_the_short_link_is_dropped(client, make_link, target):
    make_link(name="q3-plan")
    response = client.get("/q3-plan?utm_campaign=poster")
    assert response.status_code == 302
    assert response.headers["location"] == target


def test_an_unknown_name_is_404_and_uncacheable(client):
    response = client.get("/never-made")
    assert response.status_code == 404
    assert response.headers["cache-control"] == NO_STORE
    assert "location" not in response.headers


def test_a_deleted_link_stops_resolving_and_says_gone(client, auth, make_link):
    make_link(name="q3-plan")
    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204

    response = client.get("/q3-plan")
    assert response.status_code == 410
    assert response.headers["cache-control"] == NO_STORE
    assert "location" not in response.headers


@pytest.mark.parametrize("path", ["/a/b", "/never-made", "/-/api/nothing-here"])
def test_every_404_the_router_makes_is_uncacheable(client, path):
    """These never reach a handler of ours, and RFC 9110 makes 404 heuristically cacheable."""
    response = client.get(path)
    assert response.status_code == 404
    assert response.headers["cache-control"] == NO_STORE


def test_a_405_is_uncacheable_too(client, make_link):
    make_link(name="q3-plan")
    response = client.post("/q3-plan")
    assert response.status_code == 405
    assert response.headers["cache-control"] == NO_STORE


def test_creating_with_a_chosen_name_returns_that_name(client, auth, target):
    created = client.post(
        "/-/api/links", json={"url": target, "name": "q3-plan"}, headers=auth
    )
    assert created.status_code == 201, created.text
    assert created.json() == {"name": "q3-plan", "url": target}


def test_creating_without_a_name_returns_a_generated_one(client, auth, target):
    created = client.post("/-/api/links", json={"url": target}, headers=auth)
    assert created.status_code == 201, created.text
    body = created.json()
    assert isinstance(body["name"], str) and body["name"], body


def test_a_name_that_is_taken_is_refused(client, auth, make_link, target):
    make_link(name="q3-plan")
    again = client.post(
        "/-/api/links", json={"url": target, "name": "q3-plan"}, headers=auth
    )
    assert again.status_code == 409, again.text


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "ftp://example.com/x",
        "/relative/path",
        "https://",
        "http://exa mple.com/",
        "https://example.com/\r\nX-Injected: 1",
        "https://example.com/café",
        "",
    ],
)
def test_a_target_that_is_not_an_absolute_ascii_http_url_is_refused(client, auth, url):
    response = client.post(
        "/-/api/links", json={"url": url, "name": "bad-target"}, headers=auth
    )
    assert response.status_code == 422, (url, response.status_code, response.text)
    assert client.get("/bad-target").status_code == 404


def test_an_unknown_field_is_refused_rather_than_ignored(client, auth, target):
    """ADR-0011b: an unrecognised field is a 422, never silently dropped."""
    response = client.post(
        "/-/api/links",
        json={"url": target, "name": "q3-plan", "not_a_real_field": "x"},
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert client.get("/q3-plan").status_code == 404
