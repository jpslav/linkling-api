# ADR-0021 — The database file is rewritten into a canonical layout after every write that can move a cell, so a copy of it holds nothing finer than a day about a click

- Status: Proposed
- Approver: (pending)
- Date: 2026-09-25

## Context

ADR-0004 promises that nothing finer than a day can be reconstructed about a click.
ADR-0014 made that true of what SQL can read. Its **Left open** section listed four places
where a copy of `linkling.db` held more:

1. freed cells kept a deleted link's counts;
2. the order of cells within a page gave back the order of each day's first follows;
3. WAL frames kept a per-write history;
4. freed space kept a tombstoned link's old target.

The owner ruled on 2026-09-25: *"Privacy promises should be real."* The promise holds for
the raw file. That overrules the recommendation to scope it to what SQL can read. The
ruling is recorded in `products/linkling/STAKEHOLDER-QUEUE.md` of the program repo.

A deployment's file is its only copy, and data written under a weaker layout cannot be
taken back out of backups already made, so this had to land before the privacy page
(LL-012).

What was measured before choosing (SQLite 3.53.1 and Python 3.12.14 on the build machine;
the probes are in the LL-033 report and the `PLAN:` comments on jpslav/linkling-api#23).
`tests/test_ll033_db_file_bytes.py` also passes in CI against SQLite 3.45.1: the suite
prints the version it ran against. The `python:3.12-slim` image ships 3.46.1, measured
once in review round 1, and nothing here was re-measured there.

- **Cell order is wider than first-follow order.** SQLite overwrites a same-size cell in
  place, but gives a new or grown cell fresh space in the page. Its record format stores
  an integer 1 in no bytes, 2..127 in one byte and 128..32767 in two
  (<https://sqlite.org/fileformat2.html>, "Serial Type Codes"). So a count's cell moves at
  1, 2, 128 and so on, and each move records an order.
- **`VACUUM` output depends only on content.** Two databases holding the same rows,
  written in different orders and then vacuumed, differed in 3 bytes: the header's change
  counter, schema cookie and version-valid-for (offsets 24, 40 and 92).
- **Those three counters are history.**
  - In rollback-journal mode the change counter went to 305 after 300 follows. It counts
    every write ever made, including a deleted link's follows.
  - In WAL mode ordinary writes leave it alone (2 after 300 follows). But a commit that
    rewrites page 1 on a fresh connection sets it to the value that connection last read,
    plus one, and `VACUUM` rewrites page 1.
  - The schema cookie gains one per `VACUUM`.
- **Overlapping connections defeat a clean WAL.** A `wal_checkpoint(TRUNCATE)` made while
  another connection was mid-read returned `(1, 1, 0)` and left a 4,152-byte `-wal`.
  Connections that overlap also share a `-shm`, whose wal-index counts every commit made
  while it exists.

## Decision

**Proposed.** Everything below is done by the running service. Nothing is scheduled, and
nothing is anybody's chore.

1. **Every connection sets `secure_delete=ON` and `temp_store=MEMORY`**
   (`src/linkling/server/db.py`, `connect`). Freed bytes are zeroed while a write is in
   flight, and SQLite's temporary tables stay out of the temp directory.
2. **Every write path calls `db.settle(conn, relayout=…)` after it commits.**
   - A create, a delete, and a follow whose new count is in `db.RELAYOUT_COUNTS`
     (1, 2, 128, 32768, …) pass `relayout=True`. So does startup, after migrations.
   - A follow that overwrites its count in place passes `relayout=False`.
3. **A relayout write rewrites the file** (`db.canonicalise`).
   - It runs `VACUUM`, which rebuilds every table in key order and holds the write lock
     throughout, so no other writer can commit in between and be overwritten.
   - It then sets the schema cookie back to its value before the rewrite. It does that
     only if `sqlite_schema` (every name, root page and statement) is unchanged, so a
     connection that cached the schema at that cookie still holds a correct one.
4. **Every settle runs `PRAGMA wal_checkpoint(TRUNCATE)` without waiting**
   (`busy_timeout=0`, `db._checkpoint`), so the `-wal` is 0 bytes between writes.
5. **After a rewrite and a clean checkpoint, the change counter and version-valid-for are
   written back to a constant.** This is a direct 8-byte write to offsets 24 and 92
   (`db._reset_change_counter`).
   - It happens only in WAL mode and inside `BEGIN IMMEDIATE`.
   - It goes through one descriptor per file that is never closed. Closing any
     descriptor on a file releases every POSIX lock the process holds on it, SQLite's
     included.
6. **A rewrite that cannot be done now is owed, and paid by the next request of any kind**
   (`db._deferred`, `db.settle_owed`).
   - When another process holds a read open, the checkpoint cannot empty the WAL. A
     rewrite then would copy every page into a WAL that cannot shrink until that reader
     ends.
   - So the rewrite is put off, and so is one that raises. Every request's connection
     pays an owed rewrite before it closes.
7. **The service holds one connection at a time.** `get_conn` in `app.py` holds an
   `anyio.Lock` from open to close.
   - Every route takes it with `scope="function"`, so the lock is released when the
     handler returns, before the response is written.
   - The lock is awaited on the event loop, not a `threading.Lock`. A request waiting for
     it holds no worker thread, and anyio's default limiter has 40 of them.
   - `anyio` is Starlette's own dependency, already pinned in `requirements.lock.txt`.

**Journal mode stays WAL.** ADR-0005's "a tuning choice, not a door" still describes it,
and the README's backup and restore commands are unchanged.

**No migration.** The schema is the same. The first start of this version rewrites a
deployment's existing file, and so does the first start after a restore.

### Why the change counter is written directly

Nothing in SQL resets it. Without the reset it counted rewrites. Two histories with the
same day-level content, which differed only in how often a since-deleted link had been
followed, ended at 152 and 153 on LL-033's prototype, before the reset existed. Removing the reset
turns `test_leak_1_…` red in all three copy cases.

It is safe because in WAL mode SQLite does not use the counter to notice changes.
<https://sqlite.org/fileformat2.html> says: "In WAL mode, changes to the database are
detected using the wal-index and so the change counter is not needed." A second
connection with a warm page cache, open across a reset, read each of 5 later writes
correctly, and `integrity_check` returned `ok`.

This is the one part of the design that leaves SQLite's API. It is the first place to look
if a future SQLite starts using the counter in WAL mode.

### Alternatives, ranked by the recurring work they leave

| Option | Recurring work | Closes | Cost on a 2.2 MB file | Survives a restore |
|---|---|---|---|---|
| **A. This decision** | none | all four, plus the header counters | in-place follow +0.07 ms; relayout write +12–19 ms (`VACUUM`) | yes: startup rewrites it |
| B. `secure_delete` alone | none | 1 and 4 in the main file. Freeblock sizes still give a deleted target's length. | ~0 | partly |
| C. A timer-run `VACUUM` in the service | none | not "at any moment": each period's order is readable until the next run, and the counters count the runs | periodic stall | yes |
| D. Rollback journal instead of WAL | none | 3, but the change counter then counts every write ever made | ~0 | yes |
| E. A table rebuild in SQL | none | `daily_counts` only | 73–76 ms | yes |
| F. `VACUUM INTO` a new file, renamed into place | none | all | ~15 ms | yes, but **unsafe**: `-wal` and `-shm` are found by name, so another open connection would pair them with the new file |
| G. `VACUUM INTO` a copy, backed up over the file | none | all | 14–17 ms | yes, but it holds no lock between snapshot and copy-back, so a commit from another process in between was overwritten (review round 1, measured) |
| H. A `VACUUM` somebody schedules with cron | **somebody must remember it** | not "at any moment" | periodic | yes |

## Consequences

- **The proof** is `tests/test_ll033_db_file_bytes.py`, shown red against the layout before
  this ADR.
  - Two histories with the same day-level content leave byte-identical `linkling.db` and
    `-wal`. That holds whether the files are copied at rest, mid-request, or with another
    connection held open, and it holds for a file whose tables span many pages.
  - No page keeps its cells in arrival order, checked straight after each kind of write
    that moves a cell.
  - No count record or 12-byte piece of a deleted link's target survives in any file.
- **Throughput.** For 2,000 follows from 48 concurrent clients over uvicorn (SQLite 3.53.1,
  a fresh 20-link database), throughput went from 1,222 and 1,133 per second to 651 and
  541, two runs each. Every follow was answered 302 and counted. That answers ADR-0005's
  open question at this scale: hundreds per second, one at a time.
- **A write that fails to settle fails its request.** A settle that raises, after the write
  has committed, answers 500, so a follow can be counted and still answer 500. That is the
  same failure class ADR-0014 accepts for a failed count write. The rewrite stays owed.
- **Reads now queue behind writes.** A write held up by another process's write lock (up
  to the 5 s busy timeout) holds the service's lock, and the requests behind it wait,
  reads included. Before this ADR, WAL let reads through.
- **What stays**, for the privacy page (LL-012) to write from:
  - *A copy that races a write can show that write*, in its `-wal` or `-shm`. Two copies a
    moment apart show the same thing, so no storage design removes it.
  - *While another process holds a read open* (an operator's `sqlite3` shell mid-query,
    for example), the checkpoint cannot empty the WAL.
    - The `-wal` then keeps a frame per write, in order, until that read ends.
    - The rewrite is owed until then, so the latest writes' layout stays in the file.
    - It is paid by the first request after the read ends, whatever that request is.
  - *A process that merely holds the database open* keeps the `-shm` alive while it is
    open, and the `-shm` counts the writes made since. It holds no page content. The
    README's backup command opens and closes in one step.
  - *If the WAL switch loses at connect* and the file runs in rollback-journal mode
    (`db._enable_wal`), the change counter counts every write, and nothing resets it.
  - *Below SQLite*, the filesystem and the disk own the blocks of a truncated `-wal` and
    the old copies of pages an SSD has remapped. The promise covers a copy of the files.
  - *Backups already taken* under the earlier layout keep that layout's residue. Nothing
    here can reach them.
