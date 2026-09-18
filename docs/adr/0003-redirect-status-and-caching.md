# ADR-0003 — The redirect status code and its cache lifetime

- Status: Proposed
- Approver: (pending)
- Date: 2026-09-17

## Context

The brief asks for this one in so many words: "Browsers remember some kinds of redirects
forever. If we get that wrong, a link we later fix or delete keeps going to the old place
for people who already clicked it. I want a deliberate decision here, written down"
(`raw/brief.md#L46-L48`).

**What the specification actually says**, read rather than recalled — RFC 9110, fetched
from rfc-editor.org and quoted by line number in the plain-text rendering:

> "Responses with status codes that are defined as heuristically cacheable (e.g., 200, 203,
> 204, 206, 300, **301**, **308**, **404**, 405, **410**, 414, and 501 in this
> specification) can be reused by a cache with heuristic expiration unless otherwise
> indicated by the method definition or explicit cache controls; all other status codes are
> **not** heuristically cacheable." — L6845–6851

301 (L7389), 308 (L7524), 404 (L7597) and 410 (L7686) each carry the "is heuristically
cacheable" sentence. **302 (L7396) and 307 (L7490) do not** — they are in the "all other
status codes" set.

Two consequences the brief only half-names:

1. **A cached redirect is an uncounted click.** The brief wants every use counted per day
   (`#L24`). A browser reusing a cached 301 never contacts the server, so repeat visits by
   the same person are systematically undercounted. That rules out 301 and 308 on the
   counting requirement alone, before the deletion problem is considered.
2. **The dead-link response has the same defect.** 404 and 410 are both heuristically
   cacheable. A link that expires, is clicked once (410 cached), and is then extended or
   re-created keeps reading as gone for that person. So the discipline applies to *every*
   response on the redirect path, not only the 3xx.

## Decision

**Proposed.**

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| A. `301`, no cache headers | Browsers and proxies reuse it indefinitely: deletion and repointing are invisible to prior clickers, and repeat clicks go uncounted | Accurate counts; honest deletion | R1 — nothing to maintain, because the damage is silent |
| B. `301` + `Cache-Control: no-store` | Relies on every cache honouring an explicit control on a status code it is *allowed* to reuse heuristically | — | R1 |
| **C. `302` + `Cache-Control: no-store`** | Every click reaches the server — which is what counting requires anyway; one SQLite read and one write per click | A cached redirect's client-side latency; the "permanent" SEO signal, irrelevant for a team tool | **R1** |
| D. `307` + `no-store` | Identical to C for GET and HEAD; differs only in method preservation for POST, which a clicked link never is | Same as C | R1 |

**Recommend C: `302 Found` with `Location` and `Cache-Control: no-store`. Expired and
deleted links answer `410 Gone` with `no-store`; unknown names answer `404` with
`no-store`.**

Why C and not D: they are equivalent for the only method a clicked link ever uses, and 302
is the more heavily exercised path in every proxy and client ever written. The ADR's value
is that the choice is written down, not that it is clever. Why 410 rather than 404 for a
dead link: it is the truthful code, it lets `demo.sh` assert the exact state the brief asks
it to demonstrate (`#L68`), and once `no-store` is on it there is no extra cost.

**The irony worth stating plainly: the non-cacheable choice is the reversible one.** Nothing
downstream remembers a 302, so moving to 307 or even 301 later is free. A 301 once served
is remembered by clients nobody can reach.

## Consequences

- Every click reaches the service, which is what makes the per-day count true rather than a
  lower bound.
- Deleting or expiring a link takes effect immediately for everyone, including people who
  followed it yesterday — which is the brief's actual requirement at `#L46-L48`.
- The service carries the full click load; there is no cache to hide behind. At team scale
  this is nothing, and ADR-0007 keeps any CDN out of the redirect path for the same reason.

**What would settle it:** follow a `302 + no-store` link twice in one browser profile in
Chrome, Firefox and Safari and check the server saw two requests each time; then the same
with a bare `301` and see whether the second click arrives at all; then `301 + no-store` to
see whether the header rescues it. **The counter is the instrument** — this is the brief's
own "see the count go up" (`#L67`), run twice.
