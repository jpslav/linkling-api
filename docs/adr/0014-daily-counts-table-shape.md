# ADR-0014 — daily_counts is a WITHOUT ROWID table keyed by (link, UTC day), its counts die with their link by trigger, and a follow is counted by one guarded upsert

- Status: Proposed
- Approver: (pending)
- Date: 2026-09-23

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
| Row storage | **`WITHOUT ROWID`** | an ordinary rowid table (ADR-0005's sketch as printed) | Rowids are assigned in insertion order. In a rowid table they would order each link's first click of a day against every other link's, which is finer than a day. `WITHOUT ROWID` stores rows by the key alone. |
| Day encoding | **`TEXT`, `YYYY-MM-DD`, the UTC date** | `INTEGER` days since the epoch | This is the form R-008's verify line compares against (`date -u +%F`) and what a person reading the file with `sqlite3` understands. A `CHECK (day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')` refuses a timestamp, which is the exact failure ADR-0004 rejects. |
| Retention | **A trigger, `daily_counts_die_with_their_link`, `AFTER UPDATE OF deleted_at ON links`** | the same `DELETE` inside `links.delete`, in a transaction | The trigger is atomic with the tombstone by construction and holds for any writer that sets `deleted_at`. It is also visible in the schema, which LL-011's page-versus-schema check reads. |
| The increment | **One guarded upsert: `INSERT … SELECT id, ?, 1 FROM links WHERE name = ? AND deleted_at IS NULL ON CONFLICT(link_id, day) DO UPDATE SET count = count + 1`**; when it changes nothing, the follow answers 410 | look the id up, then insert by id | A follow that saw the link live and then lost a race with a delete would otherwise write a count onto the tombstone after the trigger had cleared it. As a single statement, SQLite serialises the two. Every 302 is counted exactly once, and no count exists for a deleted link. |
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
- **Left open, named rather than decided here:** SQLite's freed pages keep deleted bytes
  (a deleted link's counts, a tombstone's old target) until they are overwritten.
  `PRAGMA secure_delete` would zero them. If resolving that changes what the privacy page
  says about retention, it is a promise to people who never agreed to anything, and the
  owner decides it.
