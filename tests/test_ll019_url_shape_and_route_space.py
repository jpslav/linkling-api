"""LL-019 -- the service refuses to issue a link that does not conform to ADR-0001.

The *Done when* is "the service refuses to issue a link that does not conform", so each
assertion here is an effect of the running service: a status code **and** the absence of a
row, because a refusal that still wrote a row is not a refusal.
"""

from __future__ import annotations

import sqlite3

import pytest

# Each of these breaks ADR-0001d in a different way: a leading or trailing hyphen (which
# is also what keeps custom names out of the service's own `/-/` space), a character
# outside the grammar, a path separator, whitespace, the trailing newline that a
# `re.match` + `$` reading of the grammar would have admitted, over-length, empty, and
# non-ASCII.
NON_CONFORMING = [
    "-x",
    "x-",
    "-",
    "--",
    "q3_plan",
    "q3.plan",
    "q3/plan",
    "q3 plan",
    "q3-plan\n",
    "x" * 65,
    "",
    "q3-plán",
    "K9",  # U+212A folds to ASCII "k": the ASCII check has to run first
]

CONFORMING = ["q3-plan", "a", "9", "x7kq2m", "a-b-c", "x" * 64]


def test_a_non_conforming_name_is_refused_and_nothing_is_stored(client, auth, db_path, target):
    for name in NON_CONFORMING:
        response = client.post(
            "/-/api/links", json={"url": target, "name": name}, headers=auth
        )
        assert response.status_code == 422, (name, response.status_code, response.text)

    assert len(NON_CONFORMING) > 0  # the population is fixed here, so zero is a failure
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("SELECT count(*) FROM links").fetchone()[0]
    assert stored == 0, f"{stored} row(s) written by refused creates"


@pytest.mark.parametrize("name", CONFORMING)
def test_a_conforming_name_is_accepted_and_resolves(client, auth, name, target):
    created = client.post(
        "/-/api/links", json={"url": target, "name": name}, headers=auth
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == name

    followed = client.get(f"/{name}")
    assert followed.status_code == 302
    assert followed.headers["location"] == target


def test_a_name_is_folded_to_lowercase_when_made_and_when_followed(client, auth, target):
    created = client.post(
        "/-/api/links", json={"url": target, "name": "Q3-Plan"}, headers=auth
    )
    assert created.status_code == 201, created.text
    assert created.json()["name"] == "q3-plan"

    for spelling in ("q3-plan", "Q3-Plan", "Q3-PLAN"):
        followed = client.get(f"/{spelling}")
        assert followed.status_code == 302, spelling
        assert followed.headers["location"] == target


def test_the_same_name_in_another_case_is_the_same_link(client, auth, make_link, target):
    make_link(name="q3-plan")
    again = client.post(
        "/-/api/links", json={"url": target, "name": "Q3-PLAN"}, headers=auth
    )
    assert again.status_code == 409, again.text


def test_the_root_namespace_belongs_to_link_names(app):
    """Every route except the two follow routes lives under ADR-0001b's `/-/` prefix.

    FastAPI's defaults would have mounted `/docs`, `/redoc` and `/openapi.json` at the
    root, and `docs` and `redoc` are names somebody could want for a link.
    """
    paths = [route.path for route in app.routes]
    assert paths, "no routes at all -- the assertion below would pass having examined none"

    follow_routes = {"/{name}", "/{name}/"}
    at_root = [
        path for path in paths if not path.startswith("/-/") and path not in follow_routes
    ]
    assert at_root == [], f"routes occupying the link namespace: {at_root}"
    assert follow_routes.issubset(set(paths)), paths
