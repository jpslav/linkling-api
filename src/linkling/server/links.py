"""Storing, finding and tombstoning links.

The one rule here that is a decision rather than an implementation: **deleting a link
does not free its name**. ADR-0005's record settles that a deleted name is reserved
forever and that a generated name is never re-issued, because delete-then-recreate under
the same name is repointing by the back door -- and the poster on the wall would silently
start pointing somewhere else. The mechanism is the tombstone row: the ``links`` row stays
with its ``name``, so the ``UNIQUE`` constraint refuses the name to everybody afterwards,
including the generator.

What a tombstone keeps is a privacy promise as well as a naming one: ADR-0005 says a
tombstone "stores only a name and a date", and ADR-0008c names "a tombstone dropping its
target" as behaviour a retention test asserts. ADR-0005's own DDL declares
``target TEXT NOT NULL``, so the dropped target is written as the empty string -- the only
reading under which both of those sentences are true at once. This is flagged in the
LL-001 report as a question for the owner: if the column should be nullable instead, that
is a change to an unreleased migration today and a table rebuild after the first
deployment.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import names

#: How many times the generator may collide before the service gives up and says so.
#: ADR-0002 puts the chance of at least one collision over the product's life at about
#: one in three, so retrying is required; a silent infinite retry is not.
GENERATE_ATTEMPTS = 10


class NameTaken(Exception):
    """The name already belongs to a link -- live or deleted. Names are never re-used."""


class NameUnknown(Exception):
    """No link has ever had this name."""


class NameDeleted(Exception):
    """The link with this name was deleted. Its name stays reserved."""


class GenerationExhausted(Exception):
    """The generator could not find a free name in GENERATE_ATTEMPTS tries."""


@dataclass(frozen=True)
class Link:
    name: str
    target: str
    deleted: bool


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    target: str,
    created_by: str | None = None,
) -> Link:
    """Create a link under a name the caller chose. Raises NameTaken."""
    try:
        conn.execute(
            "INSERT INTO links(name, target, created_at, created_by) VALUES (?, ?, ?, ?)",
            (name, target, _now(), created_by),
        )
    except sqlite3.IntegrityError as exc:
        raise NameTaken(name) from exc
    return Link(name=name, target=target, deleted=False)


def create_generated(
    conn: sqlite3.Connection,
    *,
    target: str,
    created_by: str | None = None,
    generate: Callable[[], str] | None = None,
    attempts: int = GENERATE_ATTEMPTS,
) -> Link:
    """Create a link under a generated name, retrying past names already taken.

    A tombstoned name is "taken" like any other, which is what makes ADR-0005's "generated
    names are never re-issued" true without a second mechanism.

    ``generate`` is resolved here rather than bound as a default argument, so that a test
    can replace ``names.generate`` and drive the retry through the running service instead
    of calling this function directly.
    """
    make_name = names.generate if generate is None else generate
    for _ in range(attempts):
        candidate = make_name()
        try:
            return create(conn, name=candidate, target=target, created_by=created_by)
        except NameTaken:
            continue
    raise GenerationExhausted(
        f"no free generated name in {attempts} attempts -- refusing to re-issue a name"
    )


def lookup(conn: sqlite3.Connection, name: str) -> Link | None:
    """The link stored under ``name``, tombstone included, or None if there never was one."""
    row = conn.execute(
        "SELECT name, target, deleted_at FROM links WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    return Link(
        name=row["name"], target=row["target"], deleted=row["deleted_at"] is not None
    )


def delete(conn: sqlite3.Connection, name: str) -> None:
    """Tombstone the link: drop its target and maker, keep its name reserved forever."""
    link = lookup(conn, name)
    if link is None:
        raise NameUnknown(name)
    if link.deleted:
        raise NameDeleted(name)
    conn.execute(
        "UPDATE links SET target = '', created_by = NULL, deleted_at = ? "
        "WHERE name = ? AND deleted_at IS NULL",
        (_now(), name),
    )
