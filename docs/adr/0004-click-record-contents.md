# ADR-0004 — What is stored about a click, and where it can leak

- Status: Accepted
- Approver: @jpslav
- Date: 2026-09-22

## Context

"What we store about clicks is a promise to people who never agreed to anything. Decide it
once, write it on the privacy page, and don't collect more later without changing that page
first" (`raw/brief.md#L49-L51`). And: "The team can see how many times each link was used,
per day. That's all we count. We don't keep IP addresses, browsers, referrers or anything
that identifies the person clicking, and we say so publicly" (`#L24-L26`).

This is the product's reason for existing (`#L7-L11`), not a feature of it.

## Decision

**Proposed.**

### a. One counter row per (link, day)

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| **A. One row per `(link, day)`, incremented in place** | Nothing finer than a day can ever be reconstructed | Hourly charts, "when today", any per-click analysis | R1 |
| B. One row per click with a timestamp, aggregated on read | A second-precision timestamp on a low-traffic link *is* identifying-adjacent — "the 03:12 click was the one person awake" — and the privacy page would have to say "we store the time of each click" | The brief's "that's all we count" | R1 |
| C. Hourly buckets | The same identifiability worry at low volume, 24× the rows, and "per day" is what was asked | — | R1 |

**Recommend A.** "Per day" (`#L24`) reads as a deliberate coarsening, and A is the only
shape under which the privacy page can say *a number per link per day, and nothing else*.
**B is written down here specifically as rejected**, because it is what anyone building this
reaches for first and it is not obviously wrong until stated this way.

### b. Days are UTC

**The brief does not say.** UTC is deterministic, statable on the privacy page, and never
needs re-bucketing; the cost is that the team's "yesterday" is offset, which is a display
problem rather than a data one. A configurable timezone changed later re-buckets nothing
already stored, which is the trap.

### c. Nothing about a click is written anywhere else — and two places it leaks by default

**This is the part most likely to ship broken, and it was verified by reading uvicorn's
source on `main`, not by recalling it:**

- **`access_log: bool = True`** (`uvicorn/config.py` line 216) and the default access
  formatter is `'%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'`
  (line 94). **Left at defaults, every redirect writes the clicker's IP address to stdout**,
  and Docker's `json-file` driver keeps stdout on disk. A service shipped with default
  logging *keeps IP addresses* while its privacy page says it does not — the exact bug the
  brief describes at `#L37-L38`, present on day one, with nothing in the code that looks
  like a decision to store anything.
- **`proxy_headers` defaults to `True`** with `forwarded_allow_ips` of `127.0.0.1,::1`
  (lines 225, 363). Not a leak by itself, but it means the application *holds* the clicker's
  real address in `request.client`, so any later error reporter or debug dump carries it.

**Proposed: the compose file runs the service with access logging off (or with a format
carrying no `client_addr`), and nothing may log or transmit `request.client`, `User-Agent`
or `Referer`.** Enforced by a test that starts the app with the shipped settings, follows a
link, and greps the captured log for the client address — **printing
`LOG PRIVACY BLIND: no log lines captured` and failing when the capture is empty**, because
an empty log passes a naive grep exactly as a clean one does.

### d. Bots and unfurlers are counted, and the page says so

Chat clients fetch every pasted link, so some counts will be inflated. Filtering them means
*inspecting* the user agent even though it is not kept — which the privacy page would have
to say — and it means somebody maintains a bot list (R4). Proposed: **count every `GET` that
follows a link, inspect nothing, and have the page say so in one true sentence.** This
changes the number the team reads, so it is a question rather than a decision.

## Consequences

- The stored set is exactly the `links` row plus `daily_counts(link_id, day, count)`.
  The promise's boundary is the Linkling container; ADR-0007 covers what sits in front of it
  and ADR-0009 keeps third parties out of the click path.
- The privacy page can state the whole truth in about three sentences.
- Retention is genuinely open and the page must state it (`#L35-L36`) — proposed: counts are
  deleted with their link and otherwise kept, which is the simplest true sentence and makes
  the tombstone of ADR-0005 carry no counts.

## Decision record

Accepted 2026-09-22 by @jpslav, from line comments on [the definition PR](https://github.com/jpslav/tinyworks-program/pull/1). Written by `tools/decision-record.py`; each answer is also in its question file.

- **What is stored about a click?** — **A.** A number per link per day, and nothing else — nothing finer than a day can ever be reconstructed — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074333903)) <!-- decided: 2026-09-17-click-record-and-retention/stored: A -->
- **How long are the counts kept?** — **A.** Until their link is deleted, and otherwise indefinitely — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074334026)) <!-- decided: 2026-09-17-click-record-and-retention/retention: A -->
- **Which timezone decides where one day's count ends?** — **A.** UTC — never needs re-bucketing; the team's "yesterday" is slightly offset from its own — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074334128)) <!-- decided: 2026-09-17-click-record-and-retention/day-boundary: A -->
- **Are automated link previews counted as uses?** — **A.** Count every request that follows a link, inspect nothing, and say so plainly on the privacy page — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074334241)) <!-- decided: 2026-09-17-click-record-and-retention/previews: A -->
