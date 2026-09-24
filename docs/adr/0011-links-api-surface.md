# ADR-0011 — The links API surface: routes, methods, status codes and JSON field names

- Status: Accepted
- Approver: claude
- Date: 2026-09-22

## Context

ADR-0001 settles *where* the service's own routes live (`/-/…`) and ADR-0003 settles what a
follow answers. Neither says what the create and delete calls are called, what they accept,
or what they answer — and ADR-0008b says plainly that "the JSON is the contract while the
human-readable output is not". Four other pieces of work are written against that contract:
the CLI (LL-003), `demo.sh` (LL-008), the stats page (LL-016) and the daily counts (LL-006).

This is not a one-way door of the same size as the URL shape: nothing is printed on a
poster. But it is an installed-base decision in the sense ADR-0008 uses — a field name that
scripts parse, and a status code a shell script branches on, cannot be withdrawn once
people have written against them — so it is recorded rather than left implicit in handlers.

The route strings and the two request fields are not invented here: `REQUIREMENTS.md`
R-001, R-002 and R-007 already state `POST $LINKLING_BASE/-/api/links`,
`DELETE $LINKLING_BASE/-/api/links/<name>` and the keys `url` and `name`. This ADR records
them and decides the rest.

## Decision

**Proposed.**

| Call | Request | Success | Refusals |
|---|---|---|---|
| `POST /-/api/links` | `{"url": …, "name": …?, "created_by": …?}` | `201` `{"name", "url"}` | `401` no/wrong key; `422` bad name, bad target, or an unknown field; `409` name taken; `503` the generator found no free name |
| `DELETE /-/api/links/<name>` | — | `204`, no body | `401` no/wrong key; `404` never existed; `410` already deleted |
| `GET\|HEAD /<name>` | — | `302` + `Location` + `no-store` | `404` unknown (ADR-0003); `410` deleted (ADR-0003) |
| `GET /-/api/links/<name>/stats` ⚠️ Added 2026-09-24 | — | `200` `{"days": {"YYYY-MM-DD": count}}` | `401` no/wrong key, checked before the name; `404` never existed; `410` deleted; an expired link is `200` with its kept counts |
| `GET /-/stats` ⚠️ Added 2026-09-24 | — (HTTP Basic: any user name, the team key as the password) | `200` a plain HTML page: every link that has not been deleted, with its count for each UTC day | `401` no/wrong key, with `WWW-Authenticate: Basic` |
| `GET\|HEAD /<name>/` ⚠️ Added 2026-09-24 | — | as `GET\|HEAD /<name>` (ADR-0001e: `/<name>/` is treated as `/<name>`) | as `GET\|HEAD /<name>`; `/<name>//` is not a link (`404`) |

> ⚠️ **Added 2026-09-24 (LL-027), after this ADR was accepted.** The three rows marked above
> record routes `create_app` already registers, so that the table lists every route the service
> serves. They are worded from `src/linkling/server/app.py` and checked against the running app;
> the decision text and the four choices below are unchanged. `HEAD` on either `GET`-only route
> (`/-/api/links/<name>/stats`, `/-/stats`) answers `405`, for the reason choice d gives.
> `/-/privacy.json` is not listed: it does not exist yet.

Four choices inside that table are this ADR's own, each with the alternative recorded:

### a. A repeat delete answers `410`, not `404` and not `204`

`404` would be a lie (the name is taken forever, which is why a re-create is refused) and
`204` would make a delete of a name that was never yours look successful. The alternative —
idempotent `204` — is the more common REST reflex, and it is rejected because a tombstone is
a real state this product needs callers to be able to see: `410` is exactly what a follow of
the same name returns.

### b. An unknown request field is refused, not ignored

The rejected alternative is Pydantic's default, which ignores extras. LL-010 will add
`expires`; a caller who sends `expires` to a service that has not shipped it yet would then
receive a link that silently never expires, and the brief's own demo asks to "make a link
that expires and show it stop working". A `422` today is the only answer that cannot be
mistaken for success.

### c. The response carries `name` and `url`, and no short URL yet

ADR-0007a introduces `LINKLING_PUBLIC_URL` so the CLI can print the real short URL rather
than derive it from a `Host` header. The alternative considered was returning `short_url`
here now. It is deferred rather than rejected: adding a field is additive and breaks no
caller, while a field returned today is one the CLI may end up parsing before anyone has
decided what a deployment's public URL is.

### d. `HEAD` follows a link exactly as `GET` does

Starlette 1.6.0 does not add `HEAD` to a `GET` route (verified: it answered `405`), and chat
clients that unfurl a pasted link, plus `curl -I`, send `HEAD`. The alternative — leaving
`HEAD` at `405` — makes the most common automated probe of a short link fail for a reason
that has nothing to do with the link.

## Consequences

- LL-003's CLI, LL-008's `demo.sh` and LL-016's page have one written contract to build
  against, and a change to it is a diff on this file rather than a surprise in a handler.
- `410` on a repeat delete means a client can tell "there was never such a link" from "that
  link is gone", which is the distinction ADR-0005's tombstone exists to preserve.
- Adding fields to the request or the response stays additive. Removing one, or changing a
  status code, is what this ADR exists to make visible.

**What would settle the one open question here:** whether `short_url` belongs in the create
response is answerable the moment LL-003 is written — if the CLI would have to reconstruct
it from two variables, the server should send it.

## Decision record

Proposed, pending a stakeholder's acceptance through the manager. Written while building
LL-001, whose report names this file.

## Decision record

Accepted 2026-09-22 by `claude`, as the program manager `pm-2`, under two rows of
`products/linkling/decision-policy.md`: *internal module layout* is Claude's inside a repo, and
*build and release pipeline shape* is Claude's with an ADR. This is the JSON contract beneath the
`/-/…` namespace **ADR-0001 already reserved**; it does not touch the short-URL shape, which is the
owner's and is settled there. LL-003 (the CLI) and LL-016 (the stats page) may build against it.

Deferred rather than decided, and recorded so it is not re-litigated: whether the API tolerates a
trailing slash on `POST /-/api/links/`. It is refused today. Loosening later is additive;
un-loosening is not.
