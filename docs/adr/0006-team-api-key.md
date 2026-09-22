# ADR-0006 — Auth, the stats page, and tenancy

- Status: Accepted
- Approver: @jpslav
- Date: 2026-09-22

## Context

"Making, deleting and listing links needs a team API key. Following a link needs nothing"
(`raw/brief.md#L29`). Wanting an account per person is a named reason the hosted
alternatives were rejected (`#L8`), so per-person accounts are ruled out by implication.

This is Phase A because the credential pattern is installed on every laptop and the tenancy
shape is in a `UNIQUE` constraint.

## Decision

**Proposed.**

### a. One key, from the environment

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| **A. One key in `LINKLING_API_KEY`, set in the compose environment** | Rotation means changing the variable, restarting, and everyone updating their CLI; `created_by` is self-asserted and unenforced | Per-person revocation | R1 |
| B. Key generated on first boot into the data volume, printed once | As A, plus "where did the key go" on every fresh deployment | Same | R1 |
| C. A `keys` table with labels and an admin command | Real per-person revocation and a trustworthy `created_by`; more code and an admin surface | Nothing | R1 |

**Recommend A**, taken literally from `#L29`, with `created_by` recorded now as a free-text
label the CLI sends. Header: `Authorization: Bearer <key>` — the standard shape, which every
HTTP client and every reverse proxy already understands. C is additive later: the
environment key simply becomes the first row.

**A contradiction the brief does not resolve, and which is the owner's to settle:** `#L23`
says someone can delete "a link they made", while `#L29` gives the whole team one key. With
one shared key, "they" can be *recorded* but not *enforced*. Either anyone holding the key
may delete anything — recommended, and the honest reading of `#L29` — or deletion is
restricted to the maker, which requires per-person keys and is option C.

### b. The stats page sits behind the same key, via HTTP Basic

`#L27` says there is a page; `#L29` says listing needs the key; **the brief does not say**
whether the page is gated. Leaving it open means anyone who finds `/-/stats` sees every
target, including internal document URLs. A `?key=` in the URL is rejected outright — it
lands in browser history and in any proxy log. Basic auth with the key as the password is
browser-native, needs no session code, and works with `curl -u`.

### c. One team per deployment

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| **A. One team per deployment; `name` globally unique; a second team is a second stack** | Nothing | Several teams sharing one instance without rebuilding the table — `UNIQUE(name)` would become `UNIQUE(team, name)` | R1 |
| B. A `team_id` column now, always 1 | Code paths carrying a constant | Nothing | R1 |
| C. Multi-tenant from the start | Accounts, per-team keys, host-based lookup — everything `#L8` says this product is not | — | R4 |

**Recommend A**, because `#L7` and `#L10` describe a per-team deployment. **But this hinges
on one question that must not be guessed:** "custom domains per team" (`#L62`) could mean
several teams on one Linkling, or one team choosing its own domain. The first reading makes
B worth its trivial cost; the second makes A final. That is the difference between a column
and an architecture, and it is the owner's to answer.

## Consequences

- Writes without the key are refused; following a link needs no credential and sets no
  cookie. Those two must be asserted **together**, because either alone passes against a
  service that refuses everything or refuses nothing.
- Rotating the key locks out the whole team until everyone updates — acceptable at this
  scale, and the reason to revisit option C if the team grows or turns over.

## Decision record

Accepted 2026-09-22 by @jpslav, from line comments on [the definition PR](https://github.com/jpslav/tinyworks-program/pull/1). Written by `tools/decision-record.py`; each answer is also in its question file.

- **Does one Linkling serve one team, or can several teams share one?** — **A.** One team per Linkling, choosing its own domain — a second team is a second compose stack, and names are unique per deployment — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074335757)) <!-- decided: 2026-09-17-tenancy-and-name-reuse/tenancy: A -->
- **With one shared team key, who may delete a link?** — **A.** Anyone holding the team key may delete any link; its maker is recorded, not enforced — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074336017)) <!-- decided: 2026-09-17-tenancy-and-name-reuse/who-deletes: A -->
