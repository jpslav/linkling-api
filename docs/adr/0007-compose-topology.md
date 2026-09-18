# ADR-0007 — Hosting topology, where the database lives, and backups

- Status: Proposed
- Approver: (pending)
- Date: 2026-09-17

## Context

"It should run with a single `docker compose up`" (`raw/brief.md#L55`) is the whole of what
the brief says about where this runs, which makes everything else a decision rather than a
given. Two things make it Phase A: the topology determines the **origin** in every short
link ever issued, and it determines **where the SQLite file lives** — and that file is the
only copy of every link anyone has printed.

## Decision

**Proposed.**

### a. One service, built locally, database on a bind mount

One compose service built from the repository's own `Dockerfile`, so `docker compose up` is
genuinely single-step with no registry to publish to. SQLite on a **bind mount**
(`./data:/data`) rather than a named volume: a named volume survives `down` but is invisible
to a person looking for "the database", whereas a bind mount makes the backup target a file
with a path. No reverse proxy inside the compose file — TLS and the public hostname belong
to whoever deploys it — and therefore a `LINKLING_PUBLIC_URL` variable, so the CLI prints
the real short URL rather than deriving it from a `Host` header it cannot trust.

None of (a) is irreversible. It is recorded because (b) and (c) hang off it.

### b. Backups — the door the brief implies and never names

| Option | Costs | Blind failure | Recurring |
|---|---|---|---|
| A. None — "it is only a team tool" | One disk failure retires every poster the team has printed | — | R1, until it is not |
| **B. A documented `sqlite3 … ".backup"` command in the README** | Somebody has to run it | It is obvious when it was last run: the file is there or it is not | **R4** |
| C. A replication sidecar to object storage | A third party, a bucket and credentials to provision, and a restore runbook | **Its failures surface nowhere.** There is no channel on this box, so replication that silently stopped looks exactly like replication that is working | R2 in theory, **R4 in practice** without an alert surface |
| **D. A `linkling backup` CLI verb streaming a consistent copy over the API** | The person still runs it, but it composes with any scheduler the team already has | Exits non-zero, visibly | **R3** |

**Recommend B and D for the first release, with C as a post-launch candidate.** This is the
one place this definition argues *for* an R4 outcome, so the argument is stated plainly:
C's failure mode is worse than an honest R4, because replication that has quietly stopped
gives the team confidence they have not earned. An R4 backup everyone knows is manual beats
an R2 backup nobody can tell is broken. The README says in its first paragraph that
durability is the deployer's.

### c. Where the public site is hosted

**The brief does not say.** A managed static host is R1 and nothing to run, but its logs
hold every visitor's address; a static container in the team's own compose keeps the promise
boundary in the team's hands at the cost of one more thing running. **This is the owner's,
because the privacy page has to name its own host's logging either way** — it is part of the
promise text, not an infrastructure preference. The proposed default is the static
container, because it is the only option under which "we don't keep IP addresses" (`#L25`)
is true of the whole product rather than of one container.

## Consequences

- The link database is a visible file at a known path, which is what makes any backup story
  possible at all.
- The first release ships with manual backups, said out loud rather than implied.
- The public site's host becomes a sentence on the privacy page.
