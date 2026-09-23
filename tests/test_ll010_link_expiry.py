"""LL-010 / R-006 -- a link given an expiry stops resolving once it passes.

The clock is driven, not slept past: every test here monkeypatches
``linkling.server.links._now`` -- the one function both link creation and the follow
path's expiry check read the current instant from -- to a fixed ISO-8601 UTC string, so
"time passing" is a reassignment, not a `time.sleep`.

R-006's own blind line is why the core test below asserts both a live redirect *and* the
`410` that follows it in the same test, rather than the `410` alone: a lone post-expiry
assertion cannot tell "expiry works" from "the link was never made, or the clock never
moved" -- see the report for the worked example.
"""

from __future__ import annotations

import sqlite3

import pytest

from linkling.server import links as links_module

NO_STORE = "no-store"


def test_a_link_with_an_expiry_redirects_before_it_and_is_410_after(
    client, auth, make_link, target, monkeypatch
):
    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T12:00:00Z")
    make_link(name="q3-plan", expires="2026-01-01T12:00:10Z")

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T12:00:09Z")
    before = client.get("/q3-plan")
    assert before.status_code == 302, before.text
    assert before.headers["location"] == target
    assert before.headers["cache-control"] == NO_STORE

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T12:00:11Z")
    after = client.get("/q3-plan")
    assert after.status_code == 410, after.text
    assert after.headers["cache-control"] == NO_STORE
    assert "location" not in after.headers


def test_a_head_request_on_an_expired_link_is_also_410(
    client, auth, make_link, monkeypatch
):
    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:00Z")
    make_link(name="q3-plan", expires="2026-01-01T00:00:01Z")

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:02Z")
    response = client.head("/q3-plan")
    assert response.status_code == 410


def test_a_link_expiring_at_exactly_now_is_already_gone(
    client, auth, make_link, target, monkeypatch
):
    """The boundary: `expires_at` is compared with `<=`, so "expires at T" reads as "no
    longer valid from T" -- the same sense a cookie or a cache entry gives its own
    Expires/Max-Age. One second before the boundary the link is still live; asked again
    at the boundary itself, with nothing else changed, it is gone.
    """
    monkeypatch.setattr(links_module, "_now", lambda: "2026-06-01T00:00:00Z")
    make_link(name="boundary", expires="2026-06-01T00:00:10Z")

    monkeypatch.setattr(links_module, "_now", lambda: "2026-06-01T00:00:09Z")
    one_second_early = client.get("/boundary")
    assert one_second_early.status_code == 302, one_second_early.text

    monkeypatch.setattr(links_module, "_now", lambda: "2026-06-01T00:00:10Z")
    at_the_instant = client.get("/boundary")
    assert at_the_instant.status_code == 410, (
        "expires_at must be inclusive of the instant it names"
    )


def test_an_unset_expiry_never_expires_no_matter_how_much_time_passes(
    client, auth, make_link, target, monkeypatch
):
    """Unset means forever (the owner's `expiry = A`): the mechanism must not quietly
    expire a link that was never given one, at any distance into the future.
    """
    make_link(name="forever")

    monkeypatch.setattr(links_module, "_now", lambda: "2999-01-01T00:00:00Z")
    response = client.get("/forever")
    assert response.status_code == 302, response.text
    assert response.headers["location"] == target


def test_a_generated_name_can_also_be_given_an_expiry(client, auth, target, monkeypatch):
    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:00Z")
    created = client.post(
        "/-/api/links",
        json={"url": target, "expires": "2026-01-01T00:00:05Z"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    name = created.json()["name"]

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:04Z")
    assert client.get(f"/{name}").status_code == 302

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:06Z")
    assert client.get(f"/{name}").status_code == 410


def test_a_valid_expiry_is_stored_exactly_as_sent(client, auth, target, db_path):
    created = client.post(
        "/-/api/links",
        json={"url": target, "name": "q3-plan", "expires": "2026-12-31T23:59:59Z"},
        headers=auth,
    )
    assert created.status_code == 201, created.text
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            "SELECT expires_at FROM links WHERE name = 'q3-plan'"
        ).fetchone()[0]
    assert stored == "2026-12-31T23:59:59Z"


def test_omitting_expires_stores_null(client, auth, target, db_path):
    created = client.post(
        "/-/api/links", json={"url": target, "name": "q3-plan"}, headers=auth
    )
    assert created.status_code == 201, created.text
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            "SELECT expires_at FROM links WHERE name = 'q3-plan'"
        ).fetchone()[0]
    assert stored is None


@pytest.mark.parametrize(
    "bad",
    [
        "7d",
        "2026-01-01",
        "2026-01-01 00:00:00Z",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T00:00:00",
        "2026-01-01T00:00:00.000Z",
        "not-a-date",
        "",
    ],
)
def test_expires_must_be_an_iso_8601_utc_timestamp_shaped_like_created_at(
    client, auth, target, bad
):
    """ADR-0013: only the exact shape `created_at` is stored and printed in is accepted."""
    response = client.post(
        "/-/api/links",
        json={"url": target, "name": "bad-expiry", "expires": bad},
        headers=auth,
    )
    assert response.status_code == 422, (bad, response.status_code, response.text)
    assert client.get("/bad-expiry").status_code == 404


def test_an_expired_link_can_still_be_deleted(client, auth, make_link, monkeypatch):
    """Expiry and deletion are independent states; a gone-by-time link is not immune to
    a real delete, and a repeat delete of it still answers 410 for the reason ADR-0011a
    gives -- the name stays reserved either way.
    """
    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:00Z")
    make_link(name="q3-plan", expires="2026-01-01T00:00:01Z")

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:02Z")
    assert client.get("/q3-plan").status_code == 410

    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204
    assert client.get("/q3-plan").status_code == 410
