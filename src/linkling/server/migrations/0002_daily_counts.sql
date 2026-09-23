-- Migration 0002 -- daily_counts: the only thing this service records about a click.
--
-- ADR-0004: a number per link per day and nothing else. No IP address, user agent,
-- referrer, cookie or timestamp -- and nothing finer than a day may ever be
-- reconstructable from what is stored. A column added here "for debugging later" is the
-- exact failure the brief names; changing what this table holds means changing the
-- privacy page first. ADR-0014 records the choices below beyond ADR-0005's sketch.
--
-- WITHOUT ROWID, because a rowid is assigned in insertion order: it would order each
-- link's first click of a day against every other link's, which is finer than a day.
-- That removes the ordering from what SQL can read. It does not remove it from the file's
-- raw bytes: SQLite places a page's cells in insertion order, so the page layout still
-- leaks it. ADR-0014 "Left open" lists this and the other raw-file residue.
-- `day` is the UTC date, YYYY-MM-DD (ADR-0004b); the GLOB refuses a timestamp.
-- A row exists only because someone followed the link that day, so count >= 1.

CREATE TABLE daily_counts (
    link_id INTEGER NOT NULL REFERENCES links(id),
    day     TEXT    NOT NULL CHECK (day GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    count   INTEGER NOT NULL CHECK (count >= 1),
    PRIMARY KEY (link_id, day)
) WITHOUT ROWID;

-- Counts die with their link (ADR-0004, retention). Deletion is a tombstone UPDATE, not a
-- row DELETE, so an ON DELETE CASCADE would never fire. This trigger is atomic with the
-- tombstone and fires for any UPDATE that sets deleted_at, whichever code path issues it.
CREATE TRIGGER daily_counts_die_with_their_link
AFTER UPDATE OF deleted_at ON links WHEN NEW.deleted_at IS NOT NULL
BEGIN
    DELETE FROM daily_counts WHERE link_id = NEW.id;
END;
