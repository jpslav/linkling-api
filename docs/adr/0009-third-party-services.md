# ADR-0009 — No third party in the click path

- Status: Accepted
- Approver: @jpslav
- Date: 2026-09-22

## Context

The product exists because the hosted alternatives "quietly harvest data about everyone who
clicks" (`raw/brief.md#L8-L11`). Every third party in the click path sees clickers and
becomes a clause on the privacy page; every third party in the build path is a dependency
stream somebody maintains.

This looks like the most obvious decision in the set, which is exactly why it is written
down: the obvious ones are where the alternative never gets recorded and so can never be
revisited.

## Decision

**Proposed: none in the click path, none on the public site, and the minimum in the build
path.**

| Where | Proposed | Why, and what it forecloses |
|---|---|---|
| Redirect path | **Nothing.** No CDN, no external analytics, no error-tracking service | A CDN in front of redirects both caches them — undoing ADR-0003 — and logs every address. Error trackers capture request context including addresses unless scrubbed, and "unless scrubbed" is a setting somebody has to keep right (R4) |
| Link creation | **No outbound fetch of the target.** Some shorteners fetch the page title on create | That is the service making requests on the team's behalf to a site it was merely told about, and nobody asked for it |
| Public site | **No third-party fonts, scripts or embeds** | A privacy page that loads a font from a third party contradicts itself in its own markup. The site must be self-contained |
| Build path | GitHub Actions (`#L56`), a `python:3.12-slim` base image, no published registry | Compose builds locally (ADR-0007a), so no deployer needs registry credentials |

Options rejected, recorded so they are revisitable: publishing images to a registry
(additive later, but needs auth from every deployer); a replication sidecar (ADR-0007b, a
post-launch candidate); a QR-code library (`#L61` says later).

## Consequences

- The privacy page can say the service sends nothing about a click anywhere, and that
  sentence is true of the whole product rather than of one container.
- **Adding a third party later is not foreclosed — but the privacy page changes first**,
  which is the brief's own rule at `#L50-L51`. Removing one after it has logged clickers is
  impossible for the data already logged.
- So "none" is the reversible position, and that is the argument for holding it now rather
  than an appeal to minimalism.

## Decision record

Accepted 2026-09-22 by @jpslav, from line comments on [the definition PR](https://github.com/jpslav/tinyworks-program/pull/1). Written by `tools/decision-record.py`; each answer is also in its question file.

- **What will sit in front of the redirect service and terminate TLS?** — **A.** A proxy on a machine you run, logging no client addresses — say where in your comment — @jpslav, 2026-09-22: "Accepting the recommendation." ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4074334678)) <!-- decided: 2026-09-17-hosting-backups-and-site-host/tls-front: A -->
