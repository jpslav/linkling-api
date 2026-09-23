"""LL-006 / R-008 -- following a link raises that link's count for today by exactly one.

Every count here is read by opening the database file with ``sqlite3`` directly, never
through the application: a reader inside the app would agree with a writer inside the app
about a table that is wrong in the same way for both.

A missing row reads as ``None`` (see ``_count``), which is never equal to a number, so a
follow that wrote nothing fails these tests rather than passing them.

The shape tests are the other half of LL-004's "the schema contains exactly those
fields": ADR-0004 allows a number per link per day and nothing else, and ADR-0014 says why
the table is ``WITHOUT ROWID``.
"""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from linkling.server import counts
from linkling.server import links as links_module


#: Every test here counts at this one instant (2026-09-23T12:00:00Z) unless it drives the
#: clock itself, so no assertion can straddle a real midnight UTC between two reads.
FIXED_INSTANT = 1790164800.0
TODAY = counts.utc_day(FIXED_INSTANT)


@pytest.fixture(autouse=True)
def _fixed_clock(monkeypatch):
    monkeypatch.setattr(counts, "_epoch_seconds", lambda: FIXED_INSTANT)


def _rows(db_path) -> list[tuple[str, str, int]]:
    """Every row of `daily_counts`, joined to its link's name, straight from the file."""
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT links.name, daily_counts.day, daily_counts.count "
            "FROM daily_counts JOIN links ON links.id = daily_counts.link_id "
            "ORDER BY links.name, daily_counts.day"
        ).fetchall()
    finally:
        conn.close()


def _all_count_rows(db_path) -> list[tuple]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT * FROM daily_counts").fetchall()
    finally:
        conn.close()


def _count(db_path, name: str, day: str | None = None) -> int | None:
    day = TODAY if day is None else day
    for row_name, row_day, row_count in _rows(db_path):
        if row_name == name and row_day == day:
            return row_count
    return None


# --- the count moves by exactly one ---------------------------------------------------


def test_each_follow_adds_exactly_one_whichever_way_it_arrives(
    client, make_link, db_path
):
    make_link(name="q3-plan")
    assert _count(db_path, "q3-plan") is None, "a count existed before any follow"

    for expected, request in enumerate(
        [
            lambda: client.get("/q3-plan"),
            lambda: client.get("/q3-plan"),
            lambda: client.head("/q3-plan"),
            lambda: client.get("/q3-plan/"),
            lambda: client.get("/q3-plan?utm=x"),
            lambda: client.get("/Q3-Plan"),
        ],
        start=1,
    ):
        response = request()
        assert response.status_code == 302, response.text
        assert _count(db_path, "q3-plan") == expected


def test_a_head_counts_the_same_as_a_get(client, make_link, db_path):
    make_link(name="by-get")
    make_link(name="by-head")
    assert client.get("/by-get").status_code == 302
    assert client.head("/by-head").status_code == 302
    assert _count(db_path, "by-get") == 1
    assert _count(db_path, "by-head") == 1


def test_a_follow_that_does_not_redirect_writes_nothing(
    client, auth, make_link, db_path, monkeypatch
):
    make_link(name="gone-soon")
    assert client.delete("/-/api/links/gone-soon", headers=auth).status_code == 204

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:00Z")
    make_link(name="short-lived", expires="2026-01-01T00:00:01Z")
    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:02Z")

    assert client.get("/no-such-link").status_code == 404
    assert client.get("/-not-a-name-").status_code == 404
    assert client.get("/gone-soon").status_code == 410
    assert client.head("/gone-soon").status_code == 410
    assert client.get("/short-lived").status_code == 410
    assert _all_count_rows(db_path) == []


def test_a_follow_that_loses_a_race_with_a_delete_is_410_and_uncounted(
    client, auth, make_link, db_path, monkeypatch
):
    """The lookup saw the link live; the delete landed before the increment.

    Simulated by letting the lookup return the stale, live answer after the tombstone is
    already written. The guarded increment then counts nothing, and the follow answers as
    the lookup would have a moment later -- so no 302 ever leaves uncounted.
    """
    make_link(name="q3-plan")
    stale = links_module.lookup
    seen: list[str] = []

    def stale_lookup(conn, name):
        seen.append(name)
        link = stale(conn, name)
        return None if link is None else links_module.Link(
            name=link.name, target="https://example.com/", deleted=False, expired=False
        )

    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204
    monkeypatch.setattr(links_module, "lookup", stale_lookup)
    response = client.get("/q3-plan")

    assert seen == ["q3-plan"], "the stale lookup was never consulted"
    assert response.status_code == 410, response.text
    assert "location" not in response.headers
    assert _all_count_rows(db_path) == []


def test_concurrent_follows_are_each_counted_once(live_base_url, auth, db_path):
    follows = 200
    created = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": "https://example.com/", "name": "q3-plan"},
        headers=auth,
        timeout=10,
    )
    assert created.status_code == 201, created.text

    def follow(_):
        try:
            return httpx.get(
                f"{live_base_url}/q3-plan", follow_redirects=False, timeout=20
            ).status_code
        except Exception as exc:  # noqa: BLE001 - a dropped connection is a failure too
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(follow, range(follows)))

    assert set(results) == {302}, Counter(results)
    assert _count(db_path, "q3-plan") == follows


# --- nothing finer than a day survives ------------------------------------------------


def test_the_table_holds_a_link_a_day_and_a_number_and_nothing_else(
    client, make_link, db_path
):
    make_link(name="q3-plan")  # runs the app's lifespan, so the migrations are applied
    conn = sqlite3.connect(str(db_path))
    try:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(daily_counts)")]
        schema = sorted(
            (row[0], row[1])
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        with pytest.raises(sqlite3.OperationalError, match="no such column: rowid"):
            conn.execute("SELECT rowid FROM daily_counts")
    finally:
        conn.close()

    assert columns == ["link_id", "day", "count"]
    # `sqlite_autoindex_links_1` backs `links.name UNIQUE` and is excluded by the filter.
    assert schema == [
        ("table", "daily_counts"),
        ("table", "links"),
        ("table", "schema_version"),
        ("trigger", "daily_counts_die_with_their_link"),
    ]


def test_many_follows_in_one_day_are_one_row(client, make_link, db_path):
    make_link(name="q3-plan")
    for _ in range(25):
        assert client.get("/q3-plan").status_code == 302
    today = TODAY
    assert _rows(db_path) == [("q3-plan", today, 25)]


def test_the_day_column_refuses_a_timestamp(client, make_link, db_path):
    make_link(name="q3-plan")
    conn = sqlite3.connect(str(db_path))
    try:
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            conn.execute(
                "INSERT INTO daily_counts(link_id, day, count) "
                "SELECT id, '2026-09-23T10:00:00Z', 1 FROM links WHERE name = 'q3-plan'"
            )
    finally:
        conn.close()


# --- retention: counts die with their link --------------------------------------------


def test_deleting_a_link_removes_its_counts_and_only_its_counts(
    client, auth, make_link, db_path
):
    make_link(name="q3-plan")
    make_link(name="keep-me")
    for name in ("q3-plan", "q3-plan", "keep-me"):
        assert client.get(f"/{name}").status_code == 302
    today = TODAY
    assert _rows(db_path) == [("keep-me", today, 1), ("q3-plan", today, 2)], (
        "blind: the counts were not there before the delete, so their absence after it "
        "would prove nothing"
    )

    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204

    conn = sqlite3.connect(str(db_path))
    try:
        (link_id,) = conn.execute(
            "SELECT id FROM links WHERE name = 'q3-plan'"
        ).fetchone()
        left = conn.execute(
            "SELECT * FROM daily_counts WHERE link_id = ?", (link_id,)
        ).fetchall()
    finally:
        conn.close()
    assert left == []
    assert _rows(db_path) == [("keep-me", today, 1)]

    assert client.get("/q3-plan").status_code == 410
    assert _rows(db_path) == [("keep-me", today, 1)]


def test_an_expired_links_counts_are_kept(client, make_link, db_path, monkeypatch):
    """ADR-0004: counts are kept until their link is *deleted*; expiry is not deletion."""
    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:00Z")
    make_link(name="q3-plan", expires="2026-01-01T00:00:10Z")
    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:05Z")
    assert client.get("/q3-plan").status_code == 302
    before = _rows(db_path)
    assert len(before) == 1 and before[0][2] == 1, before

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:11Z")
    assert client.get("/q3-plan").status_code == 410
    assert _rows(db_path) == before


# --- the UTC day boundary -------------------------------------------------------------

#: 2026-09-28T23:59:59.5Z and 2026-09-29T00:00:00Z.
JUST_BEFORE_MIDNIGHT_UTC = 1790639999.5
MIDNIGHT_UTC = 1790640000.0


@pytest.fixture
def host_timezone_ahead_of_utc(monkeypatch):
    """Put the process in UTC+14, where it is already the 29th at the first instant."""
    monkeypatch.setenv("TZ", "Pacific/Kiritimati")
    time.tzset()
    assert time.localtime(JUST_BEFORE_MIDNIGHT_UTC).tm_mday == 29, (
        "blind: the host timezone did not change, so this test could not tell UTC from "
        "local time"
    )
    yield
    monkeypatch.undo()
    time.tzset()


def test_follows_either_side_of_midnight_utc_land_on_different_days(
    client, make_link, db_path, monkeypatch, host_timezone_ahead_of_utc
):
    instants = iter([JUST_BEFORE_MIDNIGHT_UTC, MIDNIGHT_UTC])
    calls: list[float] = []

    def clock() -> float:
        calls.append(next(instants))
        return calls[-1]

    make_link(name="q3-plan")
    monkeypatch.setattr(counts, "_epoch_seconds", clock)
    assert client.get("/q3-plan").status_code == 302
    assert client.get("/q3-plan").status_code == 302

    assert len(calls) == 2, "blind: the counter never read the driven clock"
    assert _rows(db_path) == [
        ("q3-plan", "2026-09-28", 1),
        ("q3-plan", "2026-09-29", 1),
    ]
