"""Storing, finding and tombstoning links.

The one rule here that is a decision rather than an implementation: **deleting a link
does not free its name**. ADR-0005's record settles that a deleted name is reserved
forever and that a generated name is never re-issued, because delete-then-recreate under
the same name is repointing by the back door -- and the poster on the wall would silently
start pointing somewhere else. The mechanism is the tombstone row: the ``links`` row stays
with its ``name``, so the ``UNIQUE`` constraint refuses the name to everybody afterwards,
including the generator.

What a tombstone keeps is a privacy promise as well as a naming one: ADR-0008c names "a
tombstone dropping its target" as behaviour a retention test asserts, and ADR-0005's DDL
declares ``target TEXT NOT NULL``, so the dropped target is written as the empty string.
Whether the column should be nullable instead was put to the owner and decided on
2026-09-25, by ``pm-3`` under the owner's line: the empty string stands. ADR-0005's
consequence sentence now says so (LL-033). Deleting also rewrites the database file
(``db.settle``, ADR-0021), so the old target is not left behind in freed space either --
once the rewrite has landed, which another process holding a read open can put off
(ADR-0021, "What stays").
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from . import db, names

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
    expired: bool


#: The one shape every timestamp this service stores or accepts is written in. A single
#: constant here, rather than a copy beside each user, is what keeps `_now()`'s output and
#: `app.py`'s `expires` validation from drifting into two shapes that happen to agree today.
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)


def create(
    conn: sqlite3.Connection,
    *,
    name: str,
    target: str,
    created_by: str | None = None,
    expires_at: str | None = None,
    created_at: str | None = None,
) -> Link:
    """Create a link under a name the caller chose. Raises NameTaken.

    ``created_at`` defaults to ``_now()`` here, but a caller that already read the clock
    for another reason -- ``app.py`` reads it once to check ``expires`` is still in the
    future -- should pass that same value through rather than let this call read it again.
    Two separate reads let a second tick land between them: an `expires` a caller chose to
    be one second in the future could pass validation against the first read and still be
    equal to, or earlier than, `created_at` from the second, landing already-expired.
    """
    when = _now() if created_at is None else created_at
    try:
        conn.execute(
            "INSERT INTO links(name, target, created_at, created_by, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, target, when, created_by, expires_at),
        )
    except sqlite3.IntegrityError as exc:
        raise NameTaken(name) from exc
    # A new row is a new cell: rewrite the file so its layout says nothing of the order
    # writes arrived in (ADR-0021).
    db.settle(conn, relayout=True)
    return Link(name=name, target=target, deleted=False, expired=False)


def create_generated(
    conn: sqlite3.Connection,
    *,
    target: str,
    created_by: str | None = None,
    expires_at: str | None = None,
    created_at: str | None = None,
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
            return create(
                conn,
                name=candidate,
                target=target,
                created_by=created_by,
                expires_at=expires_at,
                created_at=created_at,
            )
        except NameTaken:
            continue
    raise GenerationExhausted(
        f"no free generated name in {attempts} attempts -- refusing to re-issue a name"
    )


def is_expired(expires_at: str | None) -> bool:
    """Whether a link with this ``expires_at`` is expired right now.

    One definition, used by ``lookup`` and by the stats page, so the page marks a link
    expired by the same rule a follow uses to answer it 410. Each call reads the clock
    afresh, so a link can turn expired between one call and the next.

    A link expiring at exactly ``_now()`` is expired: ``expires_at`` is compared with
    ``<=``, not ``<``, matching RFC 7519 SS4.1.4's own
    ``exp`` claim -- "the expiration time on or after which the JWT MUST NOT be accepted"
    -- rather than RFC 6265 SS5.3's cookie, which reads the opposite way ("'expired' if the
    cookie has an expiry date in the past", so a cookie is still good exactly at its own
    Expires instant). ISO-8601 UTC in this exact shape (``TIMESTAMP_FORMAT``) sorts
    lexicographically in time order, so the comparison below is a plain Python string
    compare against ``_now()`` -- no parsing, no SQL function.
    """
    return expires_at is not None and expires_at <= _now()


def lookup(conn: sqlite3.Connection, name: str) -> Link | None:
    """The link stored under ``name``, tombstone included, or None if there never was one.

    ``expired`` is decided here (``is_expired``, against ``_now()``) rather than stored: a
    link is either past its ``expires_at`` or it is not, at the moment it is looked up, and
    there is no column to write it into.
    """
    row = conn.execute(
        "SELECT name, target, deleted_at, expires_at FROM links WHERE name = ?", (name,)
    ).fetchone()
    if row is None:
        return None
    return Link(
        name=row["name"],
        target=row["target"],
        deleted=row["deleted_at"] is not None,
        expired=is_expired(row["expires_at"]),
    )


def delete(conn: sqlite3.Connection, name: str) -> None:
    """Tombstone the link: drop its target and maker, keep its name reserved forever."""
    cursor = conn.execute(
        "UPDATE links SET target = '', created_by = NULL, deleted_at = ? "
        "WHERE name = ? AND deleted_at IS NULL",
        (_now(), name),
    )
    if cursor.rowcount == 1:
        # The tombstone shrinks the row and the trigger frees the link's count cells.
        # Rewriting the file leaves no trace of either in freed space, not even the
        # lengths of what was freed (ADR-0021).
        db.settle(conn, relayout=True)
        return
    # The UPDATE is what decides, so two callers racing on one name cannot both be told
    # they deleted it: the loser's rowcount is 0 and it reads the row to say which "no"
    # this is. Looking first and updating second would hand 204 to everybody in the race.
    link = lookup(conn, name)
    if link is None:
        raise NameUnknown(name)
    raise NameDeleted(name)
