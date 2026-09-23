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
        pytest.param("7d", id="cli-style-duration"),
        pytest.param("2030-01-01", id="date-only"),
        pytest.param("2030-01-01 00:00:00Z", id="space-not-t"),
        pytest.param("2030-01-01T00:00:00+00:00", id="numeric-offset"),
        pytest.param("2030-01-01T00:00:00", id="no-z"),
        pytest.param("2030-01-01T00:00:00.000Z", id="fractional-seconds"),
        pytest.param("not-a-date", id="garbage"),
        pytest.param("", id="empty"),
        # `strptime` parses these -- verified by running it -- but none is the shape
        # `_now()` ever prints, and `links.lookup`'s boundary check is a plain string
        # compare that depends on every stored value sharing that one shape.
        pytest.param("2030-1-01T00:00:00Z", id="single-digit-month"),
        pytest.param("2030-01-01t00:00:00z", id="lowercase-literals"),
        pytest.param("2030-01- 5T00:00:00Z", id="space-padded-day"),
        pytest.param("۲۰۳۰-01-01T00:00:00Z", id="extended-arabic-indic-digits"),
        pytest.param("２０３０-01-01T00:00:00Z", id="fullwidth-digits"),
    ],
)
def test_expires_must_be_an_iso_8601_utc_timestamp_shaped_like_created_at(
    client, auth, target, bad
):
    """ADR-0013: only the exact shape `created_at` is stored and printed in is accepted --
    not merely a shape `strptime` can parse. Every value here names a year in 2030, so a
    build that fixed the shape check but not the future-only check (or vice versa) cannot
    make this pass for the wrong reason.
    """
    response = client.post(
        "/-/api/links",
        json={"url": target, "name": "bad-expiry", "expires": bad},
        headers=auth,
    )
    assert response.status_code == 422, (bad, response.status_code, response.text)
    assert client.get("/bad-expiry").status_code == 404


def test_expires_in_the_past_is_refused_rather_than_creating_a_dead_on_arrival_link(
    client, auth, target, monkeypatch
):
    """Without this, a typo'd year makes a link that is 410 from its first follow, with
    its name reserved forever exactly as ADR-0005 reserves one after any other mistake.
    """
    monkeypatch.setattr(links_module, "_now", lambda: "2026-06-01T00:00:00Z")

    response = client.post(
        "/-/api/links",
        json={"url": target, "name": "already-gone", "expires": "2026-05-31T23:59:59Z"},
        headers=auth,
    )
    assert response.status_code == 422, response.text
    assert client.get("/already-gone").status_code == 404

    at_the_boundary = client.post(
        "/-/api/links",
        json={"url": target, "name": "also-already-gone", "expires": "2026-06-01T00:00:00Z"},
        headers=auth,
    )
    assert at_the_boundary.status_code == 422, at_the_boundary.text


def test_the_clock_is_read_once_per_create_so_expires_and_created_at_cannot_disagree(
    client, auth, target, monkeypatch, db_path
):
    """Round 2 finding: validating `expires` is still in the future and stamping
    `created_at` used to read the clock separately, so a tick landing between the two
    reads could make an `expires` chosen one second out land at or before `created_at`.
    Reading the clock once per request and reusing it removes the race by construction;
    this pins that there is exactly one read, by making a second one answer differently.
    """
    answers = iter(
        ["2026-01-01T00:00:00Z", "2026-01-01T00:00:01Z", "2026-01-01T00:00:02Z"]
    )
    monkeypatch.setattr(links_module, "_now", lambda: next(answers))

    created = client.post(
        "/-/api/links",
        json={"url": target, "name": "q3-plan", "expires": "2026-01-01T00:00:01Z"},
        headers=auth,
    )
    assert created.status_code == 201, created.text

    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT created_at, expires_at FROM links WHERE name = 'q3-plan'"
        ).fetchone()
    assert row[0] == "2026-01-01T00:00:00Z", "created_at read the clock a second time"
    assert row[1] == "2026-01-01T00:00:01Z"


def test_a_tombstoned_links_expiry_is_left_as_it_was(
    client, auth, make_link, monkeypatch, db_path
):
    """Pinned, not decided here: ADR-0005 lists `target`, `created_by` and counts as what
    a tombstone drops, and says nothing about `expires_at`. This asserts today's actual
    behaviour (it survives) so a change to it is a visible diff rather than a silent one.
    """
    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:00Z")
    make_link(name="q3-plan", expires="2026-12-31T23:59:59Z")
    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204

    with sqlite3.connect(db_path) as conn:
        stored = conn.execute(
            "SELECT expires_at FROM links WHERE name = 'q3-plan'"
        ).fetchone()[0]
    assert stored == "2026-12-31T23:59:59Z"


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
