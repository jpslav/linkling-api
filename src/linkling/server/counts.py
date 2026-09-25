"""Counting follows: one number per link per UTC day, and nothing else (ADR-0004).

This module is handed a link's name and nothing about the request -- no headers, no
client address -- so it cannot store what it never sees. It logs nothing. ``days_for``
reads a link's counts back (R-008) and writes nothing.

The increment is one statement that also checks the link is not deleted. A follow that
looked the link up as live and then lost a race with a delete would otherwise write a
count onto a tombstone after ``daily_counts_die_with_their_link`` had cleared it; as a
single statement SQLite serialises the two, and the caller answers 410 when nothing was
counted. Expiry is not re-checked here: the caller has already refused an expired link,
and a follow that passed that check a moment before ``expires_at`` got its 302 and is
counted.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

from . import db, links

#: The clock. Module-level so a test can drive it across midnight UTC instead of waiting.
_epoch_seconds = time.time

_INCREMENT = (
    "INSERT INTO daily_counts(link_id, day, count) "
    "SELECT id, ?, 1 FROM links WHERE name = ? AND deleted_at IS NULL "
    "ON CONFLICT(link_id, day) DO UPDATE SET count = count + 1 "
    "RETURNING count"
)

#: One statement, so whether the link exists, whether it is deleted and its counts all come
#: from one read snapshot. Two reads would let a delete land between them and answer 200
#: with an empty ``days`` for a link that is already gone. Newest day first, as the stats
#: page lists them.
_DAYS = (
    "SELECT links.deleted_at, daily_counts.day, daily_counts.count "
    "FROM links LEFT JOIN daily_counts ON daily_counts.link_id = links.id "
    "WHERE links.name = ? "
    "ORDER BY daily_counts.day DESC"
)


def utc_day(epoch_seconds: float) -> str:
    """The UTC date an instant falls on, as ``YYYY-MM-DD`` -- never the host's timezone."""
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).date().isoformat()


def record_follow(conn: sqlite3.Connection, name: str) -> bool:
    """Add one to today's count for ``name``. False if the link is deleted (or absent).

    The new count comes back from the statement, and it decides whether the write can have
    moved a cell (``db.RELAYOUT_COUNTS``) and so whether ``db.settle`` rewrites the file.
    """
    row = conn.execute(_INCREMENT, (utc_day(_epoch_seconds()), name)).fetchone()
    if row is None:
        return False
    db.settle(conn, relayout=row[0] in db.RELAYOUT_COUNTS)
    return True


def days_for(conn: sqlite3.Connection, name: str) -> dict[str, int]:
    """The count for each UTC day ``name`` was followed, newest first (R-008).

    Only days with a count appear: a day nobody followed the link has no row, so no zero is
    invented for it. Read-only, and it never calls ``record_follow``, so asking for a
    link's counts cannot add to them. Raises ``links.NameUnknown`` if no link ever had this
    name and ``links.NameDeleted`` if it was deleted (its counts went with it, ADR-0014).
    An expired link is not refused: expiry is not deletion, and its counts are kept.
    """
    rows = conn.execute(_DAYS, (name,)).fetchall()
    if not rows:
        raise links.NameUnknown(name)
    if rows[0]["deleted_at"] is not None:
        raise links.NameDeleted(name)
    return {row["day"]: row["count"] for row in rows if row["day"] is not None}
