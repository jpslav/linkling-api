"""LL-015 -- the privacy manifest is served as shipped, and two tests keep it true.

ADR-0008 §c names the two tests, and says "both must guard against emptiness":

1. **The schema test** introspects a freshly migrated database and compares its
   ``(table, column)`` set with the manifest's, in both directions. An unmigrated database
   and a missing manifest agree on nothing, so either side coming back empty fails with a
   message beginning ``blind:`` rather than passing.
2. **The retention test** reads each column's ``on_delete`` and ``on_expiry`` claim out of
   the manifest and checks that claim against what deleting and expiring a real link do.
   It hard-codes no column's behaviour, so a manifest edit that claims something the code
   does not do fails it, and so does a code change the manifest does not describe. It
   checks every column the manifest declares, and it guards against emptiness three ways:
   a manifest with no columns, a table holding no rows for the link before the event, and
   a table it cannot relate to a link each fail it rather than pass.

Adding a column therefore forces a manifest entry (test 1), and that entry forces a
retention claim that test 2 enforces. Nobody has to remember to update the file.

The vocabulary for ``on_delete`` and ``on_expiry`` is ADR-0020's:

- ``kept``: the value is unchanged.
- ``set``: there was no value before and there is one after.
- ``emptied``: a non-empty value became the empty string.
- ``nulled``: a value became NULL.
- ``removed``: the link's rows in that table are gone.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from linkling.server import db, privacy
from linkling.server import links as links_module


#: How to find one link's rows in each table. ``None`` means the table is not about any
#: one link, so the retention test checks the whole table. A new table fails the schema
#: test until the manifest declares it, and a declared table missing here then fails the
#: retention test by name, which is what makes whoever adds it say how its rows relate to
#: a link.
LINK_ROWS = {
    "links": "id = ?",
    "daily_counts": "link_id = ?",
    "schema_version": None,
}

#: The one column a live link has no value in. Every other column of the link's rows must
#: hold a value before the event, so that ``kept`` is never passed by NULL staying NULL.
#: A column added later that the fixture leaves NULL fails the retention test until the
#: fixture fills it or it is named here.
NULL_ON_A_LIVE_LINK = {("links", "deleted_at")}

#: Far enough ahead that the real clock never reaches it, so the link is live until the
#: test moves ``links._now`` past it.
FAR_FUTURE = "2999-01-01T00:00:00Z"
PAST_FAR_FUTURE = "2999-01-01T00:00:01Z"


# --- reading the two sides --------------------------------------------------------------


def load_manifest() -> dict:
    try:
        raw = privacy.manifest_bytes()
    except FileNotFoundError as exc:
        pytest.fail(f"blind: the manifest could not be read ({exc}), so nothing was compared")
    return json.loads(raw)


def manifest_columns(manifest: dict) -> set[tuple[str, str]]:
    return {
        (table, column)
        for table, spec in manifest.get("tables", {}).items()
        for column in spec.get("columns", {})
    }


def schema_columns(conn: sqlite3.Connection) -> set[tuple[str, str]]:
    """Every column of every table except SQLite's own ``sqlite_*`` ones."""
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite\\_%' "
            "ESCAPE '\\'"
        )
    ]
    return {
        (table, row[1])
        for table in tables
        for row in conn.execute(f"PRAGMA table_xinfo({table})")
    }


def compare(schema: set, manifest: set) -> None:
    """Fail unless the two sets are equal, and fail as ``blind:`` if either is empty."""
    if not schema:
        pytest.fail("blind: the database has no tables, so the manifest was compared with nothing")
    if not manifest:
        pytest.fail("blind: the manifest declares no columns, so the schema was compared with nothing")
    assert schema == manifest, (
        f"stored but not declared in the manifest: {sorted(schema - manifest)}; "
        f"declared in the manifest but not stored: {sorted(manifest - schema)}"
    )


@pytest.fixture
def migrated(tmp_path):
    conn = db.connect(tmp_path / "schema.db")
    try:
        db.migrate(conn)
        yield conn
    finally:
        conn.close()


# --- the route --------------------------------------------------------------------------


def test_the_route_serves_the_manifest_byte_for_byte(client):
    response = client.get("/-/privacy.json")
    assert response.status_code == 200
    assert response.content == privacy.manifest_bytes()
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_head_answers_like_get_without_a_body(client):
    response = client.head("/-/privacy.json")
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-type"] == "application/json"


def test_fetching_the_manifest_writes_nothing(client, make_link, db_path):
    """It is not a link, so fetching it must not count as a follow of anything."""
    make_link(name="privacy")
    assert client.get("/-/privacy.json").status_code == 200
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT count(*) FROM daily_counts").fetchone()[0] == 0
    finally:
        conn.close()


# --- 1. the schema test -----------------------------------------------------------------


def test_the_manifest_declares_every_stored_column_and_no_other(migrated):
    compare(schema_columns(migrated), manifest_columns(load_manifest()))


def test_every_declared_column_says_why_and_for_how_long():
    manifest = load_manifest()
    assert manifest.get("format") == 1
    assert manifest_columns(manifest), "blind: the manifest declares no columns"
    for table, spec in manifest["tables"].items():
        assert spec.get("purpose", "").strip(), f"{table} has no purpose"
        for column, claim in spec["columns"].items():
            where = f"{table}.{column}"
            assert claim.get("purpose", "").strip(), f"{where} has no purpose"
            assert claim.get("retention", "").strip(), f"{where} has no retention"
            for event in ("on_delete", "on_expiry"):
                assert claim.get(event) in privacy.EFFECTS, f"{where} {event}={claim.get(event)!r}"


def test_the_comparison_is_blind_when_the_database_is_unmigrated(tmp_path):
    conn = db.connect(tmp_path / "unmigrated.db")
    try:
        schema = schema_columns(conn)
    finally:
        conn.close()
    with pytest.raises(pytest.fail.Exception, match="^blind:"):
        compare(schema, manifest_columns(load_manifest()))


def test_the_comparison_is_blind_when_the_manifest_declares_nothing(migrated):
    with pytest.raises(pytest.fail.Exception, match="^blind:"):
        compare(schema_columns(migrated), manifest_columns({}))


def test_loading_is_blind_when_the_manifest_is_missing(monkeypatch):
    def missing():
        raise FileNotFoundError("what-we-store.json")

    monkeypatch.setattr(privacy, "manifest_bytes", missing)
    with pytest.raises(pytest.fail.Exception, match="^blind:"):
        load_manifest()


def test_the_comparison_names_each_sides_extras():
    with pytest.raises(AssertionError) as caught:
        compare({("links", "id"), ("links", "new")}, {("links", "id"), ("links", "old")})
    assert "('links', 'new')" in str(caught.value)
    assert "('links', 'old')" in str(caught.value)


# --- 2. the retention test --------------------------------------------------------------


def _link_id(db_path, name: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT id FROM links WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


def _rows_of(db_path, link_id: int, tables) -> dict[str, list[dict]]:
    """Each table's rows for one link (or the whole table), in primary-key order."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = {}
        for table in tables:
            if table not in LINK_ROWS:
                pytest.fail(
                    f"the retention test does not know which rows of {table!r} belong to a "
                    "link; add it to LINK_ROWS"
                )
            info = conn.execute(f"PRAGMA table_info({table})").fetchall()
            keys = [r["name"] for r in sorted(info, key=lambda r: r["pk"]) if r["pk"]]
            order = ", ".join(keys or [r["name"] for r in info])
            where = LINK_ROWS[table]
            if where is None:
                rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall()
            else:
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE {where} ORDER BY {order}", (link_id,)
                ).fetchall()
            snapshot[table] = [dict(row) for row in rows]
        return snapshot
    finally:
        conn.close()


def _check_claims(manifest: dict, event: str, before: dict, after: dict) -> set:
    """Check every column's ``event`` claim; return the ``(table, column)`` pairs checked."""
    checked = set()
    for table, spec in manifest["tables"].items():
        rows_before, rows_after = before[table], after[table]
        assert rows_before, f"blind: {table} held no rows for the link before {event}"
        claims = {column: claim[event] for column, claim in spec["columns"].items()}

        if "removed" in claims.values():
            assert set(claims.values()) == {"removed"}, (
                f"{table}: rows are removed or they are not; {event} claims {claims}"
            )
            assert rows_after == [], f"{table}: {event} claims removed, rows remain: {rows_after}"
            checked |= {(table, column) for column in claims}
            continue

        assert len(rows_after) == len(rows_before), (
            f"{table}: {event} claims no removal, but {len(rows_before)} rows became "
            f"{len(rows_after)}"
        )
        for column, claim in claims.items():
            for old_row, new_row in zip(rows_before, rows_after):
                old, new = old_row[column], new_row[column]
                where = f"{table}.{column} ({event}: {claim!r}, was {old!r}, now {new!r})"
                if (table, column) not in NULL_ON_A_LIVE_LINK:
                    assert old is not None, f"the fixture left {table}.{column} NULL"
                if claim == "kept":
                    assert new == old, where
                elif claim == "set":
                    assert old is None and new is not None, where
                elif claim == "emptied":
                    assert old not in (None, "") and new == "", where
                elif claim == "nulled":
                    assert old is not None and new is None, where
                else:
                    pytest.fail(f"unknown claim {where}")
            checked.add((table, column))
    return checked


def _live_link(make_link, client, db_path, name: str) -> int:
    make_link(url=f"https://example.com/{name}", name=name, created_by=f"maker of {name}",
              expires=FAR_FUTURE)
    assert client.get(f"/{name}").status_code == 302
    return _link_id(db_path, name)


def test_deleting_a_link_does_to_each_column_what_the_manifest_says(
    client, make_link, auth, db_path
):
    manifest = load_manifest()
    tables = manifest["tables"]
    doomed = _live_link(make_link, client, db_path, "doomed")
    bystander = _live_link(make_link, client, db_path, "bystander")
    before = _rows_of(db_path, doomed, tables)
    bystander_before = _rows_of(db_path, bystander, tables)

    assert client.delete("/-/api/links/doomed", headers=auth).status_code == 204
    after = _rows_of(db_path, doomed, tables)

    checked = _check_claims(manifest, "on_delete", before, after)
    assert checked, "blind: the manifest declares no columns, so no claim was checked"
    assert _rows_of(db_path, bystander, tables) == bystander_before, (
        "deleting one link changed another link's rows"
    )


def test_expiring_a_link_does_to_each_column_what_the_manifest_says(
    client, make_link, db_path, monkeypatch
):
    manifest = load_manifest()
    tables = manifest["tables"]
    expiring = _live_link(make_link, client, db_path, "expiring")
    before = _rows_of(db_path, expiring, tables)

    monkeypatch.setattr(links_module, "_now", lambda: PAST_FAR_FUTURE)
    assert client.get("/expiring").status_code == 410
    after = _rows_of(db_path, expiring, tables)

    checked = _check_claims(manifest, "on_expiry", before, after)
    assert checked, "blind: the manifest declares no columns, so no claim was checked"
