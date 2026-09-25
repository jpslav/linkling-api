# ADR-0013 — The create request's expiry value is an absolute ISO-8601 UTC timestamp, not a relative duration

- Status: Accepted
- Approver: pm-3
- Date: 2026-09-25

## Context

ADR-0011 names the field this item adds — "LL-010 will add `expires`" — and fixes that
adding it is additive, but it does not say what a caller puts *in* that field. Two other
places in this program already lean on a shape for it without deciding one:
`questions/2026-09-17-cli-verbs-and-privacy-manifest.md` sketches the CLI (LL-003,
unbuilt) as `linkling make <url> [--expires 7d]` — a relative duration, convenient at a
terminal — while migration `0001_links.sql` types `expires_at TEXT NULL` identically to
`created_at TEXT NOT NULL`, the sibling column whose own comment reads "ISO-8601 UTC".
Something has to turn one shape into the other, and where that conversion happens is what
this ADR fixes before the follow path's boundary check is written against it.

## Decision

**Proposed.**

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| A. The API accepts a relative duration (`"7d"`, `"3h"`) and computes the absolute instant server-side | A duration grammar to write, test and version in the server; ambiguity the server now owns — calendar days vs. 86400s, months, years | — | R1+, open-ended as more units are asked for |
| **B. The API accepts an absolute ISO-8601 UTC timestamp**, the same shape `created_at` is already stored and printed in | None server-side; a relative convenience (the CLI's `--expires 7d`) does one `now + duration` computation client-side before the call | — | none |
| C. Accept either shape, disambiguated by parsing | Two parsers, two failure surfaces, and a live question about which wins on an ambiguous string | — | R1 — the double surface is permanent API contract |

**Recommend B.** `expires_at` is already typed identically to `created_at` — `TEXT`,
ISO-8601 UTC — so a value entering the column in the shape it is stored and compared in is
one less translation to get wrong, and the follow path's boundary check (`expires_at` vs.
"now") stays a plain string comparison between two values of the same shape, with no
parsing on the hot path at all. It also keeps this item's mechanism separate from LL-003's
convenience: the CLI computes the absolute instant once, client-side, from whatever
phrasing it wants to offer (`7d`, a bare date, a raw timestamp), and the API's contract
stays "an ISO-8601 UTC instant after which this link is gone" — one shape, matching every
other timestamp this service stores.

Concretely: `POST /-/api/links` accepts an optional `expires` field in the exact shape
`created_at` is rendered in (`%Y-%m-%dT%H:%M:%SZ`) — checked by round-tripping the parsed
value back through the same formatter and requiring it to reproduce the input byte-for-byte,
because a permissive parser is not the same guarantee as an exact shape: `datetime.strptime`
alone accepts a single-digit month, a space-padded day, lowercase literals and non-ASCII
decimal digits, none of which `_now()` ever prints, and the follow path's boundary check
depends on every stored value sharing one shape to sort correctly as plain strings. A value
that does not reproduce is refused with `422`, the same status ADR-0011b already uses for a
request the service cannot honor.

`expires` must also name an instant strictly after the moment of the call, refused with
`422` otherwise. Without this, a mistyped year creates a link that answers `410` from its
very first follow, its name reserved forever exactly as ADR-0005 reserves one after any
other mistaken create — there is no undo once the row exists. Omitting `expires` still
means forever — the owner's `expiry = A` answer, unaffected by this ADR.

**What would settle it:** LL-003 is where a relative convenience actually gets built; if
that item finds the client-side conversion awkward, or a second caller besides the CLI
wants duration math, that is grounds to revisit. One drafted, unbuilt caller wanting a
convenience is not yet two real callers needing the same mechanism.

## Consequences

- LL-003 can offer whatever relative syntax it likes as long as it resolves to an
  absolute UTC instant before the HTTP call; the API never parses a duration string.
- `expires_at` in the database and `expires` in the request are the same textual shape,
  so the boundary check in the follow path is a string comparison — no timezone or
  duration math on the request path, and nothing to get wrong at read time that was not
  already gotten right at write time.
- Widening the accepted format later (a bare date, a duration suffix) is additive;
  today's exact shape does not need to be un-accepted for that to happen.

**Left open, named rather than decided here:** ADR-0005's tombstone lists `target`,
`created_by` and daily counts as what a delete drops; it says nothing about `expires_at`,
which did not functionally exist when it was written. This item leaves a tombstoned link's
`expires_at` exactly as it was (pinned by a test, not changed), rather than deciding by
implication whether ADR-0005's "a tombstone stores only a name and a date" should now read
as "and its dates."

## Decision record

Accepted 2026-09-25 by `pm-3`, the program manager, under the 2026-09-22 ruling in `company/DECISIONS.md` of the program repo ("What reaches the owner is user-visible impact, not cost to undo"): the shape of the API's `expires` field is not something a user of Linkling perceives, because team members reach expiry through the CLI (LL-003), which can accept friendlier forms (`7d`, a bare date) and convert them to an absolute instant before the HTTP call. The CLI owns any friendlier input; this ADR fixes only what the API takes. Proposed since 2026-09-23. Checked against `main` at `ac4e085` before accepting: `_validate_expires` in `src/linkling/server/app.py` parses `expires` with `links.TIMESTAMP_FORMAT` (`%Y-%m-%dT%H:%M:%SZ`), answers `422` to a value that does not reproduce byte-for-byte through `strftime` and to one that is not strictly after the request's `now`, and `links.is_expired` compares `expires_at` with `_now()` as plain strings. The CLI is not built (LL-003 is blocked), so what it will accept is its own to decide when it is.

**Not ruled on: "Left open".** That section names a gap between ADR-0005's tombstone and `expires_at`, and this record does not close it. `tests/test_ll010_link_expiry.py::test_a_tombstoned_links_expiry_is_left_as_it_was` pins today's behaviour, that the expiry survives a delete, so a change to it is a visible diff. Recorded by the `w-LL-032` session (LL-032) in jpslav/linkling-api#22.
