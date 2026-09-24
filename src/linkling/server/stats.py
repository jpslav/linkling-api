"""The stats page (R-010): every link, and its count for each UTC day, as plain HTML.

Read-only: ``collect`` runs one SELECT, and nothing here imports ``counts``, so viewing
this page cannot count as a follow (ADR-0004 counts requests that follow a link).

Which links are listed follows from what the tables keep. A deleted link is not listed:
``links.delete`` dropped its target and maker, and its counts went with it by trigger
(ADR-0014). An expired link is listed, marked, with its counts: expiry is not deletion,
and ADR-0014 keeps an expired link's counts. No ADR says whether this page shows an
expired link, so showing it is this module's choice. A live link nobody has followed is
listed too, with no invented zero row.

The page references nothing -- no script, stylesheet, image or link -- so there is nothing
for it to load from anyone. ADR-0009 rules out third parties in the click path and on the
public site and does not name this page; the page follows the same rule. Every value is
escaped: a target is caller-supplied text, and ``created_by`` is a free-text label.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from html import escape

from . import links

#: One statement, so the links and their counts come from one read snapshot rather than a
#: link list read at one moment and its counts at another. Days run newest first.
_QUERY = (
    "SELECT links.name, links.target, links.created_by, links.expires_at, "
    "daily_counts.day, daily_counts.count "
    "FROM links LEFT JOIN daily_counts ON daily_counts.link_id = links.id "
    "WHERE links.deleted_at IS NULL "
    "ORDER BY links.name, daily_counts.day DESC"
)


@dataclass(frozen=True)
class LinkStats:
    name: str
    target: str
    created_by: str | None
    expires_at: str | None
    expired: bool
    days: tuple[tuple[str, int], ...]


def collect(conn: sqlite3.Connection) -> list[LinkStats]:
    """Every link that has not been deleted, by name, with its (day, count) pairs."""
    grouped: dict[str, tuple[sqlite3.Row, list[tuple[str, int]]]] = {}
    for row in conn.execute(_QUERY):
        _, days = grouped.setdefault(row["name"], (row, []))
        if row["day"] is not None:
            days.append((row["day"], row["count"]))
    return [
        LinkStats(
            name=row["name"],
            target=row["target"],
            created_by=row["created_by"],
            expires_at=row["expires_at"],
            expired=links.is_expired(row["expires_at"]),
            days=tuple(days),
        )
        for row, days in grouped.values()
    ]


def _section(link: LinkStats) -> list[str]:
    lines = [f"<h2>{escape(link.name)}</h2>", f"<p>{escape(link.target)}</p>"]
    details = []
    if link.created_by:
        details.append(f"made by {escape(link.created_by)}")
    if link.expires_at:
        state = "expired" if link.expired else "expires"
        details.append(f"{state} {escape(link.expires_at)}")
    if details:
        lines.append(f"<p>{'; '.join(details)}</p>")
    if not link.days:
        lines.append("<p>No follows yet.</p>")
        return lines
    lines.append("<table>")
    lines.append('<tr><th scope="col">Day (UTC)</th><th scope="col">Count</th></tr>')
    for day, count in link.days:
        lines.append(f"<tr><td>{escape(day)}</td><td>{count}</td></tr>")
    lines.append("</table>")
    return lines


def render(stats: list[LinkStats]) -> str:
    """The whole page. Plain HTML, unstyled."""
    lines = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        "<title>Linkling stats</title>",
        "</head>",
        "<body>",
        "<h1>Linkling stats</h1>",
    ]
    if not stats:
        lines.append("<p>No links yet.</p>")
    for link in stats:
        lines.extend(_section(link))
    lines.extend(["</body>", "</html>", ""])
    return "\n".join(lines)
