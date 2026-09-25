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

The owner ruled on 2026-09-25: *"Privacy promises should be real."* The promise holds
for the raw file. The owner overruled the recommendation to scope it to what SQL can read.
The ruling is recorded in `products/linkling/STAKEHOLDER-QUEUE.md` of the program repo. A
deployment's file is its only copy, and data written under a weaker layout cannot be
removed from backups already taken, so this had to land before the privacy page (LL-012).

What was measured before choosing (SQLite 3.53.1, Python 3.12.14; the probes are in the
LL-033 report and the `PLAN:` comments on jpslav/linkling-api#23):

- **Cell order is wider than first-follow order.** SQLite overwrites a same-size cell in
  place, but gives a new or grown cell fresh space in the page. Its record format stores
  an integer 1 in no bytes, 2..127 in one byte and 128..32767 in two
  (<https://sqlite.org/fileformat2.html>, "Serial Type Codes"). So a count's cell moves at
  1, 2, 128, and so on, and each move records an order.
- **`VACUUM` output depends only on content.** Two databases with the same rows, written
  in different orders and then vacuumed, differed in 3 bytes. Those were the header's
  change counter, schema cookie and version-valid-for (offsets 24, 40 and 92).
- **Those three counters are history.** In rollback-journal mode the change counter went
  to 305 after 300 follows, so it counts every write ever made, including a deleted
  link's follows. In WAL mode ordinary writes leave it alone: it read 2 after 300 follows.
  But a commit that rewrites page 1 on a fresh connection sets it to the value that
  connection last read, plus one. `VACUUM` and the backup API both rewrite page 1. The
  schema cookie gains one per `VACUUM` or backup.
- **Overlapping connections defeat a clean WAL.** A `wal_checkpoint(TRUNCATE)` made while
  another connection was mid-read returned `(1, 1, 0)` and left a 4,152-byte `-wal`.
  Connections that overlap also share a `-shm`, whose wal-index counts every commit made
  while it exists.

## Decision

**Proposed.** Everything below is done by the running service. Nothing is scheduled, and
nothing is anybody's chore.

1. **Every connection sets `secure_delete=ON` and `temp_store=MEMORY`**
   (`src/linkling/server/db.py`, `connect`). Freed bytes are zeroed. SQLite's temporary
   tables stay out of the temp directory.
2. **Every write path calls `db.settle(conn, relayout=…)` after it commits.**
   - A create, a delete, and a follow whose new count is in `db.RELAYOUT_COUNTS`
     (1, 2, 128, 32768, …) pass `relayout=True`. So does startup, after migrations.
   - A follow that overwrites its count in place passes `relayout=False`.
3. **`relayout=True` canonicalises the file** (`db.canonicalise`).
   - It runs `VACUUM INTO` a named in-memory database.
   - It checks that every table in the copy has the same number of rows as the file.
   - It copies the copy back over the file with the backup API, in one transaction.
   - It then sets the schema cookie back to its value before the write. It does that only
     if `sqlite_schema` (every name, root page and statement) is unchanged, so a
     connection that cached the schema at that cookie still holds a correct one.
4. **Every settle then runs `PRAGMA wal_checkpoint(TRUNCATE)`,** so the `-wal` is 0 bytes
   between writes.
5. **After a canonicalisation, the change counter and version-valid-for are written back
   to a constant.** It is a direct 8-byte write to offsets 24 and 92 (`db._reset_change_counter`).
   It happens only in WAL mode, only after a checkpoint that left the WAL empty, and inside
   `BEGIN IMMEDIATE`.
6. **The service holds one connection at a time.** `get_conn` in `app.py` is an async
   dependency that holds an `anyio.Lock` from open to close. It is awaited on the event
   loop rather than a `threading.Lock`: a request waiting for it holds no worker thread,
   and anyio's default limiter has 40 of them. `anyio` is Starlette's own dependency,
   already pinned in `requirements.lock.txt`, so this adds nothing to the image.

**Journal mode stays WAL.** ADR-0005's "a tuning choice, not a door" still describes it,
and the README's backup and restore commands are unchanged.

**No migration.** The schema is the same. The first start of this version canonicalises a
deployment's existing file, and so does the first start after a restore.

### Why the change counter is written directly

Nothing in SQL resets it. Without the reset it counted canonicalisations. Two histories
with the same day-level content, differing only in how often a since-deleted link had
been followed, ended at 152 and 153.

The write is safe because in WAL mode SQLite does not use the counter to notice changes.
<https://sqlite.org/fileformat2.html>: "In WAL mode, changes to the database are detected
using the wal-index and so the change counter is not needed." A second connection with a
warm page cache, open across a reset, then read each of 5 later writes correctly, and
`integrity_check` returned `ok`.

This is the part of the design that leaves SQLite's API. It is the first place to look if
a future SQLite starts using the counter in WAL mode.

### Alternatives, ranked by the recurring work they leave

| Option | Recurring work | Closes | Cost on a 2.2 MB file | Survives a restore |
|---|---|---|---|---|
| **A. This decision** | none | all four, and the header counters | in-place follow +0.07 ms; a relayout write +14–17 ms | yes: startup canonicalises |
| B. `secure_delete` alone | none | 1 and 4 in the main file. Freeblock sizes still give a deleted target's length. | ~0 | partly |
| C. A timer-run `VACUUM` in the service | none | not "at any moment": the order within each period is readable until the next run, and the counters count the runs | periodic stall | yes |
| D. Rollback journal instead of WAL | none | 3, but the change counter then counts every write ever made | ~0 | yes |
| E. A table rebuild in SQL | none | `daily_counts` only | 73–76 ms | yes |
| F. `VACUUM INTO` a new file, renamed into place | none | all | ~15 ms | yes, but **unsafe**: `-wal` and `-shm` are found by name, so another open connection would pair them with the new file |
| G. A `VACUUM` somebody schedules with cron | **somebody must remember it** | not "at any moment" | periodic | yes |

## Consequences

- **The proof.** Two histories with the same day-level content leave byte-identical
  `linkling.db` and `-wal` files, whether copied at rest, mid-request, or with another
  connection held open. No page keeps its cells in arrival order. No count record or
  12-byte piece of a deleted link's target survives in any file.
  `tests/test_ll033_db_file_bytes.py` checks all of this, and it was shown red against the
  layout before this ADR.
- **Throughput.** 2,000 follows from 48 concurrent clients over uvicorn went from 883 and
  1,010 per second to 371 and 455 per second (two runs each, SQLite 3.53.1, a fresh
  20-link database). Every follow was answered 302 and counted. That is ADR-0005's open
  question, answered at this scale: hundreds per second, one at a time.
- **A write that fails to settle fails its request.** A failed canonicalisation raises
  after the write has committed, so a follow can be counted and still answer 500. That is
  the same failure class ADR-0014 already accepts for a failed count write.
- **What stays**, for the privacy page (LL-012) to write from:
  - *A copy that races a write can show that write*, in its `-wal` or `-shm`. Two copies a
    moment apart show the same thing, so no storage design removes it.
  - *A process outside the service that holds the database open between writes* keeps the
    `-shm` alive. An operator's `sqlite3` shell left open is one example. While it is
    open, the `-shm` counts writes since that process opened. It holds no page content.
    The README's backup command opens and closes in one step.
  - *Below SQLite*, the blocks of a truncated `-wal`, and old copies of pages an SSD has
    remapped, belong to the filesystem and the disk. The promise covers a copy of the
    files.
  - *Backups already taken* under the earlier layout keep that layout's residue. Nothing
    here can reach them.
