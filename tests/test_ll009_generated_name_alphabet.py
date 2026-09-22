"""LL-009 -- a generated name never contains a character outside ADR-0002's alphabet.

The *Done when* asks for the guard explicitly: the test must reject any name outside the
alphabet "having first asserted it examined a non-zero number of names". The population is
fixed by this test -- it asks the running service for exactly ``POPULATION`` names -- so
zero names is a failure and never the goal, and the count assertion fires before a single
name is inspected.
"""

from __future__ import annotations

import re

#: ADR-0002: six characters from the consonants excluding `y`, plus the digits 2-9.
ALPHABET_PATTERN = re.compile(r"[bcdfghjklmnpqrstvwxz2-9]{6}")

POPULATION = 200


def test_every_generated_name_uses_only_the_agreed_alphabet(client, auth, target):
    generated: list[str] = []
    for _ in range(POPULATION):
        response = client.post("/-/api/links", json={"url": target}, headers=auth)
        assert response.status_code == 201, response.text
        generated.append(response.json()["name"])

    assert len(generated) == POPULATION, (
        f"blind: examined {len(generated)} names, expected {POPULATION} -- "
        "the assertions below would have inspected whatever arrived"
    )

    offenders = [name for name in generated if not ALPHABET_PATTERN.fullmatch(name)]
    assert offenders == [], (
        f"examined {len(generated)} names; outside ADR-0002's alphabet: {offenders[:5]}"
    )


def test_a_generated_name_is_six_characters_and_never_issued_twice(client, auth, target):
    generated: list[str] = []
    for _ in range(POPULATION):
        response = client.post("/-/api/links", json={"url": target}, headers=auth)
        assert response.status_code == 201, response.text
        generated.append(response.json()["name"])

    assert len(generated) == POPULATION
    assert {len(name) for name in generated} == {6}, sorted({len(n) for n in generated})
    assert len(set(generated)) == POPULATION, "a generated name was issued twice"


def test_a_generated_name_resolves_to_the_long_url(client, auth, target):
    created = client.post("/-/api/links", json={"url": target}, headers=auth)
    assert created.status_code == 201, created.text
    name = created.json()["name"]

    followed = client.get(f"/{name}")
    assert followed.status_code == 302
    assert followed.headers["location"] == target
