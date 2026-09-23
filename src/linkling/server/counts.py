"""Counting follows: one number per link per UTC day, and nothing else (ADR-0004).

This module is handed a link's name and nothing about the request -- no headers, no
client address -- so it cannot store what it never sees. It logs nothing.

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

#: The clock. Module-level so a test can drive it across midnight UTC instead of waiting.
_epoch_seconds = time.time

_INCREMENT = (
    "INSERT INTO daily_counts(link_id, day, count) "
    "SELECT id, ?, 1 FROM links WHERE name = ? AND deleted_at IS NULL "
    "ON CONFLICT(link_id, day) DO UPDATE SET count = count + 1"
)


def utc_day(epoch_seconds: float) -> str:
    """The UTC date an instant falls on, as ``YYYY-MM-DD`` -- never the host's timezone."""
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).date().isoformat()


def record_follow(conn: sqlite3.Connection, name: str) -> bool:
    """Add one to today's count for ``name``. False if the link is deleted (or absent)."""
    cursor = conn.execute(_INCREMENT, (utc_day(_epoch_seconds()), name))
    return cursor.rowcount == 1
