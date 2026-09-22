# ADR-0005 — The data model, and what deletion does to a name

- Status: Accepted
- Approver: @jpslav
- Date: 2026-09-22

## Context

SQLite cannot alter a constraint in place, so a wrong `UNIQUE` or a missing column means
rebuilding a table — against a database that is the only copy of every link anyone has
printed. The shape is fixed by the brief: custom or generated name (`#L15`), a target
(`#L18`), optional expiry (`#L20`), deletion (`#L23`), counts per day (`#L24`), SQLite
(`#L55`).

It also carries a decision **nobody's list contained**, and which matters more than it
looks: *what happens to a name when its link is deleted.* The brief lists no edit or
repoint feature — deliberately, given `#L43-L44`. But delete-then-recreate under the same
name **is** repointing by the back door, and it produces exactly the outcome `#L42-L44`
forbids: a printed poster that silently starts pointing somewhere else.

## Decision

**Proposed.**

```
links(id INTEGER PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,   -- already lowercase, per ADR-0001c
      target TEXT NOT NULL,        -- stored byte-exact, never normalised
      created_at TEXT NOT NULL,    -- ISO-8601 UTC
      expires_at TEXT NULL,        -- NULL = forever
      deleted_at TEXT NULL,        -- tombstone; see below
      created_by TEXT NULL)        -- free-text label the CLI sends
daily_counts(link_id INTEGER, day TEXT, count INTEGER NOT NULL,
             PRIMARY KEY(link_id, day))
schema_version(version INTEGER)    -- numbered migrations from day one
```

Alternatives, and why each is rejected:

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| `name` as the primary key, no surrogate id | Tombstones and any future rename become key rewrites across `daily_counts` | Cheap tombstones | R1 |
| Normalising the target on store — trailing slash, lowercased host, sorted query | Loses the exact URL the maker supplied; `#L43-L44` says never change where a link points, and a normaliser's idea of "equivalent" is not the target site's | Byte-exact fidelity, permanently | R1 |
| No `created_by` now, add it later | Every link made before the column exists is unattributed forever | Ever answering `#L23`'s "a link **they** made" | R1 |
| No migration mechanism — "it is only SQLite" | The first schema change is performed by hand on a live volume | — | **R4** |

### What deletion does to the name

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| A. Hard delete, name immediately free | A poster pointing at `q3-plan` can be captured by the next `q3-plan` | Ever answering "was this name once a link?" | R1 |
| **B. Tombstone — `deleted_at` set, `target`, `created_by` and counts dropped, name reserved forever; generated names never re-issued** | `q3-plan` next year has to be `q3-plan-2027` | Reuse — but see below | R1 |
| C. B, plus an explicit `--reuse` flag for a key holder | An escape hatch that becomes a habit | — | R1 |

**Recommend B**, for the same asymmetry that decides ADR-0003: **loosening is additive.**
C can be added later without touching a single existing row; A cannot be undone for names
already reused. Expiry is the same state machine — an expired link keeps its row and its
name, answers 410, and is removed only by an explicit delete.

## Consequences

- Expiry precision is **seconds**, not days, because `demo.sh` must make a link that expires
  and show it stop working inside one run (`#L68`).
- The count increment is **synchronous** in the redirect, because the demo checks the count
  immediately after following (`#L67`); a buffered counter is faster but makes the demo racy
  and loses counts on a crash. SQLite in WAL mode with a busy timeout is a tuning choice,
  not a door, and is deliberately not carried as one.
- A tombstone stores only a name and a date, which the privacy page states in one clause.

**What would settle the tuning question:** with WAL on, how many redirects per second does
one worker sustain on the target machine with the synchronous increment? If it is in the
thousands, the asynchronous option is never needed and should not be built.

## Decision record

Accepted 2026-09-22 by @jpslav, from line comments on [the definition PR](https://github.com/jpslav/tinyworks-program/pull/1). Written by `tools/decision-record.py`; each answer is also in its question file.

- **Can the name of a deleted link ever be used again?** — **A.** Never — a deleted name is reserved forever and generated names are never re-issued; next year's is `q3-plan-2027` — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074335893)) <!-- decided: 2026-09-17-tenancy-and-name-reuse/name-reuse: A -->
