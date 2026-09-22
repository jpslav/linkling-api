"""The SQLite connection and the numbered migration runner.

ADR-0005 asks for numbered migrations "from day one", for a reason it states plainly:
SQLite cannot alter a constraint in place, so the first schema change is otherwise
performed by hand on the volume holding the only copy of every link anyone has printed.

Three things this runner refuses to do quietly, because a migration runner that applies
nothing looks exactly like one that had nothing to apply:

- an empty migrations directory raises. Zero migrations is a failure here, never the
  goal: the shipped directory has a fixed, known population.
- a file whose name is not ``NNNN_<slug>.sql`` raises rather than being skipped, so a
  typo cannot make a migration invisible.
- two files sharing a number raise, because their order would then depend on the
  filesystem.

Each migration runs inside its own ``BEGIN IMMEDIATE`` ... ``COMMIT`` written into the
script text. That placement is forced: ``sqlite3.Connection.executescript`` issues an
implicit ``COMMIT`` for any pending transaction before it runs, so a transaction opened
in Python around ``executescript`` would be closed by it. On an error the transaction is
left open by SQLite, so the runner rolls it back and re-raises -- verified by running it,
not by reading the documentation.

Two processes migrating the same fresh database at once are serialised by
``BEGIN IMMEDIATE``, and the loser reads ``schema_version`` inside its own transaction and
finds the work already done: measured as ``applied [1]`` in one and ``applied []`` in the
other, with neither raising. An earlier version of this paragraph said the loser "fails
loudly at startup"; running it is what showed otherwise, and the run also turned up a real
defect in ``connect`` -- see ``_enable_wal``.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_MIGRATION_FILENAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql\Z")

_BUSY_TIMEOUT_MS = 5000

#: How hard to try to put the database into WAL before serving the request anyway.
_WAL_ATTEMPTS = 20
_WAL_RETRY_SECONDS = 0.05


class MigrationError(RuntimeError):
    """The migrations on disk, or the state of the database, are not usable."""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with this service's pragmas.

    ``isolation_level=None`` turns off Python's implicit transaction handling so the
    transactions in this module are the only ones, and are visible in the SQL.

    ``check_same_thread=False`` is required, not a loosening. FastAPI runs a sync
    dependency's setup, the handler and its teardown as three separate threadpool
    submissions, and anyio hands each to whichever worker is free -- so the thread that
    opens the connection is usually *not* the thread that uses it. Sequential traffic
    hides this completely (the pool reuses one worker), which is why a green suite said
    nothing: with two concurrent clients, 161 of 200 follows answered 500 with
    ``sqlite3.ProgrammingError: SQLite objects created in a thread can only be used in
    that same thread``. Each connection here is still owned by exactly one request from
    open to close, which is the property the thread check exists to protect.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    _enable_wal(conn)
    return conn


def _enable_wal(conn: sqlite3.Connection) -> bool:
    """Ask for WAL, retry briefly, and carry on without it rather than fail a request.

    Switching a database into WAL takes a lock that SQLite's busy handler does **not**
    cover, so `busy_timeout` does not help and the ordering of the two pragmas makes no
    difference -- both were measured. Two connections opening at the same moment on a
    fresh file left one raising `sqlite3.OperationalError: database is locked` from
    inside `connect`, which on the redirect path is a 500 for a link that exists.

    ADR-0005 calls WAL "a tuning choice, not a door", and this is what taking that
    seriously looks like: the journal mode is a property of the file, the first
    connection to win sets it for everyone, and a connection that loses the race serves
    the request in the mode the file already has.
    """
    for attempt in range(_WAL_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            return True
        except sqlite3.OperationalError:
            if attempt == _WAL_ATTEMPTS - 1:
                return False
            time.sleep(_WAL_RETRY_SECONDS)
    return False


def discover_migrations(directory: Path | None = None) -> list[tuple[int, Path]]:
    """Every migration in ``directory``, in numeric order. Raises if that is not possible."""
    directory = MIGRATIONS_DIR if directory is None else directory
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")

    found: dict[int, Path] = {}
    for path in sorted(directory.iterdir()):
        if path.name.startswith("."):
            continue
        if not path.is_file():
            raise MigrationError(f"not a file in the migrations directory: {path}")
        match = _MIGRATION_FILENAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(
                f"migration filename does not match NNNN_<slug>.sql: {path.name}"
            )
        version = int(match.group(1))
        if version in found:
            raise MigrationError(
                f"two migrations share number {version:04d}: "
                f"{found[version].name} and {path.name}"
            )
        found[version] = path

    if not found:
        raise MigrationError(
            f"no migrations found in {directory} -- a schema with no migrations is a "
            "runner that would silently do nothing"
        )

    return [(version, found[version]) for version in sorted(found)]


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    """Every migration number this database has recorded as applied.

    The *set*, not the maximum. Tracking only the highest number is how a migration goes
    missing without a word: two branches add `0002` and `0003`, the higher one deploys
    first, and `0002` is then "already done" everywhere -- `migrate` returns the same
    empty list it returns when there is genuinely nothing to do, and the missing table
    surfaces later as a request-time error.
    """
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER)")
    rows = conn.execute("SELECT version FROM schema_version").fetchall()
    return {int(row["version"]) for row in rows if row["version"] is not None}


def current_version(conn: sqlite3.Connection) -> int:
    """The highest number in ``applied_versions``, or 0 on a database with none.

    Reporting only. ``migrate`` decides from the whole set, because the highest number
    is exactly what cannot tell a gap from a finished sequence.
    """
    versions = applied_versions(conn)
    return max(versions) if versions else 0


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """Apply every migration newer than the database's version. Returns what it applied."""
    migrations = discover_migrations(directory)
    already = applied_versions(conn)
    unknown = already - {version for version, _ in migrations}
    if unknown:
        raise MigrationError(
            f"the database records migrations this build does not have: {sorted(unknown)} "
            "-- it was migrated by a newer version of the service. Deploy that version "
            "again, or take a backup and roll the schema back deliberately; this build "
            "will not serve links against a schema it does not know."
        )
    applied: list[int] = []
    for version, path in migrations:
        if version in already:
            continue
        sql = path.read_text(encoding="utf-8")
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n"
            f"INSERT INTO schema_version(version) VALUES ({version});\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"migration {path.name} failed: {exc}") from exc
        applied.append(version)
    return applied
