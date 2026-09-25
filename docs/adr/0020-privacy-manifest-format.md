# ADR-0020 — The privacy manifest ships as package data, in a format whose retention claims a test enforces

- Status: Proposed
- Approver: (pending)
- Date: 2026-09-25

## Context

ADR-0008 §c (Accepted) puts the "what we store" truth in a machine-readable file in
`linkling-api`, named `privacy/what-we-store.json`, holding "each table, each column, its
purpose, its retention". The running service serves the same file at `/-/privacy.json`, so
the *deployed* copy can be checked and not only the repository's. It names two tests and says,
in its own words:

> **Two tests in the API repo, and both must guard against emptiness:**
>
> 1. The schema test introspects a freshly migrated database and asserts the column set equals
>    the manifest's, in both directions — **and asserts both sides are non-empty**, because an
>    unmigrated database and a missing manifest agree on nothing and would otherwise pass.
> 2. The retention test asserts the behaviour the manifest claims, such as a tombstone
>    dropping its target.

ADR-0008 leaves two things open, and code outside this repository will parse both. Under
ADR-0008's decision record (option A), `demo.sh` diffs this file against the privacy page in
`linkling-web`. The first open thing is where the file sits in the repository, so the service
can find it however it is installed. The second is the JSON format: its field names and its
retention vocabulary.

Two facts about the service decide the location. The image installs the package
non-editably from `src/` (`Dockerfile`: `COPY src ./src`, then `pip install … .`), and
`.dockerignore` excludes `docs` and `tests`. So a file at the repository root is not in the
image unless the Dockerfile copies it, and it is not in site-packages unless it is package
data.

## Decision

### a. Location: package data at `src/linkling/server/privacy/what-we-store.json`

It is read with `importlib.resources` (`linkling.server.privacy.manifest_bytes`) and declared
in `pyproject.toml`'s `[tool.setuptools.package-data]`. This keeps ADR-0008 §c's name,
`privacy/what-we-store.json`, but relative to the server package instead of the repository
root. The file is still "in `linkling-api`".

The options, ranked by the recurring human work each one leaves behind, then by cost and
correctness:

| Option | Recurring work | Cost and correctness |
|---|---|---|
| **A. Package data, read with `importlib.resources`** (chosen) | None. One file, one copy | One `package-data` entry. One code path works in an editable install, a non-editable install and the image |
| B. `privacy/` at the repository root, `COPY`'d by the Dockerfile and found through a new environment variable | None, but every deployment inherits a new setting | A non-editable `pip install` without the variable cannot find the file, which gives a second way to start a service that cannot serve its manifest |
| C. A repository-root file plus a copy in the package, and a test that asserts the two are byte-identical | Someone re-copies the file on every edit, and the test forces them to | Two copies of one truth: the drift ADR-0008 §c exists to prevent |

`create_app` reads the file once, when the application is built. A missing manifest stops the
service from starting, where the alternative is a 500 on the day someone checks.

### b. Format

```json
{
  "format": 1,
  "tables": {
    "<table>": {
      "purpose": "<sentence>",
      "columns": {
        "<column>": {
          "purpose": "<sentence>",
          "retention": "<sentence>",
          "on_delete": "kept | set | emptied | nulled | removed",
          "on_expiry": "kept | set | emptied | nulled | removed"
        }
      }
    }
  }
}
```

- `format` is the version of this shape. Code that parses the file should check it, so a later
  incompatible change bumps it rather than changing meaning in place.
- `purpose` and `retention` are sentences for a person to read. They are what the privacy page
  (LL-012) is compared against.
- `on_delete` and `on_expiry` say what deleting the link, and the link expiring, does to that
  column of the link's rows. They are what a test checks:

| Word | Meaning, checked on the link's rows before and after the event |
|---|---|
| `kept` | The value is unchanged |
| `set` | There was no value before, and there is one after |
| `emptied` | A non-empty value became the empty string |
| `nulled` | A value became NULL |
| `removed` | The link's rows in that table are gone. It is all or nothing per table: a table cannot claim `removed` for some columns and not others |

`set` was added while the retention test was being built, after the implementation plan listed
four words. `links.deleted_at` is NULL on a live link and gets a value when the link is deleted,
and none of the other four describes that.

**Every table the service creates is declared, `schema_version` included.** The schema test
compares every table except SQLite's own `sqlite_*` ones. Leaving `schema_version` out would
need an exception list, and an exception list is how the first real omission would get through.

### c. The tests are data-driven from the manifest

`tests/test_ll015_privacy_manifest.py` holds both of ADR-0008 §c's tests:

1. **Schema.** It migrates a fresh database and compares the `(table, column)` set from
   `sqlite_master` and `PRAGMA table_xinfo` with the manifest's, in both directions.
   `table_xinfo`, not `table_info`, because `table_info` leaves out generated columns, and a
   `STORED` one is written to the file. An empty
   side fails with a message beginning `blind:`, never with a pass.
2. **Retention.** It reads each column's claim out of the manifest and checks that claim
   against a real delete and a real expiry, for every column the manifest declares. It
   hard-codes no column's behaviour. It guards against emptiness in three ways, and each
   fails the test rather than passing it: a manifest with no columns, a table holding no
   rows for the link before the event, and a table the test does not know how to relate to
   a link, which it names.

So a new column fails the schema test until the manifest declares it, and the declaration
carries an `on_delete` and an `on_expiry` claim that the retention test then enforces. Nobody
has to remember to update the file.

### d. Ruling: a tombstone keeps `expires_at`

ADR-0013's "Left open" paragraph asked whether ADR-0005's "a tombstone stores only a name and
a date" should read "and its dates" now that `expires_at` exists. The manifest has to state an
`on_delete` for every column, so this ADR answers: **a deleted link keeps `expires_at`**
(`on_delete: "kept"`), which is what the code already does
(`tests/test_ll010_link_expiry.py::test_a_tombstoned_links_expiry_is_left_as_it_was`). The
same goes for `id` and `created_at`, and `deleted_at` is `set`. In full, a tombstone keeps
`id`, `name`, `created_at`, `expires_at` and `deleted_at`, and ADR-0005's Consequences sentence
understates it.

## What this ADR does not decide

Two owner questions are open in `products/linkling/STAKEHOLDER-QUEUE.md` in the program
repository. This ADR answers neither.

- **Whether a deleted link's target is stored as the empty string or as NULL.** The manifest
  says what the code does today, `on_delete: "emptied"`. If the owner rules NULL, three
  things change together: one word of the manifest, the assignment in `links.delete`, and a
  migration, because `links.target` is `NOT NULL` (`0001_links.sql`). The retention test needs
  no edit, and it fails until the manifest and the code agree.
- **Whether the privacy promise covers the database file's raw bytes.** The manifest declares
  what the service stores and can read back through SQL, table by table. It says nothing either
  way about freed pages or the order of cells in a page.

## Consequences

- The field names above are a contract with `demo.sh` (LL-008) and the privacy page (LL-012).
  Adding a field is additive. Renaming or removing one changes `format`.
- The deployed service always serves the manifest it was built with. `scripts/compose-smoke.sh`
  checks that the image's copy is byte-identical to the checkout's, and
  `scripts/no-third-party-check.sh` exercises the route.
- A column added or removed without the same change to the manifest cannot pass CI. Nor can
  a change to what deleting or expiring a link does to a column.
