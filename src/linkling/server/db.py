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

import os
import re
import sqlite3
import struct
import time
import threading
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_MIGRATION_FILENAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql\Z")

_BUSY_TIMEOUT_MS = 5000

#: How hard to try to put the database into WAL before serving the request anyway.
_WAL_ATTEMPTS = 20
_WAL_RETRY_SECONDS = 0.05


#: Counts at which SQLite's record format stores an integer in more bytes than the count
#: before it (https://sqlite.org/fileformat2.html, "Serial Type Codes"): 1 is stored in no
#: bytes, 2..127 in one, 128..32767 in two, and so on. At these counts the counter's cell
#: grows and moves; at every other count SQLite overwrites it in place (measured: 2 to 3
#: and 3 to 4 left every cell offset where it was). A count of 1 is a new row. ADR-0021.
RELAYOUT_COUNTS = frozenset({1, 2, 128, 32768, 8388608, 2147483648, 140737488355328})

#: What the header's file change counter and version-valid-for (offsets 24 and 92) are set
#: back to after a canonicalisation. Any constant works; what matters is that it is not a
#: tally of past writes. ADR-0021.
_CHANGE_COUNTER = 1


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

    ``secure_delete`` zeroes freed bytes (ADR-0021), and ``temp_store=MEMORY`` keeps the
    temporary tables SQLite builds out of files in the temp directory.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA secure_delete=ON")
    conn.execute("PRAGMA temp_store=MEMORY")
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


def settle(conn: sqlite3.Connection, *, relayout: bool) -> None:
    """Leave the files holding the database's content and nothing about how it got there.

    Every write path calls this after it commits (ADR-0021). ``relayout`` says whether the
    write could have moved a cell: a create, a delete, or a follow whose new count is in
    ``RELAYOUT_COUNTS``. Such a write is followed by ``canonicalise``, because SQLite places
    a new or grown cell in the page's free space, so byte offsets would give back the order
    of each day's first follows. Every write is then checkpointed with ``TRUNCATE``, which
    copies the WAL into the database and cuts the ``-wal`` to 0 bytes, so no frame of an
    earlier write survives it.

    A checkpoint that another connection's open read holds up comes back busy and leaves
    the frames: measured as ``(1, 1, 0)`` and a 4,152-byte ``-wal``. The service holds one
    connection at a time (``app.get_conn``), so only a process outside it can cause that.
    The rewrite is then owed (``_deferred``) until one lands: a rewrite that raises stays
    owed too, and ``settle_owed`` lets the next request that opens the database pay it.
    """
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    relayout = relayout or path in _deferred
    if relayout:
        # Owed from here until a rewrite has landed, so a raise below leaves it owed.
        _deferred.add(path)
        if not _checkpoint(conn):
            # Another process holds a read open. A rewrite now would put a copy of every
            # page into a WAL that cannot be emptied until it ends: the -wal of a 2.4 MB
            # file grew by 2.4 MB per create (review round 2). A later request rewrites.
            return
        canonicalise(conn)
    if not _checkpoint(conn):
        return
    if relayout:
        _reset_change_counter(conn)
        _deferred.discard(path)


def settle_owed(conn: sqlite3.Connection) -> None:
    """Do a rewrite an earlier request had to put off, on the next request that opens the database.

    ``app.get_conn`` calls this before it closes every connection, reads included (a
    request refused before it takes a connection, or ``/-/privacy.json``, opens none). Without
    it an owed rewrite waited for the next write: once the foreign reader had gone, a
    read's closing checkpoint moved the un-rewritten pages into ``linkling.db`` and they
    stayed there, however long the next write took (review round 3, measured).
    """
    if not _deferred:
        return
    if conn.execute("PRAGMA database_list").fetchone()["file"] in _deferred:
        settle(conn, relayout=False)


#: Database files owed a rewrite: one was put off because another process held a read
#: open, or it raised. Cleared only when a rewrite has landed.
_deferred: set[str] = set()


def _checkpoint(conn: sqlite3.Connection) -> bool:
    """``wal_checkpoint(TRUNCATE)``, without waiting. True when the ``-wal`` is left empty.

    With the connection's usual busy timeout the checkpoint waits the whole 5 s for an open
    reader before giving up, while the service's lock queues every other request behind
    it: 8 requests took 21.5 s against a foreign ``BEGIN; SELECT`` (review round 2). A
    reader the checkpoint cannot pass is not going to end on this request's account, so it
    gives up at once; a later request's settle tries again.
    """
    conn.execute("PRAGMA busy_timeout=0")
    try:
        busy, frames, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    # Outside WAL mode (``_enable_wal`` lost its race) SQLite answers (0, -1, -1): there is
    # no WAL to empty, which is not a reason to put off a rewrite.
    return busy == 0 and frames in (0, -1)


def canonicalise(conn: sqlite3.Connection) -> None:
    """Rewrite the database file so its bytes depend only on its content (ADR-0021).

    ``VACUUM`` rebuilds every table in key order into fresh pages and copies them back
    over the file while holding the write lock for the whole rebuild, so no other writer
    can commit in between and be overwritten. Two databases with the same rows, written in
    different orders, come out byte-identical except for the header counters, which the
    rest of this function and ``_reset_change_counter`` put back.

    ``VACUUM`` raises the schema cookie by one. The cookie is set back only when
    ``sqlite_schema`` -- every name, root page and statement -- is unchanged, so any
    connection that cached the schema at that cookie still holds a correct one. Otherwise
    the cookie would count canonicalisations, and through them the days a since-deleted
    link was followed.
    """
    schema_sql = "SELECT type, name, tbl_name, rootpage, sql FROM sqlite_schema ORDER BY rowid"
    schema = [tuple(row) for row in conn.execute(schema_sql)]
    cookie = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    conn.execute("VACUUM")
    if [tuple(row) for row in conn.execute(schema_sql)] == schema:
        conn.execute(f"PRAGMA schema_version={cookie}")


#: One descriptor per database file, opened once and never closed, for
#: ``_reset_change_counter``. POSIX record locks belong to the process, and closing *any*
#: descriptor on a file drops every lock the process holds on it -- SQLite's included. So
#: the file is never closed here. Keyed by the file's device and inode.
_header_fds: dict[tuple[int, int], int] = {}
_header_fds_lock = threading.Lock()


def _header_fd(path: str) -> int:
    st = os.stat(path)
    key = (st.st_dev, st.st_ino)
    with _header_fds_lock:
        fd = _header_fds.get(key)
        if fd is None:
            fd = os.open(path, os.O_RDWR)
            _header_fds[key] = fd
        return fd


def _reset_change_counter(conn: sqlite3.Connection) -> None:
    """Put the header's change counter back to a constant after a canonicalisation.

    SQL cannot do this, so it is 8 bytes written to the file: offsets 24 and 92, the file
    change counter and version-valid-for (https://sqlite.org/fileformat2.html). In WAL
    mode ordinary writes leave the counter alone -- 2 after 300 follows, measured -- but a
    commit that rewrites page 1 on a fresh connection writes the counter it last read plus
    one, and ``VACUUM`` always rewrites page 1. Left alone, it counted canonicalisations:
    history A and history B, differing in how often a since-deleted link was followed,
    ended at 152 and 153 (measured before this function existed, on the prototype of
    LL-033). Removing it now turns ``test_leak_1_…`` red in all three copy cases.

    Why this is safe: "In WAL mode, changes to the database are detected using the
    wal-index and so the change counter is not needed" (the same page). A second
    connection with a warm page cache, open across a reset, read each of 5 later writes
    correctly and ``integrity_check`` said ``ok``. The write happens only in WAL mode and
    only after a checkpoint that left the WAL empty. It is made inside ``BEGIN IMMEDIATE``,
    which keeps other writers in the WAL out while it happens, through a descriptor that
    is never closed (``_header_fds``): closing one would release the locks SQLite holds on
    the file, and with them the guarantee that no other process deletes the ``-wal`` or
    changes the journal mode under this connection.
    """
    if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
        return
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    counter = struct.pack(">I", _CHANGE_COUNTER)
    conn.execute("BEGIN IMMEDIATE")
    try:
        fd = _header_fd(path)
        os.pwrite(fd, counter, 24)
        os.pwrite(fd, counter, 92)
        os.fsync(fd)
    finally:
        conn.execute("COMMIT")


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
