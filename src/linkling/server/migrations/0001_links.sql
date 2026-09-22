-- Migration 0001 -- the links table, exactly as ADR-0005 prints it.
--
-- daily_counts is deliberately absent: what is stored about a click is a privacy
-- promise (ADR-0004), and LL-006 adds that table as its own numbered migration.
-- Adding a table later is additive; this one is the table that cannot be rebuilt
-- once links are in the wild.

CREATE TABLE links (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,   -- already lowercase, per ADR-0001c
    target TEXT NOT NULL,        -- stored byte-exact, never normalised
    created_at TEXT NOT NULL,    -- ISO-8601 UTC
    expires_at TEXT NULL,        -- NULL = forever; LL-010 is what sets it
    deleted_at TEXT NULL,        -- tombstone: the name stays reserved forever
    created_by TEXT NULL         -- free-text label the CLI sends; recorded, not enforced
);
