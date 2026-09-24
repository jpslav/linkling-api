"""LL-026 / R-008 -- ``GET /-/api/links/<name>/stats`` serves one link's count for each UTC day.

R-008's ``verify`` line reads this path with the team key as a Bearer token and asks
``jq -e '.days[<today>] == 1'`` of the answer, so the shape pinned here is the one that
expression reads: ``{"days": {"YYYY-MM-DD": <count>}}``, and nothing beside ``days``.

As in LL-017, the key check is only meaningful as a pair: a route that refused everybody
would pass every "401" test, and one that refused nobody would pass every "200" test.

Where a test needs to know what ``daily_counts`` holds, it reads the database file with
``sqlite3``, never through the application, for the reason ``test_ll006_daily_counts`` gives.
"""

from __future__ import annotations

import base64
import sqlite3

import pytest

from linkling.server import counts
from linkling.server import links as links_module

#: 2026-09-23T12:00:00Z, the instant test_ll006_daily_counts counts at. Noon, so that a day
#: is unambiguous whichever way a test rounds.
DAY_ONE = 1790164800.0
DAY_TWO = DAY_ONE + 86400
DAY_THREE = DAY_TWO + 86400

STATS = "/-/api/links/{name}/stats"


@pytest.fixture
def follow_on(client, monkeypatch):
    """Follow a link ``times`` times at a chosen instant, requiring each follow to redirect.

    A redirect is a counted follow (ADR-0004), so this is how a test puts rows in
    ``daily_counts`` for the route to report.
    """

    def _follow(name: str, instant: float, times: int = 1) -> None:
        monkeypatch.setattr(counts, "_epoch_seconds", lambda: instant)
        for _ in range(times):
            assert client.get(f"/{name}").status_code == 302

    return _follow


def _count_rows(db_path) -> list[tuple[str, str, int]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT links.name, daily_counts.day, daily_counts.count "
            "FROM daily_counts JOIN links ON links.id = daily_counts.link_id "
            "ORDER BY links.name, daily_counts.day"
        ).fetchall()
    finally:
        conn.close()


def _stats(client, auth, name: str = "q3-plan"):
    return client.get(STATS.format(name=name), headers=auth)


def test_the_test_days_are_the_dates_these_tests_name():
    """The tests below read their keys off ``counts.utc_day``; this pins that to real dates."""
    assert [counts.utc_day(d) for d in (DAY_ONE, DAY_TWO, DAY_THREE)] == [
        "2026-09-23",
        "2026-09-24",
        "2026-09-25",
    ]


# --- the shape R-008's jq expression reads ------------------------------------------------


def test_the_answer_is_a_days_object_keyed_by_utc_date_and_nothing_else(
    client, auth, make_link, follow_on
):
    make_link(name="q3-plan")
    follow_on("q3-plan", DAY_ONE, times=2)
    follow_on("q3-plan", DAY_TWO)

    response = _stats(client, auth)

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"days": {"2026-09-24": 1, "2026-09-23": 2}}


def test_a_day_nobody_followed_the_link_has_no_key(client, auth, make_link, follow_on):
    """R-008's blind line: a missing day must be ``null`` to ``jq -e``, never an invented 0."""
    make_link(name="q3-plan")
    follow_on("q3-plan", DAY_ONE)
    follow_on("q3-plan", DAY_THREE)

    days = _stats(client, auth).json()["days"]

    assert sorted(days) == ["2026-09-23", "2026-09-25"]
    assert "2026-09-24" not in days


def test_a_link_nobody_has_followed_has_an_empty_days_object(client, auth, make_link):
    make_link(name="q3-plan")

    response = _stats(client, auth)

    assert response.status_code == 200
    assert response.json() == {"days": {}}


def test_the_count_rises_by_exactly_one_per_follow_as_r008_reads_it(
    client, auth, make_link, follow_on
):
    """R-008's ``verify``: read, follow once, read again, and today's count is one higher."""
    make_link(name="q3-plan")
    follow_on("q3-plan", DAY_ONE)
    today = counts.utc_day(DAY_ONE)

    before = _stats(client, auth).json()["days"][today]
    follow_on("q3-plan", DAY_ONE)
    after = _stats(client, auth).json()["days"][today]

    assert (before, after) == (1, 2)


def test_the_answer_is_the_named_links_rows_and_no_other_links(
    client, auth, make_link, follow_on, db_path
):
    make_link(name="q3-plan")
    make_link(name="alpha-one", url="https://example.org/second")
    follow_on("q3-plan", DAY_ONE, times=3)
    follow_on("alpha-one", DAY_ONE, times=5)
    follow_on("alpha-one", DAY_TWO)

    assert _stats(client, auth, "q3-plan").json() == {"days": {"2026-09-23": 3}}
    assert _stats(client, auth, "alpha-one").json() == {
        "days": {"2026-09-24": 1, "2026-09-23": 5}
    }
    assert _count_rows(db_path) == [
        ("alpha-one", "2026-09-23", 5),
        ("alpha-one", "2026-09-24", 1),
        ("q3-plan", "2026-09-23", 3),
    ]


def test_the_answer_is_never_cacheable(client, auth, make_link):
    make_link(name="q3-plan")

    assert _stats(client, auth).headers["cache-control"] == "no-store"


def test_a_name_is_folded_as_a_follow_folds_it(client, auth, make_link, follow_on):
    make_link(name="q3-plan")
    follow_on("q3-plan", DAY_ONE)

    response = _stats(client, auth, "Q3-PLAN")

    assert response.status_code == 200
    assert response.json() == {"days": {"2026-09-23": 1}}


# --- the team key (ADR-0006a): Bearer, as the create and delete calls ---------------------


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-the-key"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic " + base64.b64encode(b":test-team-key-7fqj2").decode()},
    ],
    ids=["no-header", "wrong-key", "bearer-without-token", "empty-token", "basic-with-the-key"],
)
def test_without_the_team_key_as_a_bearer_token_the_answer_is_401(
    client, auth, make_link, headers
):
    make_link(name="q3-plan")

    response = client.get(STATS.format(name="q3-plan"), headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "days" not in response.text


def test_the_same_request_with_the_key_is_200(client, auth, make_link):
    """The other half of the pair: the 401 above is the key's doing, not the route's."""
    make_link(name="q3-plan")

    assert client.get(STATS.format(name="q3-plan")).status_code == 401
    assert client.get(STATS.format(name="q3-plan"), headers=auth).status_code == 200


def test_the_key_is_checked_before_the_name_so_a_caller_without_it_learns_no_names(
    client, auth, make_link
):
    make_link(name="q3-plan")
    make_link(name="gone-link", url="https://example.org/gone")
    assert client.delete("/-/api/links/gone-link", headers=auth).status_code == 204

    answers = {
        name: client.get(STATS.format(name=name))
        for name in ("q3-plan", "gone-link", "never-made")
    }

    assert {name: r.status_code for name, r in answers.items()} == {
        "q3-plan": 401,
        "gone-link": 401,
        "never-made": 401,
    }
    assert len({r.text for r in answers.values()}) == 1


# --- unknown, deleted, expired ------------------------------------------------------------


def test_a_name_that_never_existed_is_404(client, auth):
    response = _stats(client, auth, "never-made")

    assert response.status_code == 404
    assert "days" not in response.text


@pytest.mark.parametrize("name", ["-not-a-name", "under_score", "a" * 65])
def test_a_name_that_cannot_be_a_link_is_404_like_a_name_that_never_existed(
    client, auth, name
):
    assert _stats(client, auth, name).status_code == 404


def test_a_deleted_name_is_410_not_an_empty_200(client, auth, make_link, follow_on):
    make_link(name="q3-plan")
    follow_on("q3-plan", DAY_ONE, times=2)
    assert _stats(client, auth).json() == {"days": {"2026-09-23": 2}}

    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204

    response = _stats(client, auth)
    assert response.status_code == 410
    assert "days" not in response.text


def test_an_expired_links_kept_counts_are_served_with_200(
    client, auth, make_link, follow_on, monkeypatch
):
    """ADR-0014 keeps an expired link's counts, and the stats page shows them; so does this.

    A follow of the same link is 410 by then, so the counts cannot move either way.
    """
    monkeypatch.setattr(links_module, "_now", lambda: "2026-09-23T11:00:00Z")
    make_link(name="q3-plan", expires="2026-09-23T13:00:00Z")
    monkeypatch.setattr(links_module, "_now", lambda: "2026-09-23T12:00:00Z")
    follow_on("q3-plan", DAY_ONE, times=2)

    monkeypatch.setattr(links_module, "_now", lambda: "2026-09-23T14:00:00Z")
    assert client.get("/q3-plan").status_code == 410

    response = _stats(client, auth)
    assert response.status_code == 200
    assert response.json() == {"days": {"2026-09-23": 2}}


# --- reading is not following -------------------------------------------------------------


def test_reading_stats_never_counts_as_a_follow(
    client, auth, make_link, follow_on, db_path, monkeypatch
):
    """Every way of asking -- live, never followed, unknown, deleted, expired, refused --
    leaves ``daily_counts`` exactly as the follows left it.

    A live link nobody has followed is the case that catches a route which counts on read:
    it would write a row where there was none, and answer with today's count at 1.
    """
    monkeypatch.setattr(links_module, "_now", lambda: "2026-09-23T11:00:00Z")
    make_link(name="q3-plan")
    make_link(name="never-followed", url="https://example.org/second")
    make_link(name="gone-link", url="https://example.org/gone")
    make_link(name="lapsed", url="https://example.org/lapsed", expires="2026-09-23T13:00:00Z")
    monkeypatch.setattr(links_module, "_now", lambda: "2026-09-23T12:00:00Z")
    follow_on("q3-plan", DAY_ONE, times=2)
    follow_on("lapsed", DAY_ONE)
    follow_on("gone-link", DAY_ONE)
    assert client.delete("/-/api/links/gone-link", headers=auth).status_code == 204
    monkeypatch.setattr(links_module, "_now", lambda: "2026-09-23T14:00:00Z")
    before = _count_rows(db_path)
    assert before == [("lapsed", "2026-09-23", 1), ("q3-plan", "2026-09-23", 2)]

    for name in ("q3-plan", "never-followed", "never-made", "gone-link", "lapsed"):
        for _ in range(3):
            assert _stats(client, auth, name).status_code in (200, 404, 410)
            client.get(STATS.format(name=name))  # and without the key

    assert _count_rows(db_path) == before
    assert _stats(client, auth, "never-followed").json() == {"days": {}}
    assert _stats(client, auth, "q3-plan").json() == {"days": {"2026-09-23": 2}}
