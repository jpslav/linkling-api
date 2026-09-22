"""LL-017 -- writes need the team key, following needs nothing at all.

ADR-0006's Consequences say why the two halves live in one test: "either alone passes
against a service that refuses everything or refuses nothing". A service that refused
every request would pass the write half; a service that refused nothing would pass the
follow half. Only the pair says anything.
"""

from __future__ import annotations

import pytest


def test_writes_need_the_key_and_following_needs_no_credential(
    client, auth, make_link, target
):
    created = make_link(name="q3-plan")
    assert created["name"] == "q3-plan"

    # Half one: writes without the key are refused. Both writes, because "making,
    # deleting and listing links needs a team API key" (brief line 29) is about the verbs,
    # not about one of them.
    keyless_create = client.post("/-/api/links", json={"url": target, "name": "sneak"})
    assert keyless_create.status_code == 401, keyless_create.text
    assert keyless_create.headers.get("www-authenticate") == "Bearer"

    keyless_delete = client.delete("/-/api/links/q3-plan")
    assert keyless_delete.status_code == 401, keyless_delete.text

    # ...and each refusal is an effect, not only a status: the link it tried to make does
    # not exist, and the link it tried to delete still resolves.
    assert client.get("/sneak").status_code == 404

    # Half two: following works with no credential and no cookie, and sets none.
    client.cookies.clear()
    followed = client.get("/q3-plan")
    assert followed.status_code == 302
    assert followed.headers["location"] == target
    assert "set-cookie" not in followed.headers, dict(followed.headers)
    assert len(client.cookies) == 0, dict(client.cookies)
    assert "authorization" not in {name.lower() for name in followed.request.headers}


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": "Bearer wrong-key"},
        {"Authorization": "Bearer "},
        {"Authorization": "Basic dGVzdDp0ZXN0"},
        {"Authorization": ""},
    ],
)
def test_a_write_with_the_wrong_credential_is_refused(client, header, target):
    response = client.post(
        "/-/api/links", json={"url": target, "name": "nope"}, headers=header
    )
    assert response.status_code == 401, response.text
    assert client.get("/nope").status_code == 404


def test_deleting_with_the_key_works(client, auth, make_link):
    make_link(name="q3-plan")
    deleted = client.delete("/-/api/links/q3-plan", headers=auth)
    assert deleted.status_code == 204, deleted.text
