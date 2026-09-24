"""LL-027 -- ADR-0011's surface table lists every route the service serves, and only those.

The ADR is Accepted, so a route that ships without a row is a reader learning the surface wrong
from a document that says it is settled. The route list here is read off the application, not
written out again, so a route added later fails this until its row is added to the table (a
correction by addition, marked and dated, like the three the note under the table records).
"""

from __future__ import annotations

import re
from pathlib import Path

ADR = Path(__file__).resolve().parent.parent / "docs" / "adr" / "0011-links-api-surface.md"


def table_calls(text: str) -> set[tuple[frozenset[str], str]]:
    """The (methods, path) of every row of the table: `` | `GET\\|HEAD /<name>` | ... ``."""
    calls = set()
    for line in text.splitlines():
        row = re.match(r"^\| `([^`]+)`", line)
        if not row:
            continue
        methods, _, path = row.group(1).replace("\\|", "|").partition(" ")
        if path.startswith("/"):
            calls.add((frozenset(methods.split("|")), path.replace("<name>", "{name}")))
    return calls


def served_calls(app) -> set[tuple[frozenset[str], str]]:
    return {(frozenset(route.methods), route.path) for route in app.routes}


def test_the_table_has_a_row_for_every_route_and_no_row_for_any_other(app):
    table = table_calls(ADR.read_text(encoding="utf-8"))
    served = served_calls(app)
    assert served - table == set(), "routes the service serves that the table does not list"
    assert table - served == set(), "table rows for routes the service does not serve"


def test_the_comparison_has_something_to_compare(app):
    """An empty route list, or a table the parser found no rows in, would make it vacuous."""
    assert served_calls(app), "the application registered no routes"
    assert table_calls(ADR.read_text(encoding="utf-8")), "no rows were read from the table"
