# ADR-0001 — The shape of the short URL

- Status: Proposed
- Approver: (pending)
- Date: 2026-09-17

## Context

The brief's first stated anxiety: "Short links get pasted everywhere — slides, printed
posters, other people's docs. Once one is out there we can never change what it looks like
or where it points without breaking things we don't control. Think hard about the shape of
the URL and the made-up names before anything ships" (`raw/brief.md#L42-L45`).

No migration reaches a printed poster. This decision is carried by every link ever issued.

It decomposes into five sub-decisions, each one-way in its own right.

## Decision

**Proposed, not settled.** Five parts:

### a. Names live at the root — `https://<host>/<name>`

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| **A. Root — `/<name>`** | Shares the root namespace with every route the service will ever need (solved by (b)) | Nothing, given (b) | **R1** |
| B. Prefix — `/l/<name>` | Two extra characters on every printed link, forever | Nothing; it only makes every link longer for the product's life | R1 |
| C. Subdomain — `<name>.<host>` | Wildcard DNS and wildcard TLS at every deployment; dots make reading aloud worse | Name grammar becomes DNS rules | R4 — wildcard cert renewal, per deployer |

**Recommend A.** "Short" is the first adjective the brief uses about a generated name
(`#L16`), and B pays two characters per link forever to solve a problem (b) solves for free.

### b. The service's own routes live under a prefix custom names may not use

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| A. A reserved-word list at the root (`/api`, `/stats`, `/health` …) | Every future route is a new reserved word, and each addition may collide with a name somebody already made | Any future route whose name is already a live link | **R4** — somebody checks the list against live names before adding any route |
| **B. A grammar-guaranteed prefix: custom names may not begin with `-`, so everything of the service's own lives under `/-/…`** | One slightly ugly stats URL that only the team ever types | Nothing — routes can be added forever, collision-free by construction | **R1** |
| C. Admin API on a second port or hostname | Two things to expose and terminate TLS for at every deployment | Nothing | R4, per deployer |

**Recommend B.** It is the only option under which the route set can grow without ever
consulting the live link set. The name grammar in (d) has to forbid a leading hyphen
anyway, so the guarantee is free.

### c. Names are case-insensitive, folded to lowercase

| Option | Costs | Forecloses | Recurring |
|---|---|---|---|
| **A. Case-insensitive, folded to lowercase on create and lookup** | None | Telling `Q3-Plan` from `q3-plan`, which nobody wants on a poster | R1 |
| B. Case-sensitive (RFC path semantics) | A slide or a spoken link loses case; `Q3-Plan` on a poster 404s | Moving to case-insensitive later, which collides existing pairs with no clean migration | R1 |

**Recommend A**, and note *why this is a door at all*: only A keeps the other option open.
Lowercase-only data can become case-sensitive later without collision; the reverse cannot.

### d. Custom-name grammar

`^[a-z0-9]([a-z0-9-]*[a-z0-9])?$`, 1–64 characters, ASCII, no leading or trailing hyphen.
The brief's own example is `q3-plan` (`#L15`). The one-way half is what is *permitted*:
anything allowed now cannot be withdrawn without breaking issued links, so the grammar
starts narrow. Widening it later (Unicode, IDN) is additive and is not a door.

### e. Query strings and trailing slashes

**The brief does not say.** Proposed: a query string on the short link is dropped, and
`/<name>/` is treated as `/<name>`. The target already carries its own "tracking-laden"
parameters (`#L7-L8`), and merging two query strings is ambiguous in a way nobody will
remember. **This part is not genuinely irreversible** — it is here because "what a link
does when opened" is one promise and splitting it across documents is how half of it gets
forgotten.

## Consequences

- Every short link reads `https://<host>/<name>`; the stats page is `https://<host>/-/stats`
  and the API is under `https://<host>/-/api/`.
- A future route can never collide with a live link, so no one has to check.
- The host itself is not decided here — it comes from the hosting decision (ADR-0007) and
  from the owner, who is the only party who knows the domain. Until it is chosen, every
  `verify:` line in `REQUIREMENTS.md` refers to `$LINKLING_BASE` rather than a literal.
- (c) is the sub-decision most likely to be waved through as obvious and is the one whose
  alternative can never be recovered.

**What would settle the only empirical question here:** whether anyone on the team
currently pastes short links containing capitals — readable from the team's own chat
history, and it bears only on (c).
