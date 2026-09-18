# ADR-0008 — Repo layout, the CLI vocabulary, and where the privacy truth lives

- Status: Accepted
- Approver: @jpslav
- Date: 2026-09-18

## Context

The executable name, the config location, the environment variable names and the CLI verbs
are installed on every laptop; the JSON field names are parsed by every script anyone writes.
The split is given: the service in `linkling-api`, the public site in `linkling-web`
(`raw/brief.md#L57`), and `linkling-api/README.md` (read on its trunk) already says the CLI
lives with the service.

This ADR also fixes **where the "what we store" truth lives**, because that is a layout
decision and it is what makes the brief's promise at `#L36-L38` machine-checkable instead of
a thing somebody remembers to re-read.

## Decision

**Proposed.**

### a. Layout and names

One package `linkling` in `linkling-api`, with `linkling.server` (FastAPI) and
`linkling.cli`. **The CLI talks HTTP only and never opens the database**, so it works from a
laptop against a remote deployment. Config from `LINKLING_URL` and `LINKLING_API_KEY`,
falling back to `~/.config/linkling/config.toml`. `linkling-web` is hand-written HTML and
CSS with no build step — a site generator would add a dependency stream somebody has to keep
updating, which is an R4 nobody asked for.

### b. The vocabulary is the brief's own

`linkling make <target> [--name q3-plan] [--expires 7d]`, `linkling list`,
`linkling delete <name>`, `linkling counts <name>` — the verbs at `#L30-L31`, taken
verbatim, because that line is the closest thing available to a user saying what they will
type. Every verb takes `--json`, and **the JSON is the contract while the human-readable
output is not**, said in the ADR so nobody writes a script that parses the pretty form.

The stored number is `count` in `daily_counts`, not `clicks`: the brief's word is "used"
(`#L24`), and calling it a click invites someone to later believe a click *event* exists to
be enriched. Alternatives considered and rejected: `create`/`rm`/`ls`, and `hits`/`visits`.

### c. The privacy manifest — one source, in the API repo

The service's stored fields are declared in a machine-readable manifest in `linkling-api`
(`privacy/what-we-store.json`: each table, each column, its purpose, its retention), and the
running service serves the same file at `/-/privacy.json` so anyone can check the *deployed*
copy rather than the repository's.

**Two tests in the API repo, and both must guard against emptiness:**

1. The schema test introspects a freshly migrated database and asserts the column set equals
   the manifest's, in both directions — **and asserts both sides are non-empty**, because an
   unmigrated database and a missing manifest agree on nothing and would otherwise pass.
2. The retention test asserts the behaviour the manifest claims, such as a tombstone
   dropping its target.

**The cross-repo half** — that the published page matches — is harder, because a workflow's
token reaches only its own repository, so a check in `linkling-web` needs a credential to
read `linkling-api`.

| Option | Blind case — what it prints when it measures nothing | Recurring |
|---|---|---|
| A. `linkling-web` CI fetches the manifest with a read-only token in a repo secret and diffs it against the page | Must print `PRIVACY MATCH BLIND: could not fetch manifest (HTTP <code>)` and **exit non-zero**. A fetch failure read as "no difference" is the whole failure mode | R1 after one owner action to create the token |
| **B. `demo.sh` compares the running service's `/-/privacy.json` with the sibling `linkling-web` checkout when present** | `PRIVACY MATCH BLIND: linkling-web checkout not found` and non-zero — **it must not pass when the sibling is absent**, or the release gate goes green having compared nothing | **R3** — it runs whenever the release gate runs |
| C. The page fetches `/-/privacy.json` at view time and renders it | If the fetch fails the page says "could not load what the service stores" rather than showing stale text | R1, at the cost of a static page that is no longer static |

**Recommend the manifest plus the two tests now (R1, depending on nothing), and B as the
release-gate check (R3, needing no secret), with A as a later upgrade priced at one owner
action.** B attaches to a ritual that already exists — the release gate runs `demo.sh`
(`#L66-L70`) — while A's extra value is catching drift *between* releases, which matters
only once the page changes independently of the service. **What is not acceptable in any
variant is a check that passes when it cannot find one side.**

## Consequences

- `linkling` is the executable, the import name and the config directory. Those are
  installed-base decisions: aliases can be added later, names cannot be withdrawn.
- The privacy promise has exactly one source of truth, in the repository that actually does
  the storing, and it is served by the running service so the deployed copy is checkable.
- `linkling-web` carries no record of a contract it must honour — see the Consequences note
  in the report; this ADR lands in `linkling-api/docs/adr/` and a pointer from the web repo
  is follow-up work.

## Decision record

Accepted 2026-09-18 by @jpslav, from line comments on [the definition PR](https://github.com/jpslav/tinyworks-program/pull/1). Written by `tools/decision-record.py`; each answer is also in its question file.

- **Are these the CLI's verbs: make, list, delete, counts?** — **A.** `linkling make`, `list`, `delete`, `counts` — the brief's own words — @jpslav, 2026-09-18: "this one" ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4047083739)) <!-- decided: 2026-09-17-cli-verbs-and-privacy-manifest/verbs: A -->
- **How is the privacy page checked against what the service actually stores?** — **A.** The release gate compares them — `demo.sh` diffs the running service's manifest against the `linkling-web` checkout; no secret — @jpslav, 2026-09-18: "this one" ([comment](https://github.com/jpslav/tinyworks-program/pull/1#discussion_r4047090762)) <!-- decided: 2026-09-17-cli-verbs-and-privacy-manifest/privacy-check: A -->
