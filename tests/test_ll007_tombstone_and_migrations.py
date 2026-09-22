"""LL-007 -- a deleted name is never re-issued, and migrations are numbered and ordered.

Two halves of one *Done when*: "a numbered migration mechanism exists before the second
migration is needed, and a deleted name cannot be re-issued".
"""

from __future__ import annotations

import sqlite3

import pytest

from linkling.server import db, links, names

OTHER_TARGET = "https://example.com/somewhere/else"


def test_a_deleted_name_cannot_be_re_issued(client, auth, make_link, target):
    make_link(name="q3-plan")
    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204
    assert client.get("/q3-plan").status_code == 410

    again = client.post(
        "/-/api/links", json={"url": OTHER_TARGET, "name": "q3-plan"}, headers=auth
    )
    assert again.status_code == 409, again.text

    # The poster on the wall still does not point at OTHER_TARGET, which is the whole
    # reason the name is reserved.
    still_gone = client.get("/q3-plan")
    assert still_gone.status_code == 410
    assert "location" not in still_gone.headers


def test_deleting_twice_says_gone_rather_than_pretending(client, auth, make_link):
    make_link(name="q3-plan")
    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204
    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 410
    assert client.delete("/-/api/links/never-existed", headers=auth).status_code == 404


def test_a_tombstone_keeps_only_its_name_and_its_dates(client, auth, make_link, db_path):
    make_link(name="q3-plan", created_by="jp")
    assert client.delete("/-/api/links/q3-plan", headers=auth).status_code == 204

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM links WHERE name = 'q3-plan'").fetchone()

    assert row is not None, "the row was removed -- then the name would be free again"
    assert row["target"] == "", "the tombstone still carries where the link pointed"
    assert row["created_by"] is None, "the tombstone still carries who made it"
    assert row["deleted_at"] is not None
    assert row["created_at"] is not None


def test_a_generated_name_is_not_re_issued_after_its_link_is_deleted(
    client, auth, monkeypatch, target
):
    """Drive the retry through the API by replacing the generator the service calls."""
    offered = iter(["bcdfgh", "bcdfgh", "jklmnp"])
    monkeypatch.setattr(names, "generate", lambda: next(offered))

    first = client.post("/-/api/links", json={"url": target}, headers=auth)
    assert first.status_code == 201, first.text
    assert first.json()["name"] == "bcdfgh"

    assert client.delete("/-/api/links/bcdfgh", headers=auth).status_code == 204

    second = client.post("/-/api/links", json={"url": OTHER_TARGET}, headers=auth)
    assert second.status_code == 201, second.text
    assert second.json()["name"] == "jklmnp", "the tombstoned name was handed out again"
    assert client.get("/bcdfgh").status_code == 410


def test_the_generator_gives_up_rather_than_re_issue(client, auth, monkeypatch, target):
    """Invert the double: a generator that can only ever offer a taken name must fail.

    This is what shows the previous test is wired to the retry and not to the stub's
    third value: with no free name on offer the service refuses, and the tombstoned name
    stays gone.
    """
    monkeypatch.setattr(names, "generate", lambda: "bcdfgh")

    first = client.post("/-/api/links", json={"url": target}, headers=auth)
    assert first.status_code == 201, first.text
    assert client.delete("/-/api/links/bcdfgh", headers=auth).status_code == 204

    second = client.post("/-/api/links", json={"url": OTHER_TARGET}, headers=auth)
    assert second.status_code == 503, second.text
    assert client.get("/bcdfgh").status_code == 410


# --- the migration runner ---------------------------------------------------------


def _write(directory, name, sql):
    (directory / name).write_text(sql, encoding="utf-8")


def test_connecting_while_another_writer_holds_the_lock_still_succeeds(tmp_path):
    """Putting a database into WAL takes a lock SQLite's busy handler does not cover.

    Every request opens its own connection, so a connection that raises here is a 500 on
    a link that exists. This holds the lock deliberately rather than racing for it: a
    two-thread version of this test passed against the broken code about as often as not,
    which is the same defect as no test at all.
    """
    database = tmp_path / "locked.db"
    sqlite3.connect(database).execute("CREATE TABLE placeholder(x)")  # a delete-mode file

    holder = sqlite3.connect(database, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO placeholder VALUES (1)")
    try:
        conn = db.connect(database)  # must not raise, WAL or no WAL
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode in {"delete", "wal"}, mode
            assert conn.execute("SELECT 1").fetchone()[0] == 1
        finally:
            conn.close()
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def test_the_journal_mode_is_wal_when_nothing_is_in_the_way(tmp_path):
    """The tolerance above must not have quietly turned WAL off for everybody."""
    conn = db.connect(tmp_path / "plain.db")
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_the_shipped_migrations_are_a_non_empty_numbered_sequence():
    """Zero shipped migrations is a failure, not a goal: this population is fixed."""
    found = db.discover_migrations()
    assert len(found) >= 1, "no migrations ship with the service"
    numbers = [version for version, _ in found]
    assert numbers == sorted(numbers)
    assert numbers[0] == 1


def test_migrations_apply_in_numeric_order(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    _write(directory, "0001_first.sql", "CREATE TABLE a(x INTEGER);")
    # Depends on 0001 having run: applied in the wrong order, SQLite has no table `a`.
    _write(directory, "0002_second.sql", "ALTER TABLE a ADD COLUMN y INTEGER;")

    conn = db.connect(tmp_path / "ordered.db")
    try:
        assert db.migrate(conn, directory) == [1, 2]
        columns = [row[1] for row in conn.execute("PRAGMA table_info(a)")]
        assert columns == ["x", "y"]
        assert db.current_version(conn) == 2
    finally:
        conn.close()


def test_a_second_run_applies_nothing(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    _write(directory, "0001_first.sql", "CREATE TABLE a(x INTEGER);")

    database = tmp_path / "twice.db"
    first = db.connect(database)
    try:
        assert db.migrate(first, directory) == [1]
    finally:
        first.close()

    second = db.connect(database)
    try:
        # `0001_first.sql` has no IF NOT EXISTS, so a re-application would raise rather
        # than quietly succeed -- the assertion is that it is not even attempted.
        assert db.migrate(second, directory) == []
        assert db.current_version(second) == 1
        rows = second.execute("SELECT count(*) FROM schema_version").fetchone()[0]
        assert rows == 1
    finally:
        second.close()


def test_a_migration_numbered_below_the_current_version_is_still_applied(tmp_path):
    """The runner tracks the set of applied versions, not the highest one.

    Two branches adding `0002` and `0003` is the ordinary way this happens: if the higher
    number deploys first, a runner keyed on `MAX(version)` reports "nothing to do" for the
    lower one forever, and the missing table turns up as a request-time error.
    """
    directory = tmp_path / "migrations"
    directory.mkdir()
    _write(directory, "0001_first.sql", "CREATE TABLE a(x INTEGER);")
    _write(directory, "0003_third.sql", "CREATE TABLE c(x INTEGER);")

    database = tmp_path / "gap.db"
    first = db.connect(database)
    try:
        assert db.migrate(first, directory) == [1, 3]
    finally:
        first.close()

    _write(directory, "0002_second.sql", "CREATE TABLE b(x INTEGER);")
    second = db.connect(database)
    try:
        assert db.migrate(second, directory) == [2], "0002 was silently skipped"
        tables = {row[0] for row in second.execute("SELECT name FROM sqlite_master")}
        assert {"a", "b", "c"} <= tables, tables
        assert db.applied_versions(second) == {1, 2, 3}
    finally:
        second.close()


def test_a_database_migrated_by_a_newer_build_refuses_to_start(tmp_path):
    """Recorded version 9 with no 0009 file is a downgrade, and downgrades say so."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    _write(directory, "0001_first.sql", "CREATE TABLE a(x INTEGER);")

    conn = db.connect(tmp_path / "newer.db")
    try:
        assert db.migrate(conn, directory) == [1]
        conn.execute("INSERT INTO schema_version(version) VALUES (9)")
        with pytest.raises(db.MigrationError, match="newer version"):
            db.migrate(conn, directory)
    finally:
        conn.close()


def test_an_empty_migrations_directory_raises_rather_than_reporting_success(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    conn = db.connect(tmp_path / "empty.db")
    try:
        with pytest.raises(db.MigrationError, match="no migrations found"):
            db.migrate(conn, directory)
    finally:
        conn.close()


def test_a_missing_migrations_directory_raises(tmp_path):
    conn = db.connect(tmp_path / "missing.db")
    try:
        with pytest.raises(db.MigrationError, match="not found"):
            db.migrate(conn, tmp_path / "nowhere")
    finally:
        conn.close()


def test_a_migration_that_is_not_numbered_raises(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    _write(directory, "0001_first.sql", "CREATE TABLE a(x INTEGER);")
    _write(directory, "add_column.sql", "ALTER TABLE a ADD COLUMN y INTEGER;")
    with pytest.raises(db.MigrationError, match="NNNN_"):
        db.discover_migrations(directory)


def test_two_migrations_sharing_a_number_raise(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    _write(directory, "0001_first.sql", "CREATE TABLE a(x INTEGER);")
    _write(directory, "0001_also_first.sql", "CREATE TABLE b(x INTEGER);")
    with pytest.raises(db.MigrationError, match="share number"):
        db.discover_migrations(directory)


def test_a_failing_migration_leaves_nothing_behind(tmp_path):
    directory = tmp_path / "migrations"
    directory.mkdir()
    _write(
        directory,
        "0001_first.sql",
        "CREATE TABLE a(x INTEGER);\nINSERT INTO nope VALUES (1);",
    )
    conn = db.connect(tmp_path / "rollback.db")
    try:
        with pytest.raises(db.MigrationError):
            db.migrate(conn, directory)
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master")]
        assert "a" not in tables, "half a migration was left applied"
        assert db.current_version(conn) == 0
    finally:
        conn.close()


def test_the_store_refuses_a_name_that_is_already_a_tombstone(tmp_path):
    """The `UNIQUE` constraint, not a code path, is what reserves the name."""
    conn = db.connect(tmp_path / "store.db")
    try:
        db.migrate(conn)
        links.create(conn, name="q3-plan", target="https://example.com/a")
        links.delete(conn, "q3-plan")
        with pytest.raises(links.NameTaken):
            links.create(conn, name="q3-plan", target="https://example.com/b")
    finally:
        conn.close()
