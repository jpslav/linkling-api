# ADR-0014 — daily_counts is a WITHOUT ROWID table keyed by (link, UTC day), its counts die with their link by trigger, and a follow is counted by one guarded upsert

- Status: Accepted
- Approver: pm-3
- Date: 2026-09-25

## Context

ADR-0004 (Accepted) settles *what* is stored about a click: a number per link per day and
nothing else, with nothing finer than a day ever reconstructable. The counts are kept until
their link is deleted, UTC decides where a day ends, and every request that follows a link
is counted, inspecting nothing. ADR-0005 (Accepted) sketches the table as
`daily_counts(link_id INTEGER, day TEXT, count INTEGER NOT NULL, PRIMARY KEY(link_id, day))`
and makes the increment synchronous in the redirect.

Neither ADR fixes the table's row storage, its constraints, how the day is encoded or how
retention is enforced. Once counts exist, changing any of those means rebuilding the
table. Two facts from the code shape the answers:

- **Deletion is a tombstone, not a row delete.** `links.delete` is an `UPDATE` that sets
  `deleted_at` and keeps the row (`src/linkling/server/links.py`, `delete`). So an
  `ON DELETE CASCADE` foreign key from `daily_counts` would never fire.
- **Expiry is computed, not stored** (LL-010, ADR-0013): `links.lookup` decides `expired`
  against the clock, and `follow()` answers 410 for it.

This was decided with LL-004 and LL-006, whose plan is the `PLAN:` comment on
jpslav/linkling-api#9.

## Decision

**Proposed.** It is implemented as `src/linkling/server/migrations/0002_daily_counts.sql`
and `src/linkling/server/counts.py`. No field beyond ADR-0005's three is added.

| Choice | Recommended | Alternative | Why |
|---|---|---|---|
| Row storage | **`WITHOUT ROWID`** | an ordinary rowid table (ADR-0005's sketch as printed) | Rowids are assigned in insertion order. In a rowid table they would order each link's first click of a day against every other link's, which is finer than a day. `WITHOUT ROWID` stores rows by the key alone, so SQL cannot read that order. The file's raw bytes still can: see "Left open" below. |
| Day encoding | **`TEXT`, `YYYY-MM-DD`, the UTC date** | `INTEGER` days since the epoch | This is the form R-008's verify line compares against (`date -u +%F`) and what a person reading the file with `sqlite3` understands. A `CHECK (day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')` refuses a timestamp, which is the exact failure ADR-0004 rejects. |
| Retention | **A trigger, `daily_counts_die_with_their_link`, `AFTER UPDATE OF deleted_at ON links`** | the same `DELETE` inside `links.delete`, in a transaction | The trigger is atomic with the tombstone by construction. It fires for any `UPDATE` that sets `deleted_at`, whichever code path issues it. It is also visible in the schema, where LL-011's page-versus-schema check (not yet built) can read it. |
| The increment | **One guarded upsert: `INSERT … SELECT id, ?, 1 FROM links WHERE name = ? AND deleted_at IS NULL ON CONFLICT(link_id, day) DO UPDATE SET count = count + 1`**; when it changes nothing, the follow answers 410. ADR-0021 appends `RETURNING count`, so the new count can decide whether the file must be rewritten. | look the id up, then insert by id | A follow that saw the link live and then lost a race with a delete would otherwise write a count onto the tombstone after the trigger had cleared it. As a single statement, SQLite serialises the two. Every 302 is counted exactly once, and no count exists for a deleted link. |
| `count >= 1` | **`CHECK`** | none | A row exists only because someone followed the link that day. |

**Expiry.** The count is taken after `follow()`'s expiry check, so a follow of an expired
link writes nothing. The guard does not re-check expiry: a follow that passed that check
a moment before `expires_at` got its 302 and is counted. An expired link's counts are
**kept**, because expiry is not deletion and ADR-0004 keeps counts until their link is
deleted.

**The day comes from the clock in `counts._epoch_seconds`**, converted with
`datetime.fromtimestamp(t, timezone.utc)`, never from the host's timezone.

## Consequences

- The stored set about a click is exactly `daily_counts(link_id, day, count)`.
  `tests/test_ll006_daily_counts.py` asserts the column list, the absence of `rowid`,
  and the whole list of tables and triggers, so an added column or table fails the suite.
- Deleting a link removes its counts in the same statement. Expiring it does not.
- A count write that fails makes the follow a 500 rather than an uncounted redirect.
  ADR-0005's synchronous increment already implies this.

## Left open: what the database file's raw bytes keep

> **Answered 2026-09-25 by the owner: the promise covers the raw bytes.** In his words, *"Privacy promises should be real."* He overruled the recommendation below to scope it to what SQL can read. The question and answer are in the program repo at `products/linkling/STAKEHOLDER-QUEUE.md`, under "Does the privacy promise cover the database file's raw bytes, or only what the service can read?" (asked 2026-09-23). **[ADR-0021](0021-db-file-canonical-after-every-write.md) closes all four sites below** (LL-033). This section is kept as the record of what was found.

This ADR guarantees what SQL can read. ADR-0004's "nothing finer than a day can ever be
reconstructed" could also be read to cover the file's raw bytes: a copy of `linkling.db`,
a backup or a volume snapshot. At that level, SQLite keeps more than the schema shows.
These are the sites, found in this PR's review (round 2) and confirmed by probes:

1. **Freed cells after the retention trigger.** A deleted link's count records stay in
   the page's free space until they are overwritten. A probe followed a link five times
   and deleted it: SQL returned no rows, and the file's bytes still held the record with
   count 5. `PRAGMA secure_delete=ON` on every connection zeroes freed space.
2. **Cell order within a page.** SQLite writes a page's cells in insertion order, so the
   byte offsets give back the order in which links got their first follow of the day.
   Four links first followed in the order ddd, bbb, aaa, ccc read back as `ddd, aaa, ccc`
   in true relative order; bbb's cell had moved when its count grew. `secure_delete` does
   not help. A `VACUUM` rewrites pages in key order, but it has to run repeatedly, and
   the order it erases is rebuilt the next day.
3. **WAL frames.** The `-wal` file holds a page image per commit until checkpoint. This
   service opens a connection per request, and the last connection to close checkpoints
   and removes the WAL. So the WAL holds this history only while requests overlap. What
   survives after that is filesystem residue, below SQLite.
4. **A tombstone's old target** stays in freed space the same way as (1). This predates
   this ADR (LL-007's tombstone), and `secure_delete` would cover it too.

Whether the promise covers the file's raw bytes decides what the privacy page (LL-012)
may say. It is a promise to people who never agreed to anything, so the owner decides it.
This ADR does not.

## Decision record

Accepted 2026-09-25 by `pm-3`, the program manager, under the 2026-09-22 ruling in `company/DECISIONS.md` of the program repo ("What reaches the owner is user-visible impact, not cost to undo"): the counts table's row storage, day encoding, retention trigger and increment statement are not something a user of Linkling perceives, so they are Claude's, recorded here. Proposed since 2026-09-23. Checked against `main` at `42444f5` before accepting: `src/linkling/server/migrations/0002_daily_counts.sql` declares `daily_counts` `WITHOUT ROWID` with `PRIMARY KEY (link_id, day)`, the two `CHECK`s and the trigger `daily_counts_die_with_their_link`, and `_INCREMENT` in `src/linkling/server/counts.py` is the one guarded upsert. The trigger also carries `WHEN NEW.deleted_at IS NOT NULL`, a detail the table above leaves out.

**Not accepted: "Left open".** That section is the owner's open question, whether the privacy promise covers the database file's raw bytes. It is in `products/linkling/STAKEHOLDER-QUEUE.md` of the program repo (asked 2026-09-23, blocks LL-012). Accepting the rest of this ADR does not answer it, and neither does this record. Recorded by the `w-LL-030` session (LL-031) in jpslav/linkling-api#20.
