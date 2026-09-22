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
``BEGIN IMMEDIATE``; the loser then tries to apply a migration that is already there and
fails loudly at startup rather than half-applying one. That is the intended outcome for a
single-worker service: noisy, not clever.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_MIGRATION_FILENAME = re.compile(r"(\d{4})_[a-z0-9_]+\.sql\Z")

_BUSY_TIMEOUT_MS = 5000


class MigrationError(RuntimeError):
    """The migrations on disk, or the state of the database, are not usable."""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with this service's pragmas.

    ``isolation_level=None`` turns off Python's implicit transaction handling so the
    transactions in this module are the only ones, and are visible in the SQL.
    """
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # tuning, not a door -- ADR-0005 Consequences
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


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


def current_version(conn: sqlite3.Connection) -> int:
    """The highest applied migration number, or 0 on a database with none."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version(version INTEGER)")
    row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
    return 0 if row is None or row["v"] is None else int(row["v"])


def migrate(conn: sqlite3.Connection, directory: Path | None = None) -> list[int]:
    """Apply every migration newer than the database's version. Returns what it applied."""
    migrations = discover_migrations(directory)
    applied: list[int] = []
    for version, path in migrations:
        if version <= current_version(conn):
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
