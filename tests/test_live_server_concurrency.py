"""What happens when two people click at the same time.

Everything else in this suite issues one request at a time, and that is precisely the
assumption that hid the worst defect this branch had: FastAPI runs a sync dependency's
setup, the handler and its teardown as three separate threadpool submissions, and anyio
gives each to whichever worker is free. Sequentially that is always the same worker, so a
SQLite connection opened with the default ``check_same_thread=True`` never noticed.
Concurrently the handler runs on a different worker and sqlite3 raises
``ProgrammingError``. Measured against the shipped entry point before the fix: **161 of
200 concurrent follows answered 500**.

A green suite said nothing about that, which is the argument for this file existing
rather than for one more sequential assertion.
"""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import httpx

FOLLOWS = 200
CREATES = 100
DELETERS = 8


def test_concurrent_follows_all_succeed(live_base_url, long_url_server, auth):
    created = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": long_url_server, "name": "q3-plan"},
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
        results = list(pool.map(follow, range(FOLLOWS)))

    assert len(results) == FOLLOWS, len(results)
    assert set(results) == {302}, Counter(results)


def test_concurrent_creates_all_succeed_and_names_stay_unique(
    live_base_url, long_url_server, auth
):
    def create(_):
        try:
            response = httpx.post(
                f"{live_base_url}/-/api/links",
                json={"url": long_url_server},
                headers=auth,
                timeout=20,
            )
            name = response.json().get("name") if response.status_code == 201 else None
            return response.status_code, name
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__, None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(create, range(CREATES)))

    statuses = Counter(status for status, _ in results)
    assert statuses == Counter({201: CREATES}), statuses
    names = [name for _, name in results]
    assert len(set(names)) == CREATES, "a generated name was issued twice"


def test_only_one_concurrent_delete_is_told_it_deleted_the_link(
    live_base_url, long_url_server, auth
):
    """The losers of the race hear 410. Looking first and updating second gave them 204."""
    created = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": long_url_server, "name": "contested"},
        headers=auth,
        timeout=10,
    )
    assert created.status_code == 201, created.text

    def delete(_):
        try:
            return httpx.delete(
                f"{live_base_url}/-/api/links/contested", headers=auth, timeout=20
            ).status_code
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__

    with ThreadPoolExecutor(max_workers=DELETERS) as pool:
        results = list(pool.map(delete, range(DELETERS)))

    counts = Counter(results)
    assert counts[204] == 1, counts
    assert counts[410] == DELETERS - 1, counts
