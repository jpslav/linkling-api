"""LL-033: a copy of the database file holds nothing finer than a day about a click.

The owner ruled on 2026-09-25 that ADR-0004's promise -- nothing finer than a day can be
reconstructed about a click -- holds for the raw file, not only for what SQL can read.
ADR-0021 is how. This file is the evidence: it reads the raw bytes of every file beside the
database, copied the way ``cp -r data/`` would copy them, and never asks SQLite what they
mean except to find the table it walks and to count that table's rows as a control.

**Two histories, one day-level content.** The same links are made, the same counts land on
the same UTC days, the same link is deleted and the same link expires. What differs is
everything finer than a day, and what a deleted link used to hold:

- the order of every day's follows (so the order of each day's first follows);
- how often the deleted link was followed before it went (3 against 140, which crosses the
  128 at which SQLite stores a count in two bytes);
- the deleted link's target, in text and in length.

If the files are a function of the day-level content alone, the two histories leave the
same bytes. That comparison plus four direct searches are the four leaks ADR-0014 "Left
open" listed.

**Three moments to copy at:**

- ``at-rest``: after the last request, with the service still running.
- ``mid-request``: inside the last follow, with the service's connection open and its
  write done -- a backup that races a request.
- ``another-connection-open``: a second connection held open for the whole history. Under
  the code before ADR-0021 that is what overlapping requests did; now it is a process
  outside the service, such as an operator's ``sqlite3`` shell left open.

**Blind is never a pass.** A copy with no database bytes, a page walk that does not find
every row, a search that cannot find a live link's record or target, and a ``-wal`` missing
while a connection is open each fail with a message starting ``DB-BYTES BLIND:``.

The ``-shm`` is searched for targets and count records but not compared between the
histories: SQLite fills its WAL salts with random bytes, and under
``another-connection-open`` its wal-index counts writes since that connection opened.
ADR-0021 records that residue.
"""

from __future__ import annotations

import random
import sqlite3
import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from linkling.server import counts, links
from linkling.server.app import create_app
from linkling.server.config import Config

KEY = "ll033-test-key"
DB = "linkling.db"
WAL = "linkling.db-wal"
JOURNAL = "linkling.db-journal"
SHM = "linkling.db-shm"

DAYS = ["2026-09-01", "2026-09-02", "2026-09-03"]
#: Noon UTC on each day, as epoch seconds, for ``counts._epoch_seconds``.
NOON = {"2026-09-01": 1788264000.0, "2026-09-02": 1788350400.0, "2026-09-03": 1788436800.0}
LIVE = [f"live{i:02d}" for i in range(30)]
GONE_DAY = "2026-09-02"
GONE_TARGET = {
    "A": "https://gone-alpha.example/PRIVATE-TARGET-ALPHA-7731",
    "B": "https://gone-beta-longer-host.example/another/PRIVATE-TARGET-BETA-9902?q=1",
}
GONE_FOLLOWS = {"A": 3, "B": 140}
SHUFFLE_SEED = {"A": 1, "B": 2}
#: A piece of the deleted target is this many bytes of it, anywhere in any file.
WINDOW = 12

CASES = {
    "at-rest": {"copy_mid_request": False, "hold_open": False},
    "mid-request": {"copy_mid_request": True, "hold_open": False},
    "another-connection-open": {"copy_mid_request": False, "hold_open": True},
}


def _day_level_content() -> dict[tuple[str, str], int]:
    """The counts both histories end with, per (link, day). One link-day crosses 128."""
    rnd = random.Random(20260925)
    plan = {}
    for day in DAYS:
        for name in LIVE:
            k = rnd.choice([0, 0, 1, 1, 2, 3, 4, 7])
            if k:
                plan[(name, day)] = k
    plan[("live03", "2026-09-02")] = 131
    plan[("live00", "2026-09-03")] = 3
    # live12 is left for the tail's first and second follow on the last day. Its cell sits
    # mid-page, so a grown cell that moved would show; the newest link's cell sits at the
    # top of the content area, where a move leaves nothing behind (measured).
    plan.pop(("live12", "2026-09-03"), None)
    plan[("brief", "2026-09-01")] = 2
    return plan


def _copy(data: Path) -> dict[str, bytes]:
    """Every file beside the database, read the way ``cp -r data/`` would read it."""
    return {p.name: p.read_bytes() for p in sorted(data.iterdir()) if p.is_file()}


def _run_history(
    data: Path, which: str, mp: pytest.MonkeyPatch, *, copy_mid_request, hold_open, tail=None
):
    data.mkdir()
    path = data / DB
    app = create_app(Config(api_key=KEY, db_path=str(path)))
    auth = {"Authorization": f"Bearer {KEY}"}
    now = {"value": "2026-08-31T00:00:00Z"}
    clock = {"value": NOON[DAYS[0]]}
    mp.setattr(links, "_now", lambda: now["value"])
    mp.setattr(counts, "_epoch_seconds", lambda: clock["value"])
    content = _day_level_content()
    copied: dict[str, bytes] = {}
    record_tail = tail is not None
    held = None

    with TestClient(app, follow_redirects=False) as client:
        if hold_open:
            held = sqlite3.connect(path)
            held.execute("SELECT count(*) FROM links").fetchall()

        def create(name, url, **extra):
            body = {"name": name, "url": url, **extra}
            assert client.post("/-/api/links", json=body, headers=auth).status_code == 201

        for name in LIVE:
            create(name, f"https://example.com/{name}")
        create("gone", GONE_TARGET[which])
        create("brief", "https://example.com/brief", expires="2026-09-02T00:00:00Z")

        rnd = random.Random(SHUFFLE_SEED[which])
        for day in DAYS:
            clock["value"] = NOON[day]
            now["value"] = f"{day}T12:00:00Z"
            follows = [n for (n, d), k in content.items() if d == day for _ in range(k)]
            if day == GONE_DAY:
                follows += ["gone"] * GONE_FOLLOWS[which]
            rnd.shuffle(follows)
            for name in follows:
                assert client.get(f"/{name}").status_code == 302, name
            if day == GONE_DAY:
                assert client.delete("/-/api/links/gone", headers=auth).status_code == 204
                assert client.get("/gone").status_code == 410
                assert client.get("/brief").status_code == 410  # expired; counts kept

        # The tail: the same writes in both histories, each of a kind that moves a cell,
        # copied after each one. A later canonicalisation would erase an earlier write's
        # layout, so each kind has to be looked at straight after it happens.
        def after(step):
            if record_tail:
                tail[step] = _copy(data)

        create("aaa-late", "https://example.com/aaa-late")  # sorts before every other name
        after("a create")
        assert client.get("/live12").status_code == 302
        after("a first follow")
        assert client.get("/live12").status_code == 302
        after("a second follow")
        for _ in range(128 - content.get(("live05", DAYS[-1]), 0)):
            assert client.get("/live05").status_code == 302
        after("a count reaching 128")
        assert client.delete("/-/api/links/live06", headers=auth).status_code == 204
        after("a delete")

        # The last request: one more follow of a link already followed today, so it is
        # the same in-place write in both histories.
        if copy_mid_request:
            record = counts.record_follow

            def record_then_copy(conn, name):
                counted = record(conn, name)
                copied.update(_copy(data))
                return counted

            mp.setattr(counts, "record_follow", record_then_copy)
            assert client.get("/live00").status_code == 302
            mp.setattr(counts, "record_follow", record)
        else:
            assert client.get("/live00").status_code == 302
            copied.update(_copy(data))

    if held is not None:
        held.close()
    return copied


@pytest.fixture(scope="module")
def copies(tmp_path_factory):
    """Both histories, copied at each of the three moments. Built once for the module."""
    mp = pytest.MonkeyPatch()
    out = {case: {} for case in CASES}
    out["tail"] = {}
    try:
        for case, how in CASES.items():
            tmp = tmp_path_factory.mktemp(case)
            for which in "AB":
                tail = {} if case == "at-rest" else None
                out[case][which] = _run_history(tmp / which, which, mp, **how, tail=tail)
                if tail is not None:
                    out["tail"][which] = tail
    finally:
        mp.undo()
    return out


def _db_bytes(copy: dict[str, bytes]) -> bytes:
    data = copy.get(DB, b"")
    if len(data) < 100 or data[:16] != b"SQLite format 3\x00":
        pytest.fail(f"DB-BYTES BLIND: no database bytes in the copy ({len(data)} bytes of {DB})")
    return data


def _inspect(copy: dict[str, bytes], tmp: Path, sql: str, *args) -> list[tuple]:
    """Ask SQLite about the copied database file alone, opened read-only and immutable.

    Used only for controls: which page the table starts on, how many rows it has, a live
    link's id, whether the file is intact. ``immutable=1`` makes SQLite read that one file
    and nothing beside it, so what it says is what the copied ``.db`` holds by itself.
    """
    tmp.mkdir(parents=True, exist_ok=True)
    for stale in tmp.iterdir():
        stale.unlink()
    path = tmp / "inspect.db"
    path.write_bytes(_db_bytes(copy))
    conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _int_field(value: int) -> tuple[int, bytes]:
    if value == 0:
        return 8, b""
    if value == 1:
        return 9, b""
    for serial, width in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 6), (6, 8)):
        if -(1 << (8 * width - 1)) <= value < (1 << (8 * width - 1)):
            return serial, value.to_bytes(width, "big", signed=True)
    raise ValueError(value)


def _count_record_body(link_id: int, day: str, count: int) -> bytes:
    """The values SQLite writes for one ``daily_counts`` row (fileformat2.html, "Record Format").

    The body only, without the record header before it: when a cell is freed, SQLite
    writes a 4-byte freeblock header over the cell's start, which is exactly the payload
    length and the record header. The values after them are what survives. A count of 1
    has no bytes of its own, so its body is the link and the day -- which is still that
    row, surviving.
    """
    _, id_body = _int_field(link_id)
    _, count_body = _int_field(count)
    return id_body + day.encode() + count_body


#: b-tree page types (fileformat2.html, "B-tree Pages") and each one's header length.
_PAGE_HEADER = {2: 12, 5: 12, 10: 8, 13: 8}


def _misplaced(data: bytes, roots: dict[str, int]) -> tuple[dict[str, int], list[str]]:
    """Walk every b-tree over raw bytes. Returns cells seen per tree and what is out of place.

    A page written in key order, all at once -- which is what ``VACUUM`` does -- has its
    cells packed from the end of the page in key order, no freeblock and no fragmented
    bytes, and the file has no free pages. A page that took its rows one at a time holds
    them in arrival order; a page a cell left or grew out of holds a freeblock. Either is a
    record of what happened when, which is what this looks for.
    """
    page_size = struct.unpack(">H", data[16:18])[0]
    page_size = 65536 if page_size == 1 else page_size
    problems = []
    free_pages = struct.unpack(">I", data[36:40])[0]
    if free_pages:
        problems.append(f"{free_pages} free pages")
    cells_seen = {}
    for tree, root in roots.items():
        cells_seen[tree] = 0
        pending = [root]
        while pending:
            page = pending.pop()
            base = (page - 1) * page_size
            header = base + (100 if page == 1 else 0)
            kind = data[header]
            if kind not in _PAGE_HEADER:
                pytest.fail(f"DB-BYTES BLIND: page {page} of {tree} is type {kind}, not a b-tree page")
            freeblock = struct.unpack(">H", data[header + 1 : header + 3])[0]
            fragmented = data[header + 7]
            cells = struct.unpack(">H", data[header + 3 : header + 5])[0]
            start = header + _PAGE_HEADER[kind]
            offsets = [
                struct.unpack(">H", data[start + 2 * i : start + 2 * i + 2])[0] for i in range(cells)
            ]
            if freeblock or fragmented:
                problems.append(f"{tree} page {page}: freeblock at {freeblock}, {fragmented} fragmented bytes")
            if offsets != sorted(offsets, reverse=True):
                problems.append(f"{tree} page {page}: cells out of key order")
            if kind in (2, 5):  # interior: each cell starts with its left child's page number
                pending += [struct.unpack(">I", data[base + o : base + o + 4])[0] for o in offsets]
                pending.append(struct.unpack(">I", data[header + 8 : header + 12])[0])
            else:
                cells_seen[tree] += cells
    return cells_seen, problems


def _assert_laid_out_by_content_alone(copy: dict[str, bytes], tmp: Path, label: str) -> None:
    data = _db_bytes(copy)
    roots = dict(_inspect(copy, tmp, "SELECT name, rootpage FROM sqlite_schema WHERE rootpage > 0"))
    [(rows,)] = _inspect(copy, tmp, "SELECT count(*) FROM daily_counts")
    cells_seen, problems = _misplaced(data, roots)
    if rows == 0 or cells_seen.get("daily_counts") != rows:
        pytest.fail(
            f"DB-BYTES BLIND: the page walk found {cells_seen.get('daily_counts')} cells for "
            f"{rows} daily_counts rows ({label})"
        )
    assert not problems, f"{label}: {problems[:6]}"


@pytest.mark.parametrize("case", CASES)
def test_the_file_is_intact(copies, case, tmp_path):
    """What the other tests read is a sound database, not a half-written one."""
    for which in "AB":
        result = _inspect(copies[case][which], tmp_path / which, "PRAGMA integrity_check")
        assert result == [("ok",)], f"history {which}: {result}"


@pytest.mark.parametrize("case", CASES)
def test_leak_1_a_deleted_links_counts_are_nowhere_in_the_files(copies, case, tmp_path):
    """No count record the deleted link ever had survives, and nothing else tells A from B."""
    for which in "AB":
        copy = copies[case][which]
        _db_bytes(copy)
        ids = dict(_inspect(copy, tmp_path / which, "SELECT name, id FROM links"))
        if "gone" not in ids or "live03" not in ids:
            pytest.fail(f"DB-BYTES BLIND: the copied {DB} alone holds {len(ids)} links")
        gone_id, live_id = ids["gone"], ids["live03"]
        control = _count_record_body(live_id, "2026-09-02", 131)
        if not any(control in blob for blob in copy.values()):
            pytest.fail("DB-BYTES BLIND: a live link's count record is not found either")
        found = [
            (name, k)
            for k in range(1, GONE_FOLLOWS[which] + 1)
            for name, blob in copy.items()
            if _count_record_body(gone_id, GONE_DAY, k) in blob
        ]
        assert not found, f"history {which}: the deleted link's count records survive: {found[:5]}"

    a, b = copies[case]["A"], copies[case]["B"]
    for name in sorted((set(a) | set(b)) - {SHM}):
        left, right = a.get(name, b""), b.get(name, b"")
        differing = sum(x != y for x, y in zip(left, right)) + abs(len(left) - len(right))
        assert differing == 0, (
            f"{differing} bytes of {name} differ between two histories with the same "
            "day-level content"
        )


@pytest.mark.parametrize("case", CASES)
def test_leak_2_no_page_keeps_the_order_rows_arrived_in(copies, case, tmp_path):
    """Every b-tree page holds its cells packed in key order, with no free space left behind.

    SQLite takes a new cell from the free space at the top of the content area, so a page
    written row by row stores rows in arrival order: the order of each day's first follows.
    """
    for which in "AB":
        _assert_laid_out_by_content_alone(copies[case][which], tmp_path / which, f"history {which}")


def test_leak_2_every_kind_of_write_that_moves_a_cell_is_rewritten(copies, tmp_path):
    """Straight after a create, a first follow, a second, a count reaching 128 and a delete.

    Each of these moves or frees a cell. Checked after each one, because the next
    canonicalisation would otherwise tidy up after a write that was never tidied itself.
    """
    steps = ["a create", "a first follow", "a second follow", "a count reaching 128", "a delete"]
    for which in "AB":
        tail = copies["tail"].get(which, {})
        if sorted(tail) != sorted(steps):
            pytest.fail(f"DB-BYTES BLIND: history {which} copied {sorted(tail)}, not {steps}")
        for step in steps:
            _assert_laid_out_by_content_alone(
                tail[step], tmp_path / which / step.replace(" ", "-"), f"history {which}, after {step}"
            )


def test_startup_rewrites_a_file_an_earlier_version_left(tmp_path):
    """A database written by the layout before ADR-0021 is rewritten when the service starts.

    It is written here the way that layout wrote it: rows in arrival order, a delete that
    frees cells, no ``secure_delete``. Starting the app and stopping it must leave a file
    whose pages carry none of that.
    """
    data = tmp_path / "data"
    data.mkdir()
    path = data / DB
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    from linkling.server import db

    db.migrate(conn)
    for name in ("ccc", "aaa", "bbb", "gone"):
        conn.execute(
            "INSERT INTO links(name, target, created_at) VALUES (?, ?, '2026-08-31T00:00:00Z')",
            (name, GONE_TARGET["A"] if name == "gone" else f"https://example.com/{name}"),
        )
    for name in ("gone", "bbb", "ccc", "aaa", "gone", "bbb"):
        conn.execute(
            "INSERT INTO daily_counts(link_id, day, count) "
            "SELECT id, '2026-09-01', 1 FROM links WHERE name = ? "
            "ON CONFLICT(link_id, day) DO UPDATE SET count = count + 1",
            (name,),
        )
    conn.execute(
        "UPDATE links SET target = '', deleted_at = '2026-09-01T12:00:00Z' WHERE name = 'gone'"
    )
    conn.close()
    before = _copy(data)
    _, problems_before = _misplaced(
        _db_bytes(before),
        dict(_inspect(before, tmp_path / "before", "SELECT name, rootpage FROM sqlite_schema WHERE rootpage > 0")),
    )
    if not problems_before:
        pytest.fail("DB-BYTES BLIND: the file written the old way already looks rewritten")

    with TestClient(create_app(Config(api_key=KEY, db_path=str(path)))):
        pass
    after = _copy(data)
    _assert_laid_out_by_content_alone(after, tmp_path / "after", "after startup")
    target = GONE_TARGET["A"].encode()
    pieces = {target[i : i + WINDOW] for i in range(len(target) - WINDOW + 1)}
    assert not {n for n, blob in after.items() for p in pieces if p in blob}


@pytest.mark.parametrize("case", CASES)
def test_leak_3_no_write_ahead_log_or_journal_keeps_past_writes(copies, case):
    for which in "AB":
        copy = copies[case][which]
        _db_bytes(copy)
        if case != "at-rest" and WAL not in copy:
            # A WAL-mode connection is open, so its -wal exists; missing means the copy missed it.
            pytest.fail("DB-BYTES BLIND: a connection is open but the copy has no -wal")
        for name in (WAL, JOURNAL):
            size = len(copy.get(name, b""))
            assert size == 0, f"history {which}: {name} holds {size} bytes"


@pytest.mark.parametrize("case", CASES)
def test_leak_4_a_deleted_links_target_is_nowhere_in_the_files(copies, case):
    for which in "AB":
        copy = copies[case][which]
        _db_bytes(copy)
        if not any(b"https://example.com/live07" in blob for blob in copy.values()):
            pytest.fail("DB-BYTES BLIND: a live link's target is not found either")
        target = GONE_TARGET[which].encode()
        pieces = {target[i : i + WINDOW] for i in range(len(target) - WINDOW + 1)}
        holding = sorted({name for name, blob in copy.items() for p in pieces if p in blob})
        assert not holding, f"history {which}: pieces of the deleted target in {holding}"
