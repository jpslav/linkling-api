"""The same promises, over a real socket, with a real HTTP client.

`TestClient` speaks ASGI in-process. These run the app under uvicorn on a loopback port
and follow the redirect to a second loopback server standing in for the long URL, so
"one hop" is counted by the client rather than asserted about a response object. It is
the HTTP half of LL-014; the real-browser-cache half is the manager's, and `README.md`
says how to run the service for it.

The fixtures are in `conftest.py`. The concurrent cases are in
`test_live_server_concurrency.py`, because "one request at a time" is exactly the
assumption that hid a defect here once.
"""

from __future__ import annotations

import httpx

#: What `conftest.py`'s stand-in for the long URL serves.
LONG_URL_BODY = b"arrived at the long url"


def test_a_real_client_reaches_the_long_url_in_exactly_one_hop(
    live_base_url, long_url_server, auth
):
    created = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": long_url_server, "name": "q3-plan"},
        headers=auth,
        timeout=10,
    )
    assert created.status_code == 201, created.text

    # No Authorization header and no cookie jar: this is the clicker, not the team.
    with httpx.Client(follow_redirects=True, timeout=10) as client:
        arrived = client.get(f"{live_base_url}/q3-plan")

    assert len(arrived.history) == 1, [r.status_code for r in arrived.history]
    hop = arrived.history[0]
    assert hop.status_code == 302
    assert hop.headers["location"] == long_url_server
    assert hop.headers["cache-control"] == "no-store"
    assert "set-cookie" not in hop.headers
    assert arrived.status_code == 200
    assert arrived.content == LONG_URL_BODY


def test_a_deleted_link_is_gone_over_the_wire_too(live_base_url, long_url_server, auth):
    created = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": long_url_server, "name": "q3-plan"},
        headers=auth,
        timeout=10,
    )
    assert created.status_code == 201, created.text

    deleted = httpx.delete(
        f"{live_base_url}/-/api/links/q3-plan", headers=auth, timeout=10
    )
    assert deleted.status_code == 204, deleted.text

    gone = httpx.get(f"{live_base_url}/q3-plan", follow_redirects=False, timeout=10)
    assert gone.status_code == 410
    assert gone.headers["cache-control"] == "no-store"
    assert "location" not in gone.headers


def test_a_keyless_write_is_refused_over_the_wire(live_base_url, long_url_server):
    refused = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": long_url_server, "name": "sneak"},
        timeout=10,
    )
    assert refused.status_code == 401, refused.text
    assert httpx.get(f"{live_base_url}/sneak", timeout=10).status_code == 404
