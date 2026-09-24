"""LL-016 / R-010 -- ``GET /-/stats`` lists every link with its count for each day.

The page is opened in a browser, so it is gated by HTTP Basic with the team key as the
password (ADR-0006b), not by the Bearer header the API uses. As in LL-017, the gate is only
meaningful as a pair: a page that refused everybody would pass the "401 without a key"
half, and a page that refused nobody would pass the "200 with one" half.

Counts are read straight from the database file with ``sqlite3``, never through the
application, for the reason ``test_ll006_daily_counts`` gives.
"""

from __future__ import annotations

import base64
import re
import sqlite3
from html.parser import HTMLParser

import httpx
import pytest
from fastapi.testclient import TestClient

from linkling.server import counts
from linkling.server import links as links_module
from linkling.server.app import create_app
from linkling.server.config import Config

#: 2026-09-23T12:00:00Z, the instant test_ll006_daily_counts counts at. Noon, so that a day
#: is unambiguous whichever way a test rounds.
DAY_ONE = 1790164800.0
DAY_TWO = DAY_ONE + 86400
DAY_THREE = DAY_TWO + 86400


def _basic(user: str, password: str | bytes) -> dict[str, str]:
    raw = user.encode() + b":" + (password if isinstance(password, bytes) else password.encode())
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@pytest.fixture
def stats_auth(config) -> dict[str, str]:
    """What ``curl -u ":$KEY"`` sends: an empty user name and the key as the password."""
    return _basic("", config.api_key)


@pytest.fixture
def follow_on(client, monkeypatch):
    """Follow a link ``times`` times at a chosen instant, and say it counted."""

    def _follow(name: str, instant: float, times: int = 1) -> None:
        monkeypatch.setattr(counts, "_epoch_seconds", lambda: instant)
        for _ in range(times):
            assert client.get(f"/{name}").status_code == 302

    return _follow


def _count_rows(db_path) -> list[tuple[int, str, int]]:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute(
            "SELECT link_id, day, count FROM daily_counts ORDER BY link_id, day"
        ).fetchall()
    finally:
        conn.close()


class _Page(HTMLParser):
    """The stats page as data: its links in order, each with its paragraphs and day rows."""

    def __init__(self, html: str):
        super().__init__()
        self.sections: list[dict] = []
        self.preamble: list[str] = []
        self._tag: str | None = None
        self._text: list[str] = []
        self._cells: list[str] = []
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        if tag in ("h1", "h2", "p", "td", "th"):
            self._tag, self._text = tag, []
        if tag == "h2":
            self.sections.append({"name": "", "paragraphs": [], "rows": []})
        if tag == "tr":
            self._cells = []

    def handle_data(self, data):
        if self._tag is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == self._tag:
            text = "".join(self._text).strip()
            if tag == "h2":
                self.sections[-1]["name"] = text
            elif tag == "p":
                (self.sections[-1]["paragraphs"] if self.sections else self.preamble).append(text)
            elif tag == "td":
                self._cells.append(text)
            self._tag = None
        if tag == "tr" and len(self._cells) == 2:
            day, count = self._cells
            self.sections[-1]["rows"].append((day, int(count)))

    def section(self, name: str) -> dict:
        found = [s for s in self.sections if s["name"] == name]
        assert len(found) == 1, f"{name!r} appears {len(found)} times: {self.sections}"
        return found[0]


def _page(response) -> _Page:
    assert response.status_code == 200, response.text
    return _Page(response.text)


# --- the page lists every link with its count for each day ------------------------------


def test_the_page_lists_every_link_with_its_count_for_each_day(
    client, make_link, follow_on, stats_auth, target
):
    make_link(name="q3-plan")
    make_link(name="alpha-one", url="https://example.org/second")
    make_link(name="never-followed", url="https://example.net/third")
    follow_on("q3-plan", DAY_ONE, times=2)
    follow_on("q3-plan", DAY_TWO)
    follow_on("alpha-one", DAY_TWO)

    response = client.get("/-/stats", headers=stats_auth)

    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    page = _page(response)
    assert [s["name"] for s in page.sections] == ["alpha-one", "never-followed", "q3-plan"]

    q3 = page.section("q3-plan")
    # Byte-exact, ampersand and pipe and all: escaped on the wire, read back by the parser.
    assert target in q3["paragraphs"]
    assert q3["rows"] == [("2026-09-24", 1), ("2026-09-23", 2)]
    assert page.section("alpha-one")["rows"] == [("2026-09-24", 1)]
    assert "https://example.org/second" in page.section("alpha-one")["paragraphs"]

    # A link nobody has followed is still a link: it is listed, with no invented zero row.
    quiet = page.section("never-followed")
    assert quiet["rows"] == []
    assert "No follows yet." in quiet["paragraphs"]


def test_an_empty_service_answers_with_a_page_and_no_links(client, stats_auth):
    page = _page(client.get("/-/stats", headers=stats_auth))
    assert page.sections == []
    assert "No links yet." in page.preamble


# --- the gate: Basic, the team key as the password ---------------------------------------


def test_the_page_needs_the_key_over_basic_and_gives_it_to_no_one_else(
    client, make_link, stats_auth, auth
):
    make_link(name="secret-name", url="https://example.com/secret-target")

    # Half one: the key, as curl -u ":$KEY" sends it, opens the page.
    opened = client.get("/-/stats", headers=stats_auth)
    assert opened.status_code == 200, opened.text
    assert "secret-name" in opened.text

    # Half two: nothing else does, and the refusal carries a Basic challenge so a browser
    # asks for a password, and carries nothing of what the page holds.
    refused = client.get("/-/stats")
    assert refused.status_code == 401, refused.text
    assert refused.headers["www-authenticate"].startswith("Basic realm=")
    assert "secret-name" not in refused.text
    assert "secret-target" not in refused.text

    # ADR-0006b: Basic, not Bearer. The right key in the API's own scheme is still refused.
    bearer = client.get("/-/stats", headers=auth)
    assert bearer.status_code == 401, bearer.text
    assert bearer.headers["www-authenticate"].startswith("Basic realm=")


@pytest.mark.parametrize(
    "header",
    [
        _basic("", "not-the-key"),
        _basic("", ""),
        _basic("the-key-as-a-user-name", "not-the-key"),
        _basic("", b"\xff\xfe"),
        {"Authorization": "Basic " + base64.b64encode(b"no-colon-at-all").decode()},
        {"Authorization": "Basic !!!not-base64!!!"},
        {"Authorization": "Basic "},
        {"Authorization": "Basic"},
        {"Authorization": "Digest username=x"},
        {"Authorization": ""},
    ],
    ids=[
        "wrong-password",
        "empty-password",
        "key-in-the-user-field",
        "non-utf8-password",
        "no-colon",
        "not-base64",
        "empty-credentials",
        "scheme-only",
        "other-scheme",
        "empty-header",
    ],
)
def test_a_wrong_or_malformed_credential_is_a_401_never_a_500(client, make_link, header):
    make_link(name="q3-plan")
    response = client.get("/-/stats", headers=header)
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"].startswith("Basic realm=")


def test_the_user_name_is_ignored_and_the_password_is_everything_after_the_first_colon(
    db_path, config, make_link, client
):
    make_link(name="q3-plan")
    named = client.get("/-/stats", headers=_basic("alice", config.api_key))
    assert named.status_code == 200, named.text

    colon_config = Config(api_key="a:b:c", db_path=str(db_path))
    with TestClient(create_app(colon_config), follow_redirects=False) as colon_client:
        assert colon_client.get("/-/stats", headers=_basic("", "a:b:c")).status_code == 200
        assert colon_client.get("/-/stats", headers=_basic("a", "b:c")).status_code == 401
        assert colon_client.get("/-/stats", headers=_basic("", "a")).status_code == 401


def test_the_bearer_check_still_ignores_padding_around_the_key(client, config, target):
    """The comparison moved into a helper Basic now shares; Bearer must keep its `.strip()`."""
    padded = client.post(
        "/-/api/links",
        json={"url": target, "name": "padded"},
        headers={"Authorization": f"Bearer  {config.api_key} "},
    )
    assert padded.status_code == 201, padded.text


def test_over_a_real_socket_the_verify_line_of_r010_passes_and_the_blind_cases_fail(
    live_base_url, config, auth
):
    """``curl -sf -u ":$KEY" $BASE/-/stats | grep -q q3-plan``, with httpx as the curl."""
    made = httpx.post(
        f"{live_base_url}/-/api/links",
        json={"url": "https://example.com/plan", "name": "q3-plan"},
        headers=auth,
    )
    assert made.status_code == 201, made.text
    assert httpx.get(f"{live_base_url}/q3-plan", follow_redirects=False).status_code == 302

    page = httpx.get(f"{live_base_url}/-/stats", auth=httpx.BasicAuth("", config.api_key))
    assert page.status_code == 200 and "q3-plan" in page.text

    keyless = httpx.get(f"{live_base_url}/-/stats")
    assert keyless.status_code == 401
    assert keyless.headers["www-authenticate"].startswith("Basic ")


# --- viewing is not following -------------------------------------------------------------


def test_viewing_the_page_leaves_every_daily_count_row_unchanged(
    client, make_link, follow_on, stats_auth, auth, db_path, monkeypatch
):
    make_link(name="one")
    make_link(name="two", url="https://example.org/two")
    make_link(name="unfollowed", url="https://example.net/three")
    follow_on("one", DAY_ONE, times=2)
    follow_on("two", DAY_TWO)

    before = _count_rows(db_path)
    # Not vacuous: two link/day rows exist, so "unchanged" has something to be true of.
    assert [(day, n) for _, day, n in before] == [("2026-09-23", 2), ("2026-09-24", 1)]

    # Move the clock to a day nobody has followed on, so a page that counted anything
    # would either add a row or raise one.
    monkeypatch.setattr(counts, "_epoch_seconds", lambda: DAY_THREE)
    for headers, expected in (
        (stats_auth, 200),
        (stats_auth, 200),
        ({}, 401),
        (auth, 401),
        (_basic("", "wrong"), 401),
    ):
        assert client.get("/-/stats", headers=headers).status_code == expected
    assert client.get("/-/stats?refresh=1", headers=stats_auth).status_code == 200
    client.head("/-/stats", headers=stats_auth)
    client.post("/-/stats", headers=stats_auth)

    assert _count_rows(db_path) == before


# --- which links are listed ---------------------------------------------------------------


def test_a_deleted_link_is_gone_from_the_page_and_an_expired_one_is_marked_and_keeps_its_counts(
    client, make_link, follow_on, stats_auth, auth, monkeypatch
):
    make_link(name="going", url="https://example.com/going")
    follow_on("going", DAY_ONE)
    assert client.delete("/-/api/links/going", headers=auth).status_code == 204

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:00Z")
    make_link(name="short-lived", url="https://example.com/brief", expires="2026-01-01T00:00:01Z")
    make_link(name="for-good", url="https://example.com/forever")
    make_link(name="later", url="https://example.com/later", expires="2030-01-01T00:00:00Z")
    follow_on("short-lived", DAY_ONE)

    # The clock has not reached the expiry yet: the same link is not marked.
    page = _page(client.get("/-/stats", headers=stats_auth))
    live = page.section("short-lived")
    assert "expires 2026-01-01T00:00:01Z" in live["paragraphs"]
    assert not any("expired" in p for p in live["paragraphs"]), live

    monkeypatch.setattr(links_module, "_now", lambda: "2026-01-01T00:00:02Z")
    page = _page(client.get("/-/stats", headers=stats_auth))

    assert "going" not in [s["name"] for s in page.sections]
    assert "going" not in client.get("/-/stats", headers=stats_auth).text
    gone = page.section("short-lived")
    assert "expired 2026-01-01T00:00:01Z" in gone["paragraphs"]
    assert gone["rows"] == [("2026-09-23", 1)]

    # Neither a link with no expiry nor one whose expiry is still ahead is marked.
    assert not any("expire" in p for p in page.section("for-good")["paragraphs"])
    assert "expires 2030-01-01T00:00:00Z" in page.section("later")["paragraphs"]
    assert not any("expired" in p for p in page.section("later")["paragraphs"])


# --- what is written into the page --------------------------------------------------------


def test_everything_written_into_the_page_is_escaped_and_the_page_loads_nothing(
    client, make_link, follow_on, stats_auth, target
):
    hostile_url = "https://example.com/?q=<script>alert(1)</script>&b=\"x\"&c='y'"
    make_link(name="hostile", url=hostile_url, created_by="<b>mallory</b> & co")
    make_link(name="ordinary")
    follow_on("hostile", DAY_ONE)

    response = client.get("/-/stats", headers=stats_auth)
    page = _page(response)

    hostile = page.section("hostile")
    assert hostile_url in hostile["paragraphs"]
    assert "made by <b>mallory</b> & co" in hostile["paragraphs"]

    wire = response.text.lower()
    for forbidden in ("<script", "<b>", "<link", "<img", "<iframe", "<object", "<embed"):
        assert forbidden not in wire, forbidden
    for attribute in ("href=", "src=", "srcset=", "action=", "javascript:"):
        assert attribute not in wire, attribute
    assert not re.search(r"\son[a-z]+\s*=", wire), "an inline event handler"
    assert "@import" not in wire and "url(" not in wire
